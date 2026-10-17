"""Tests run against real git repos in tmp_path; nothing is mocked."""

import subprocess
import sys
from pathlib import Path

import pytest

import treeflow as tf


def sh(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def clone(tmp_path, monkeypatch) -> Path:
    """A clone of a bare "origin", at a path with a space in it."""
    for k, v in {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }.items():
        monkeypatch.setenv(k, v)
    origin, seed, repo = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "my repo"
    sh("git", "init", "-q", "--bare", "-b", "main", str(origin))
    sh("git", "init", "-q", "-b", "main", str(seed))
    (seed / "a.txt").write_text("a\n")
    sh("git", "add", ".", cwd=seed)
    sh("git", "commit", "-qm", "init", cwd=seed)
    sh("git", "push", "-q", str(origin), "main", cwd=seed)
    sh("git", "clone", "-q", str(origin), str(repo))
    return repo


def commit(path: Path, name: str) -> None:
    (path / name).write_text(name)
    sh("git", "add", ".", cwd=path)
    sh("git", "commit", "-qm", name, cwd=path)


def slot_status(r, path: Path) -> str:
    return tf.status(r, tf.find(r, path))


def test_get_creates_then_reuses_released_slot(clone):
    r = tf.repo(clone)
    p1, p2 = tf.get(r), tf.get(r)
    assert p1 != p2 and {p1.name, p2.name} <= set(tf.NAMES)
    assert p1.parent == r.pool and r.pool.name == "my repo.wt"
    tf.release(r, p1)
    assert tf.get(r) == p1


def test_dirty_slot_is_not_reused_even_if_config_hides_untracked(clone):
    r = tf.repo(clone)
    p1 = tf.get(r)
    (p1 / "notes.txt").write_text("unsaved")
    tf.release(r, p1)
    sh("git", "config", "status.showUntrackedFiles", "no", cwd=clone)
    assert slot_status(r, p1) == "dirty"
    assert tf.get(r) != p1


def test_orphan_commits_on_detached_head_block_reuse_and_rm(clone):
    r = tf.repo(clone)
    p1 = tf.get(r)
    commit(p1, "orphan.txt")  # detached HEAD: no branch holds this commit
    tf.release(r, p1)
    assert tf.get(r) != p1
    with pytest.raises(tf.Refused):
        tf.remove(r, p1)


def test_rm_keeps_branch_with_unpushed_commits(clone):
    r = tf.repo(clone)
    p1 = tf.get(r)
    sh("git", "switch", "-qc", "feat", cwd=p1)
    commit(p1, "f.txt")
    tf.release(r, p1)
    tf.remove(r, p1)
    assert not p1.exists()
    assert "feat" in sh("git", "branch", "--list", "feat", cwd=clone)


def test_rm_refusals(clone, tmp_path):
    r = tf.repo(clone)
    p1 = tf.get(r)
    with pytest.raises(tf.Refused, match="claimed"):
        tf.remove(r, p1)
    tf.remove(r, p1, force=True)
    assert not p1.exists()

    with pytest.raises(tf.Refused, match="main"):
        tf.remove(r, clone, force=True)

    foreign = tmp_path / "elsewhere"
    sh("git", "worktree", "add", "-q", "--detach", str(foreign), cwd=clone)
    with pytest.raises(tf.Refused, match="not a treeflow slot"):
        tf.remove(r, foreign, force=True)

    p2 = tf.get(r)
    sh("git", "worktree", "unlock", str(p2), cwd=clone)
    sh("git", "worktree", "lock", "--reason", "mine", str(p2), cwd=clone)
    with pytest.raises(tf.Refused, match="someone else"):
        tf.remove(r, p2, force=True)
    with pytest.raises(tf.Refused):
        tf.release(r, p2)
    assert slot_status(r, p2) == "user-locked"
    assert slot_status(r, foreign) == "foreign"


def test_plain_git_cannot_remove_a_claimed_slot(clone):
    r = tf.repo(clone)
    p1 = tf.get(r)
    res = subprocess.run(["git", "worktree", "remove", str(p1)], cwd=clone, capture_output=True)
    assert res.returncode != 0 and p1.exists()


def test_deleted_slot_folders(clone):
    r = tf.repo(clone)
    p1, p2 = tf.get(r), tf.get(r)
    tf.release(r, p1)
    sh("rm", "-rf", str(p1), str(p2))
    assert slot_status(r, p1) == "missing"
    assert slot_status(r, p2).endswith("missing: run release")
    assert tf.get(r) not in (p1, p2)  # missing slots are skipped, never handed out

    tf.gc(r)  # p1's record dropped; p2 is still claimed (locked), so git keeps it
    names = {wt.path.name for wt in tf.worktrees(clone)}
    assert p1.name not in names and p2.name in names
    tf.release(r, p2)
    tf.gc(r)
    assert p2.name not in {wt.path.name for wt in tf.worktrees(clone)}


def test_works_from_inside_a_slot(clone):
    r = tf.repo(clone)
    p1 = tf.get(r)
    inner = tf.repo(p1)
    assert inner.main == r.main and inner.pool == r.pool
    assert tf.get(inner) != p1

    (p1 / "sub").mkdir()
    res = subprocess.run(
        [sys.executable, "-c", "import sys, treeflow; sys.exit(treeflow.main(['release']))"],
        cwd=p1 / "sub", capture_output=True, text=True,
    )  # fmt: skip
    assert res.returncode == 0, res.stderr
    assert slot_status(r, p1) == "free"


def test_concurrent_gets_return_different_slots(clone):
    r = tf.repo(clone)
    tf.release(r, tf.get(r))  # one free slot: both processes will want it
    cmd = [sys.executable, "-c", "import sys, treeflow; sys.exit(treeflow.main(['get']))"]
    procs = [subprocess.Popen(cmd, cwd=clone, stdout=subprocess.PIPE, text=True) for _ in range(2)]
    outs = [p.communicate()[0].strip() for p in procs]
    assert all(p.returncode == 0 for p in procs)
    assert len(set(outs)) == 2


def test_bad_base_leaves_no_claim(clone):
    r = tf.repo(clone)
    with pytest.raises(tf.Error):
        tf.get(r, base="nope")  # create path
    p1 = tf.get(r)
    tf.release(r, p1)
    with pytest.raises(tf.Error):
        tf.get(r, base="nope")  # reuse path: lock, switch fails, unlock
    assert slot_status(r, p1) == "free"


def test_gc_removes_only_free_slots(clone):
    r = tf.repo(clone)
    free, dirty, claimed = tf.get(r), tf.get(r), tf.get(r)
    tf.release(r, free)
    (dirty / "x").write_text("x")
    tf.release(r, dirty)
    assert tf.gc(r) == [free]
    assert dirty.exists() and claimed.exists() and not free.exists()


def test_cli_get_prints_only_the_path(clone):
    res = subprocess.run(
        [sys.executable, "-c", "import sys, treeflow; sys.exit(treeflow.main(['get']))"],
        cwd=clone, capture_output=True, text=True,
    )  # fmt: skip
    assert res.returncode == 0
    [out] = res.stdout.splitlines()
    assert Path(out).parent == tf.repo(clone).pool and Path(out).is_dir()
    assert "git switch -c" in res.stderr


def test_cli_exit_codes(clone, monkeypatch, capsys):
    monkeypatch.chdir(clone)
    assert tf.main(["rm", str(clone)]) == 3
    assert tf.main(["release", "/nowhere"]) == 1
    assert tf.main(["list"]) == 0
    assert "main" in capsys.readouterr().out


def test_names_legacy_slots_and_foreign_folders(clone, monkeypatch):
    r = tf.repo(clone)
    legacy, foreign = r.pool / "wt1", r.pool / "my-experiment"
    for path in (legacy, foreign):
        sh("git", "worktree", "add", "-q", "--detach", str(path), cwd=clone)
    assert slot_status(r, legacy) == "free" and slot_status(r, foreign) == "foreign"
    assert tf.gc(r) == [legacy] and foreign.exists()

    monkeypatch.setattr(tf, "NAMES", ["pikachu"])
    assert [tf.get(r, new=True).name for _ in range(2)] == ["pikachu", "pikachu-2"]


def test_cli_takes_slot_names(clone, monkeypatch, capsys):
    monkeypatch.chdir(clone)
    r = tf.repo(clone)
    p1 = tf.get(r)
    assert tf.main(["cd", p1.name]) == 0
    assert capsys.readouterr().out.strip() == str(p1)
    assert tf.main(["release", p1.name]) == 0 and slot_status(r, p1) == "free"
    assert tf.main(["rm", p1.name]) == 0 and not p1.exists()
    assert tf.main(["rm", "nosuchslot"]) == 1
    assert tf.main(["release", "."]) == 3  # the main checkout is not a slot


def test_get_with_a_branch(clone):
    r = tf.repo(clone)
    p1 = tf.get(r, "feat")  # new branch, at origin/HEAD
    assert sh("git", "branch", "--show-current", cwd=p1).strip() == "feat"
    with pytest.raises(tf.Error, match="already checked out"):
        tf.get(r, "feat")
    commit(p1, "f.txt")
    tf.release(r, p1)
    sh("git", "switch", "-q", "--detach", cwd=p1)
    tf.release(r, tf.get(r))  # p1 is reused (detached) and released again
    p2 = tf.get(r, "feat")  # existing branch: checked out as is, commit included
    assert (p2 / "f.txt").exists()
    with pytest.raises(tf.Error, match="not a valid branch name"):
        tf.get(r, "bad..name")


def test_get_fetches_first(clone, tmp_path):
    other = tmp_path / "other"
    sh("git", "clone", "-q", str(tmp_path / "origin.git"), str(other))
    commit(other, "new.txt")
    sh("git", "push", "-q", "origin", "main", cwd=other)
    assert (tf.get(tf.repo(clone)) / "new.txt").exists()


def test_half_done_operation_is_not_free(clone):
    r = tf.repo(clone)
    p1 = tf.get(r)
    sh("git", "bisect", "start", cwd=p1)
    tf.release(r, p1)
    assert slot_status(r, p1) == "dirty"
    assert tf.get(r) != p1


def test_gc_leaves_vanished_foreign_worktrees_alone(clone, tmp_path):
    r = tf.repo(clone)
    foreign = tmp_path / "usb-drive" / "wt"
    sh("git", "worktree", "add", "-q", "--detach", str(foreign), cwd=clone)
    sh("rm", "-rf", str(foreign))
    tf.get(r)
    tf.gc(r)
    assert foreign.resolve() in {wt.path.resolve() for wt in tf.worktrees(clone)}


def test_git_env_from_a_hook_does_not_fool_is_safe(clone, monkeypatch):
    r = tf.repo(clone)
    p1 = tf.get(r)
    (p1 / "work.txt").write_text("unsaved")
    monkeypatch.setenv("GIT_DIR", str(clone / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(clone))
    assert not tf.is_safe(p1)


def test_age_tolerates_odd_lock_reasons():
    assert tf.age("treeflow 2026-10-17") == "?"
    assert tf.age("treeflow") == "?"


def test_init_prints_a_shell_function(capsys):
    assert tf.main(["init"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("treeflow() {") and "command treeflow" in out


def test_get_branch_upstreams(clone, tmp_path):
    other = tmp_path / "other"
    sh("git", "clone", "-q", str(tmp_path / "origin.git"), str(other))
    sh("git", "switch", "-qc", "pr-42", cwd=other)
    commit(other, "pr.txt")
    sh("git", "push", "-q", "origin", "pr-42", cwd=other)

    r = tf.repo(clone)
    pr = tf.get(r, "pr-42")  # only on origin: tracked from there, with its commits
    assert (pr / "pr.txt").exists()
    assert sh("git", "rev-parse", "--abbrev-ref", "@{u}", cwd=pr).strip() == "origin/pr-42"

    feat = tf.get(r, "feat")  # brand new: no upstream, not origin/main
    res = subprocess.run(["git", "rev-parse", "@{u}"], cwd=feat, capture_output=True)
    assert res.returncode != 0
