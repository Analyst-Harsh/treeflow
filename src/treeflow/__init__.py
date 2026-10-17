"""treeflow: hand out, track and clean up a pool of git worktrees.

Slots live next to the repo in `<repo>.wt/pikachu`, `<repo>.wt/eevee`, ... (older
`wtN` slots still count). A slot is *claimed* while it carries a git worktree lock
whose reason starts with "treeflow", so plain git also refuses to remove or prune
it. A slot is *free* when it is unclaimed, present on disk, clean, mid nothing
(rebase, merge, bisect...), and has no commits that only HEAD reaches.
"""

from __future__ import annotations

import argparse
import fcntl
import itertools
import os
import random
import re
import shutil
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

CLAIM = "treeflow"

# Gen 1, minus the names that aren't filesystem-safe (nidoran♀/♂, mr. mime).
NAMES = """
bulbasaur ivysaur venusaur charmander charmeleon charizard squirtle wartortle blastoise
caterpie metapod butterfree weedle kakuna beedrill pidgey pidgeotto pidgeot rattata
raticate spearow fearow ekans arbok pikachu raichu sandshrew sandslash nidorina nidoqueen
nidorino nidoking clefairy clefable vulpix ninetales jigglypuff wigglytuff zubat golbat
oddish gloom vileplume paras parasect venonat venomoth diglett dugtrio meowth persian
psyduck golduck mankey primeape growlithe arcanine poliwag poliwhirl poliwrath abra
kadabra alakazam machop machoke machamp bellsprout weepinbell victreebel tentacool
tentacruel geodude graveler golem ponyta rapidash slowpoke slowbro magnemite magneton
farfetchd doduo dodrio seel dewgong grimer muk shellder cloyster gastly haunter gengar
onix drowzee hypno krabby kingler voltorb electrode exeggcute exeggutor cubone marowak
hitmonlee hitmonchan lickitung koffing weezing rhyhorn rhydon chansey tangela kangaskhan
horsea seadra goldeen seaking staryu starmie scyther jynx electabuzz magmar pinsir tauros
magikarp gyarados lapras ditto eevee vaporeon jolteon flareon porygon omanyte omastar
kabuto kabutops aerodactyl snorlax articuno zapdos moltres dratini dragonair dragonite
mewtwo mew
""".split()
SLOT_RE = re.compile(rf"wt\d+|(?:{'|'.join(NAMES)})(?:-\d+)?")

# Repo-locating variables (as listed by `git rev-parse --local-env-vars`). Inherited
# from a git hook they would override `cwd`, and is_safe would inspect the wrong tree.
LOCAL_ENV = {
    "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX",
    "GIT_SHALLOW_FILE", "GIT_GRAFT_FILE",
}  # fmt: skip

# Files in a worktree's git dir that mean an operation is half done.
BUSY = (
    "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-merge", "rebase-apply",
)  # fmt: skip

SHELL_INIT = """\
treeflow() {
  case "$1" in
    get|cd)
      local out
      out="$(command treeflow "$@")" || return
      if [ -d "$out" ]; then cd -- "$out"; else printf '%s\\n' "$out"; fi
      ;;
    *) command treeflow "$@" ;;
  esac
}
"""


class Error(Exception):
    """Exit code 1: git failed or the request makes no sense."""


class Refused(Error):
    """Exit code 3: refused because it could lose work or touch something not ours."""


# ---------------------------------------------------------------- git helpers


def git(*args: str, cwd: Path | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if k not in LOCAL_ENV}
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if r.returncode:
        raise Error(r.stderr.strip() or f"git {args[0]} failed")
    return r.stdout


@dataclass
class Worktree:
    path: Path
    head: str = ""
    branch: str | None = None  # None when detached
    locked: str | None = None  # lock reason, "" if locked without one
    prunable: bool = False
    bare: bool = False

    @property
    def claimed(self) -> bool:
        return self.locked is not None and self.locked.startswith(CLAIM + " ")

    @property
    def user_locked(self) -> bool:
        return self.locked is not None and not self.claimed

    @property
    def missing(self) -> bool:
        return self.prunable or not self.path.exists()


