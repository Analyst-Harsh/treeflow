# treeflow

Hand out, track and clean up a pool of git worktrees, so parallel work (several
Claude sessions, experiments, PR reviews) never happens in your main checkout.

```
~/code/myrepo/              main checkout, left alone
~/code/myrepo.wt/pikachu/   a slot
~/code/myrepo.wt/eevee/     another slot
```

## Install

```bash
uv tool install --editable ~/Dev/"AI Projects"/treeflow
echo 'eval "$(treeflow init)"' >> ~/.zshrc     # lets `get` and `cd` change your directory
```

The shell function only wraps `get` and `cd`; everything else goes straight to the CLI.

## Use

```bash
treeflow get                  # claim a slot at the latest origin/main and cd into it
treeflow get -b login-fix     # same, on branch login-fix (created if new, resumed if not)
treeflow get -b login-fix --code  # ...and open it in VS Code
treeflow get pikachu          # claim pikachu as you left it (created if it doesn't exist)
treeflow get pikachu -b fix   # claim pikachu and switch it to fix (only if it's clean)
treeflow cd pikachu           # jump into a slot; `treeflow cd` alone goes back to main
treeflow cd pikachu --code    # ...and open it in VS Code
treeflow cd --code            # open the worktree you're in (from any subfolder)
treeflow status              # every worktree, its status and the commands for it
                              # (* marks where you are; `list` works too)
treeflow release              # give the current slot back (or `release pikachu`)
treeflow rm pikachu           # delete a slot; its branch is kept
treeflow gc                   # delete every free slot
```

Every command says what it did, on stderr (stdout stays just the path):

```
✦ created pikachu  on new branch login-fix from origin/main (a1b2c3d)
↻ reused eevee  detached at a1b2c3d; `git switch -c <name>` before committing
○ released eevee  back to the pool
✕ removed pikachu  branch login-fix kept
```

Glyphs and colors only appear on a terminal; piped or with `NO_COLOR` set you get plain
`treeflow: ...` lines.

Slots are named by name (`pikachu`) or by path. `get --base REF` starts somewhere other
than `origin/HEAD`; `get --new` always makes a fresh slot.

Exit codes: `0` ok, `1` error, `2` bad usage, `3` refused to protect your work.

## How it decides

- **Claimed** = the slot has a git worktree lock whose reason starts with `treeflow`.
  You can see it in `git worktree list`, and plain `git worktree remove` refuses it too.
- **Free** = not locked, folder present, no uncommitted or untracked changes, no
  rebase/merge/bisect in progress, and no commits that only this HEAD reaches.
  Only free slots are reused or garbage-collected.
- **Never touched**: the main worktree, worktrees outside `<repo>.wt/` (for example
  `claude -w` ones under `.claude/worktrees/`), worktrees in `<repo>.wt/` whose name
  isn't a slot name, and worktrees you locked yourself. Not even with `--force`.
- `get` fetches `origin` first (offline, it warns and carries on). Without a branch the
  slot is a detached HEAD: run `git switch -c <name>` before committing.

## Known limits

- macOS and Linux only (uses `fcntl`).
- Claims never expire. A forgotten claim keeps its slot busy until you `release` it
  (`treeflow status` shows how old each claim is).
- `release` doesn't know who claimed a slot: releasing someone else's slot by name
  hands it back to the pool while they're still in it.
- Ignored files in a slot (`.env`, `node_modules`) survive reuse and are deleted by `rm`
  and `gc`.

## Develop

```bash
uv run pytest      # tests run against real temporary git repos
uv run ruff check
```
