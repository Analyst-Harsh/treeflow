# 🌳 treeflow

**A pool of named git worktrees: hand them out, track them, clean them up.**

Run five coding agents, a PR review and an experiment at the same time, each in its
own checkout, without ever touching your main working copy.

```
~/code/myrepo/              ← main checkout, left alone
~/code/myrepo.wt/pikachu/   ← a slot (agent 1)
~/code/myrepo.wt/eevee/     ← another slot (agent 2)
~/code/myrepo.wt/snorlax/   ← free, waiting to be reused
```

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![No dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![macOS | Linux](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)

---

## Why

`git worktree` is great for parallel work, but by hand it's a chore: think of a
path, pick a base, remember which ones are busy, and clean up the leftovers without
deleting someone's uncommitted changes. treeflow does that bookkeeping for you:

- **One command to claim a fresh checkout** at the latest `origin/main`, on a new or
  existing branch.
- **Slots get reused**, so you don't pay for a fresh checkout every time.
- **Your work is protected.** A slot with uncommitted changes, unpushed commits or a
  rebase in progress is never reused or deleted.
- **Pure stdlib Python**, no dependencies.

## 🤖 Running agents in parallel

Coding agents (Claude Code, Codex, Aider, Cursor CLI, ...) step on each other when
they share a checkout: one agent's half-finished edit breaks the other's test run,
and two of them can't be on different branches at once. Give each agent its own slot:

```bash
# Terminal 1
treeflow get -b feat/auth      # claims a slot, cd's into it
claude "implement OAuth login"

# Terminal 2
treeflow get -b fix/flaky-tests
claude "fix the flaky tests in tests/api"

# Terminal 3
treeflow get -b refactor/db
codex "move the db layer to async"
```

Each agent gets its own working tree, branch, index and build output. They all share
the same `.git` object store, so a new slot is cheap and commits made in one slot
are instantly visible to the others.

### Scripting a fan-out

`treeflow` (the CLI itself, not the shell function) prints just the slot path on
stdout and its messages on stderr, so you can use it in scripts:

```bash
for task in auth billing search; do
  dir=$(command treeflow get -b "feat/$task")
  (cd "$dir" && claude -p "implement $task, see TASKS.md" > "../$task.log" 2>&1) &
done
wait
treeflow status    # see what each slot is doing and what to run next
```

When an agent is done, push or merge its branch, then:

```bash
treeflow release pikachu   # back to the pool, branch kept
treeflow gc                # delete every free slot
```

> **Tip:** `treeflow status` shows every slot, whether it's claimed, how long ago,
> whether it's dirty, and the exact command to resume, release or remove it.

## Install

```bash
git clone <this-repo> ~/src/treeflow
uv tool install --editable ~/src/treeflow
echo 'eval "$(treeflow init)"' >> ~/.zshrc     # lets `get` and `cd` change your directory
```

The shell function only wraps `get` and `cd`; everything else goes straight to the CLI.
Works with zsh and bash.

## Usage

```bash
treeflow get                      # claim a slot at the latest origin/main and cd into it
treeflow get -b login-fix         # same, on branch login-fix (created if new, resumed if not)
treeflow get -b login-fix --code  # ...and open it in VS Code
treeflow get pikachu              # claim pikachu as you left it (created if it doesn't exist)
treeflow get pikachu -b fix       # claim pikachu and switch it to fix (only if it's clean)

treeflow cd pikachu               # jump into a slot; `treeflow cd` alone goes back to main
treeflow cd pikachu --code        # ...and open it in VS Code
treeflow cd --code                # open the worktree you're in (from any subfolder)

treeflow status                   # every worktree, its status and the commands for it
                                  # (* marks where you are; `list` works too)
treeflow release                  # give the current slot back (or `release pikachu`)
treeflow rm pikachu               # delete a slot; its branch is kept
treeflow gc                       # delete every free slot
```

Every command says what it did on stderr (stdout stays just the path):

```
🌱 created pikachu  on new branch login-fix from origin/main (a1b2c3d)
🔁 reused eevee  detached at a1b2c3d; `git switch -c <name>` before committing
🍃 released eevee  back to the pool
🪓 removed pikachu  branch login-fix kept
💻 opened pikachu in VS Code
🚧 fetch failed, starting from local refs: ...
❌ pikachu is already claimed; `treeflow release pikachu` it first
```

Emojis and colors only appear on a terminal; piped or with `NO_COLOR` set you get plain
`treeflow: ...` lines.

| Option | What it does |
| --- | --- |
| `get --base REF` | start from `REF` instead of `origin/HEAD` |
| `get --new` | always make a fresh slot instead of reusing a free one |
| `--code` | open the worktree in VS Code |

Slots can be referred to by name (`pikachu`) or by path.

**Exit codes:** `0` ok · `1` error · `2` bad usage · `3` refused, to protect your work.

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
  and `gc`. A fresh slot doesn't have them, so agents may need to run `npm install`
  or copy `.env` first.

## Develop

```bash
uv run pytest      # tests run against real temporary git repos
uv run ruff check
```