def worktrees(cwd: Path) -> list[Worktree]:
    """Parse `git worktree list --porcelain -z`. The main worktree comes first."""
    wts: list[Worktree] = []
    cur: Worktree | None = None
    for field in git("worktree", "list", "--porcelain", "-z", cwd=cwd).split("\0"):
        if not field:  # an empty field ends a record
            if cur:
                wts.append(cur)
            cur = None
            continue
        key, _, val = field.partition(" ")
        if key == "worktree":
            cur = Worktree(Path(val))
        elif cur is None:
            continue
        elif key == "HEAD":
            cur.head = val
        elif key == "branch":
            cur.branch = val.removeprefix("refs/heads/")
        elif key == "locked":
            cur.locked = val
        elif key == "prunable":
            cur.prunable = True
        elif key == "bare":
            cur.bare = True
    if cur:
        wts.append(cur)
    return wts


def is_safe(path: Path) -> bool:
    """True if removing or reusing this worktree cannot lose work.

    Dirty files are work. So are commits reachable only from this HEAD (made on
    a detached HEAD); commits on a branch survive because the branch is kept. A
    half-done rebase, merge or bisect is work too. The explicit status flags
    override user config that would hide changes.
    """
    gitdir = Path(git("rev-parse", "--absolute-git-dir", cwd=path).strip())
    if any((gitdir / f).exists() for f in BUSY):
        return False
    status = git(
        "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none", cwd=path
    )
    if status.strip():
        return False
    orphans = git(
        "rev-list", "--max-count=1", "HEAD", "--not", "--branches", "--remotes", "--tags", cwd=path
    )
    return not orphans.strip()


# ----------------------------------------------------------------------- pool


@dataclass
class Repo:
    main: Path  # main worktree, resolved
    pool: Path  # <parent>/<repo>.wt, resolved
    common: Path  # shared .git dir, holds the flock file


def repo(cwd: Path) -> Repo:
    """Find the repo from anywhere inside it, including from inside a slot."""
    wts = worktrees(cwd)
    if wts[0].bare:
        raise Error("bare repositories are not supported")
    main = wts[0].path.resolve()
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd).strip()
    return Repo(main, (main.parent / f"{main.name}.wt").resolve(), Path(common))


def is_managed(r: Repo, wt: Worktree) -> bool:
    p = wt.path.resolve()  # macOS: /tmp and /var are really /private/...
    return p.parent == r.pool and bool(SLOT_RE.fullmatch(p.name))


def is_free(r: Repo, wt: Worktree) -> bool:
    return is_managed(r, wt) and wt.locked is None and not wt.missing and is_safe(wt.path)


def next_name(r: Repo, wts: list[Worktree]) -> str:
    """A random unused name; skips registered slots and leftover folders alike."""
    used = {w.path.resolve().name for w in wts if is_managed(r, w)}
    if r.pool.is_dir():
        used |= {p.name for p in r.pool.iterdir()}
    for k in itertools.count(1):  # all taken: pikachu-2, eevee-2, ...
        left = [m for n in NAMES if (m := n if k == 1 else f"{n}-{k}") not in used]
        if left:
            return random.choice(left)
    raise AssertionError("unreachable")


def slot_path(r: Repo, slot: str) -> Path:
    """A bare name means a slot in the pool; anything with a slash is a path."""
    return Path(slot) if "/" in slot or slot in (".", "..") else r.pool / slot


def find(r: Repo, path: Path) -> Worktree:
    target = path.resolve()
    for wt in worktrees(r.main):
        if wt.path.resolve() == target:
            return wt
    raise Error(f"{path} is not a worktree of {r.main}")


def current(cwd: Path) -> Path | None:
    try:
        return Path(git("rev-parse", "--show-toplevel", cwd=cwd).strip()).resolve()
    except Error:  # e.g. cwd is inside .git
        return None


@contextmanager
def pool_lock(r: Repo) -> Generator[None]:
    """Serialise check-then-act across processes: git's own lock check isn't atomic."""
    with open(r.common / "treeflow.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def claim_reason() -> str:
    return f"{CLAIM} {datetime.now(UTC).isoformat(timespec='seconds')}"


def fetch(r: Repo) -> None:
    """Best effort: offline or a broken remote must not stop you from working."""
    if "origin" not in git("remote", cwd=r.main).split():
        return
    try:
        git("fetch", "--quiet", "origin", cwd=r.main)
    except Error as e:
        warn(f"fetch failed, starting from local refs: {e}")


def default_base(r: Repo) -> str:
    try:
        return git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=r.main).strip()
    except Error:  # no remote: start from wherever the main worktree is
        return git("rev-parse", "HEAD", cwd=r.main).strip()


def has_ref(r: Repo, ref: str) -> bool:
    try:
        git("rev-parse", "--verify", "--quiet", ref, cwd=r.main)
        return True
    except Error:
        return False


def find_slot(r: Repo, slot: str) -> Worktree | None:
    """The registered worktree a slot name or path points at, if any."""
    try:
        return find(r, slot_path(r, slot))
    except Error:
        return None


def claimable(r: Repo, wt: Worktree) -> None:
    name = wt.path.name
    if not is_managed(r, wt):
        raise Refused(f"{wt.path} is not a treeflow slot")
    if wt.claimed:
        raise Refused(f"{name} is already claimed; `treeflow release {name}` it first")
    if wt.user_locked:
        raise Refused(f"{wt.path} was locked by someone else ({wt.locked!r})")
    if wt.missing:
        raise Error(f"{wt.path} no longer exists on disk")


def take(r: Repo, wt: Worktree) -> Path:
    """Claim a slot without touching its files: resuming work, dirty or not."""
    git("worktree", "lock", "--reason", claim_reason(), str(wt.path), cwd=r.main)
    where = f"on branch {wt.branch}" if wt.branch else f"detached at {wt.head[:7]}"
    say("↻", CYAN, f"reused {paint(BOLD, wt.path.name)}", f"as you left it, {where}")
    return wt.path


def get(
    r: Repo,
    branch: str | None = None,
    base: str | None = None,
    new: bool = False,
    slot: str | None = None,
) -> Path:
    """Claim a slot (the named one, else the first free one, else a new one).

    A named slot that exists is taken as you left it, unless a branch or base asks
    for a checkout, which needs it clean. A named slot that doesn't exist is created.
    Otherwise the slot ends up at `branch`, or detached at base. An existing branch
    is checked out as is (resuming the unclaimed slot that already holds it), one
    that only exists on origin is tracked from there, and a new one is created at
    base without an upstream (so `git push -u origin <branch>` sets the right one).
    """
    if slot and new:
        raise Error("--new makes a slot with a random name; drop it or the slot name")
    if slot and not (branch or base):
        with pool_lock(r):
            if wt := find_slot(r, slot):
                claimable(r, wt)
                return take(r, wt)
    fetch(r)
    base = base or default_base(r)
    # Check the request up front, so a failing switch below means a bad slot, not bad input.
    try:
        sha = git("rev-parse", "--short", "--verify", "--quiet", f"{base}^{{commit}}", cwd=r.main)
    except Error:
        raise Error(f"unknown base {base!r}") from None
    sha = sha.strip()
    if branch:
        git("check-ref-format", "--branch", branch, cwd=r.main)
    with pool_lock(r):
        wts = worktrees(r.main)
        if not branch:
            switch, add = ["--detach", base], ["--detach", "{path}", base]
            what = f"detached at {sha}; `git switch -c <name>` before committing"
        elif has_ref(r, f"refs/heads/{branch}"):
            if holder := next((w for w in wts if w.branch == branch), None):
                mine = slot is None or find_slot(r, slot) == holder
                if mine and is_managed(r, holder) and holder.locked is None and not holder.missing:
                    return take(r, holder)
                raise Error(f"branch {branch} is already checked out at {holder.path}")
            switch, add = [branch], ["{path}", branch]
            what = f"on branch {branch}"
        elif has_ref(r, f"refs/remotes/origin/{branch}"):
            remote = f"origin/{branch}"
            switch, add = (
                ["-c", branch, "--track", remote],
                ["-b", branch, "--track", "{path}", remote],
            )
            what = f"on branch {branch}, tracking {remote}"
        else:
            switch, add = (
                ["-c", branch, "--no-track", base],
                ["-b", branch, "--no-track", "{path}", base],
            )
            from_ = sha if base.startswith(sha) else f"{base} ({sha})"
            what = f"on new branch {branch} from {from_}"

        if slot and (wt := find_slot(r, slot)):
            claimable(r, wt)
            if not is_safe(wt.path):
                raise Refused(
                    f"{wt.path.name} has uncommitted changes or commits on no branch; "
                    f"`treeflow get {wt.path.name}` takes it as is"
                )
            git("worktree", "lock", "--reason", claim_reason(), str(wt.path), cwd=r.main)
            try:
                git("switch", *switch, cwd=wt.path)
            except Error:
                git("worktree", "unlock", str(wt.path), cwd=r.main)
                raise
            say("↻", CYAN, f"reused {paint(BOLD, wt.path.name)}", what)
            return wt.path
        if slot and not SLOT_RE.fullmatch(slot):
            raise Error(
                f"{slot!r} is not a slot name (Gen-1 Pokémon, like pikachu); "
                f"for a branch, use `treeflow get -b {slot}`"
            )
        if not new and not slot:
            for wt in sorted((w for w in wts if is_managed(r, w)), key=lambda w: w.path.name):
                if not is_free(r, wt):
                    continue
                git("worktree", "lock", "--reason", claim_reason(), str(wt.path), cwd=r.main)
                try:
                    git("switch", *switch, cwd=wt.path)
                    say("↻", CYAN, f"reused {paint(BOLD, wt.path.name)}", what)
                    return wt.path
                except Error as e:  # this slot is broken somehow; try the next one
                    git("worktree", "unlock", str(wt.path), cwd=r.main)
                    warn(f"skipped {wt.path.name}: {e}")
        path = r.pool / (slot or next_name(r, wts))
        add = [str(path) if a == "{path}" else a for a in add]
        git("worktree", "add", "--quiet", "--lock", "--reason", claim_reason(), *add, cwd=r.main)
        say("✦", GREEN, f"created {paint(BOLD, path.name)}", what)
        return path


def release(r: Repo, path: Path) -> None:
    with pool_lock(r):
        wt = find(r, path)
        if not is_managed(r, wt):
            raise Refused(f"{wt.path} is not a treeflow slot")
        if wt.user_locked:
            raise Refused(f"{wt.path} was locked by someone else ({wt.locked!r}); not unlocking")
        if wt.claimed:
            git("worktree", "unlock", str(wt.path), cwd=r.main)
            say("○", DIM, f"released {paint(BOLD, wt.path.name)}", "back to the pool")
        else:
            warn(f"{wt.path.name} was not claimed")


def remove(r: Repo, path: Path, force: bool = False) -> None:
    with pool_lock(r):
        wt = find(r, path)
        if wt.path.resolve() == r.main:
            raise Refused("refusing to remove the main worktree")
        if not is_managed(r, wt):
            raise Refused(f"{wt.path} is not a treeflow slot; remove it with git")
        if wt.user_locked:
            raise Refused(f"{wt.path} was locked by someone else ({wt.locked!r})")
        if not force:
            if wt.claimed:
                raise Refused(f"{wt.path} is claimed; `treeflow release` it first or use --force")
            if not wt.missing and not is_safe(wt.path):
                raise Refused(
                    f"{wt.path} has uncommitted changes or commits on no branch; use --force"
                )
        if wt.locked is not None:
            git("worktree", "unlock", str(wt.path), cwd=r.main)
        if wt.missing:
            # Drops just this record; `git worktree prune` would drop foreign ones too.
            git("worktree", "remove", str(wt.path), cwd=r.main)
        else:
            # --force is safe here: our checks passed (or the user forced), and
            # git would otherwise refuse worktrees that contain submodules.
            git("worktree", "remove", "--force", str(wt.path), cwd=r.main)
        kept = f"branch {wt.branch} kept" if wt.branch else ""
        say("✕", DIM, f"removed {paint(BOLD, wt.path.name)}", kept)


def gc(r: Repo) -> list[Path]:
    """Remove every free slot and the records of unclaimed vanished ones."""
    removed: list[Path] = []
    with pool_lock(r):
        for wt in worktrees(r.main):
            if not is_managed(r, wt) or wt.locked is not None:
                continue
            if not wt.missing and not is_safe(wt.path):
                continue
            try:
                # No --force: git re-checks for changes at removal time.
                git("worktree", "remove", str(wt.path), cwd=r.main)
                removed.append(wt.path)
            except Error as e:
                warn(f"kept {wt.path}: {e}")
    return removed


def status(r: Repo, wt: Worktree) -> str:
    if wt.path.resolve() == r.main:
        return "main"
    if not is_managed(r, wt):
        return "foreign"
    if wt.claimed:
        s = f"claimed {age(wt.locked)}"
        return s + ", missing: run release" if wt.missing else s
    if wt.user_locked:
        return "user-locked"
    if wt.missing:
        return "missing"
    return "free" if is_safe(wt.path) else "dirty"


def age(reason: str | None) -> str:
    try:
        then = datetime.fromisoformat((reason or "").split(" ", 1)[1])
        mins = int((datetime.now(UTC) - then).total_seconds() // 60)
    except (IndexError, ValueError, TypeError):  # TypeError: timestamp without a timezone
        return "?"
    if mins < 60:
        return f"{mins}m"
    return f"{mins // 60}h" if mins < 24 * 60 else f"{mins // (24 * 60)}d"


# ------------------------------------------------------------------------ cli


BOLD, DIM, RED, GREEN, YELLOW, MAGENTA, CYAN = "1", "2", "31", "32", "33", "35", "36"
STATUS_COLOR = {
    "free": GREEN, "claimed": CYAN, "dirty": YELLOW, "missing": RED, "user-locked": MAGENTA,
    "main": DIM, "foreign": DIM,
}  # fmt: skip


def fancy(stream) -> bool:
    return stream.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def paint(code: str, text: str, stream=None) -> str:
    stream = stream or sys.stderr
    return f"\x1b[{code}m{text}\x1b[0m" if code and fancy(stream) else text


def say(glyph: str, color: str, msg: str, hint: str = "") -> None:
    """A glyph and colors on a terminal; the plain `treeflow: msg, hint` everywhere else."""
    if fancy(sys.stderr):
        line = f"{paint(color, glyph)} {msg}" + (f"  {paint(DIM, hint)}" if hint else "")
    else:
        line = f"treeflow: {msg}" + (f", {hint}" if hint else "")
    print(line, file=sys.stderr)


def warn(msg: str) -> None:
    say("!", YELLOW, msg)


def open_in_vscode(path: Path) -> None:
    if code := shutil.which("code"):
        cmd = [code, str(path)]
    elif sys.platform == "darwin":
        cmd = ["open", "-a", "Visual Studio Code", str(path)]
    else:
        return warn("`code` is not on PATH; in VS Code run 'Shell Command: Install code'")
    # Detached with no stdout, so `cd "$(treeflow get --code)"` doesn't wait for the editor.
    subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )  # fmt: skip
    say("↗", DIM, f"opened {paint(BOLD, path.name)} in VS Code")


def hints(name: str, st: str, here: bool) -> list[str]:
    """Commands that make sense for a slot in this state; none for worktrees that aren't ours."""
    me = "" if here else f" {name}"  # bare `release` and `cd --code` act on where you are
    kind = st.split()[0]
    if kind == "claimed":
        if "missing" in st:
            return [f"treeflow release {name}"]
        return [f"treeflow release{me}", f"treeflow cd{me} --code"]
    if kind == "dirty":
        return [f"treeflow get {name}", f"treeflow rm {name} --force"]
    if kind == "free":
        return [f"treeflow get {name}", f"treeflow rm {name}"]
    if kind == "missing":
        return [f"treeflow rm {name}"]
    return []


def print_table(r: Repo, here: Path | None) -> None:
    """The table alone when piped; on a terminal, also what you can do next."""
    rows = [(" ", "NAME", "BRANCH", "STATUS", "PATH")]
    for wt in worktrees(r.main):
        mark = "*" if wt.path.resolve() == here else " "
        name = wt.path.name if is_managed(r, wt) else "-"
        rows.append((mark, name, wt.branch or "(detached)", status(r, wt), str(wt.path)))
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    tty = sys.stdout.isatty()
    for i, row in enumerate(rows):
        # Pad before painting: escape codes would throw off ljust.
        cells = [cell.ljust(w) for cell, w in zip(row[:4], widths, strict=True)]
        if i:
            cells[0] = paint(BOLD, cells[0], sys.stdout)
            cells[3] = paint(STATUS_COLOR.get(row[3].split()[0], ""), cells[3], sys.stdout)
        print("  ".join(cells) + "  " + row[4])
        if i and tty and (h := hints(row[1], row[3], row[0] == "*")):
            print(" " * (widths[0] + 2) + paint(DIM, "› " + " · ".join(h), sys.stdout))
    if not tty:
        return
    free = sorted(row[1] for row in rows if row[3] == "free")
    print()
    reuse = f"reuses {free[0]}" if free else "creates a new one"
    print(f"  treeflow get                claim a slot {paint(DIM, f'({reuse})', sys.stdout)}")
    print("  treeflow get <slot>         claim that slot as you left it")
    print("  treeflow get --branch <b>   ...on branch b; add --code to open VS Code")
    if free or any(row[3] == "missing" for row in rows):
        print("  treeflow gc                 remove free slots")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="treeflow", description="Hand out, track and clean up a pool of git worktrees."
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get", help="claim a slot (free, named or new) and print its path")
    g.add_argument("slot", nargs="?", help="claim this slot as you left it (created if new)")
    g.add_argument("-b", "--branch", help="check out this branch, creating it if new")
    g.add_argument("--base", help="commit to start from (default: origin/HEAD, fetched)")
    g.add_argument("--new", action="store_true", help="always create a new slot")
    g.add_argument("--code", action="store_true", help="open the slot in VS Code")
    c = sub.add_parser("cd", help="print a slot's path (the shell function cd's there)")
    c.add_argument(
        "slot", nargs="?", help="slot name, path or . (default: main; with --code, the current one)"
    )
    c.add_argument("--code", action="store_true", help="open the slot in VS Code")
    sub.add_parser(
        "status", aliases=["list"], help="show every worktree, its status and what you can do next"
    )
    rl = sub.add_parser("release", help="give a slot back to the pool")
    rl.add_argument("slot", nargs="?", help="slot name or path (default: the current one)")
    rm = sub.add_parser("rm", help="delete a slot (its branch is kept)")
    rm.add_argument("slot", help="slot name or path")
    rm.add_argument("--force", action="store_true", help="also remove claimed or dirty slots")
    sub.add_parser("gc", help="remove every free slot")
    sub.add_parser("init", help='print the shell function for `eval "$(treeflow init)"`')
    a = p.parse_args(argv)

    if a.cmd == "init":
        print(SHELL_INIT, end="")
        return 0
    try:
        r = repo(Path.cwd())
        if a.cmd in ("get", "cd"):
            if a.cmd == "get":
                path = get(r, a.branch, a.base, a.new, a.slot)
            else:
                if a.slot == "." or (not a.slot and a.code):  # the worktree you're in
                    target = current(Path.cwd()) or Path.cwd()
                else:
                    target = slot_path(r, a.slot) if a.slot else r.main
                wt = find(r, target)
                if wt.missing:
                    raise Error(f"{wt.path} no longer exists on disk")
                path = wt.path
            if a.code:
                open_in_vscode(path)
            if sys.stdout.isatty():  # not captured by the shell function
                say("›", DIM, 'tip: add `eval "$(treeflow init)"` to ~/.zshrc', "to cd there")
            print(path)
        elif a.cmd in ("status", "list"):
            print_table(r, current(Path.cwd()))
        elif a.cmd == "release":
            here = slot_path(r, a.slot) if a.slot else current(Path.cwd()) or Path.cwd()
            release(r, here)
        elif a.cmd == "rm":
            remove(r, slot_path(r, a.slot), a.force)
        elif a.cmd == "gc":
            removed = gc(r)
            for path in removed:
                say("✕", DIM, f"removed {paint(BOLD, path.name)}")
            if not removed:
                say("·", DIM, "nothing to remove")
    except Refused as e:
        head, _, hint = str(e).partition("; ")
        if hint and fancy(sys.stderr):
            say("✗", RED, head)
            print(f"  {paint(DIM, '› ' + hint)}", file=sys.stderr)
        else:
            say("✗", RED, str(e))
        return 3
    except Error as e:
        say("✗", RED, str(e))
        return 1
    return 0
