# START HERE — brief for the installing agent

You are being handed a complete design and implementation for **agentd**, a local
daemon that lets a human drive Claude Code agents from a GitHub Projects board.
Your job in this session is to **install and verify it**, not to redesign it.

Read `docs/DESIGN.md` before you touch anything — it explains why several
non-obvious choices are the way they are. If you find yourself wanting to change
one of them, check the "Rejected alternatives" section first; the reasoning is
probably already there.

---

## What you are building

A polling daemon on this machine. The board's columns route to different agents:

- A comment on a card in **`In plan`** summons the
  **central planning agent**, which runs in the planning repo itself
  (e.g. `~/workspace/planning`), triages per that repo's `CLAUDE.md`, drafts
  replies, and moves cards. No worktree, no PR.
- Dragging a card into an **`agent: <track>`** column — or commenting on one
  already there — starts that track's agent in its own repo: git worktree,
  branch `agent/issue-N`, draft PR, outcome posted back on the issue.

Comments on an issue become follow-up turns in the same session; the first
comment on an untouched card starts one. Nothing runs when the laptop is off.
That is a requirement, not a limitation.

---

## Your tasks, in order

### 1. Gather what only the human can tell you

`config/config.example.toml` carries placeholder values: a board repo
`you/board`, user project #1, and example routes (`central`, `web`, `docs`,
`infra`, `blog`). None of them are the human's. Do not guess. Ask, in one
message, for:

- the board: the repo its issues live in, the login that owns the Project,
  the project number, and whether the owner is a user or an organization
- the board's `agent: *` columns, one route per column

- confirmation of each route's `workdir` — the absolute path to the local clone
  of that track's repo — and which routes to enable now (routes whose clone
  does not exist yet should be commented out, not guessed at)
- each worktree route's `repo` slug and default branch
- whether `blocked_status` should park blocked cards in the board's existing
  `Blocked` column, or stay disabled (the default; see the config comment)
- whether the human has created the run-state field for `agent_status_field`
  (the example config expects a single-select field named **`Agent status`**
  with options spelled exactly `working`, `done`, `blocked`). Creating a *new*
  field is the one board change that is allowed — it disturbs no existing view.
  If the human doesn't want it, set `agent_status_field = ""` instead.

### 2. Preflight

Verify and report, do not silently fix:

```sh
claude --version
gh --version && gh auth status
git --version
python3 --version          # needs >= 3.11 for tomllib — NOT /usr/bin/python3 (3.9)
```

Then check the Projects scope specifically, because this is the most common
failure and the error it produces later is opaque:

```sh
gh auth status 2>&1 | grep -o "project"
```

If `project` is absent, tell the human to run
`gh auth refresh -s project,read:project` themselves. Do not run auth commands
on their behalf.

### 3. Read the board and confirm the routed columns

Query the project and list its `Status` single-select options. Every column
named in a `[[route]]` must exist on the board, spelled exactly — including the
space in `agent: web`. The board needs **no new options**: the daemon routes on
the columns the human already uses and never moves cards as a lease.

If a routed column is missing, print the exact mismatch and stop. **Do not
create or rename Status options** — that is the human's board and renaming one
silently breaks their existing views and automations. The only permitted
addition is the new `Agent status` field from step 1, if the human opted in
and asks you to create it.

### 4. Install

```sh
mkdir -p ~/bin ~/.agentd ~/agents/logs
install -m 755 src/agentd.py ~/bin/agentd.py
cp config/config.example.toml ~/.agentd/config.toml
```

Now edit `~/.agentd/config.toml` with the answers from step 1. Leave
`reconcile_on_start = "note"` and `max_concurrent_agents = 2` alone unless the
human asks otherwise.

### 5. Install the per-repo guardrails

For **each worktree route's repo** (web, docs, …):

- `templates/claude-settings.json` → `<repo>/.claude/settings.json`
  Merge with any existing file rather than overwriting. If a `permissions.deny`
  array already exists, union the entries. **Fill in the allow-list
  placeholders with the repo's real test/lint/build commands** — headless runs
  auto-deny any Bash command not allowed, so a missing allow rule means the
  agent cannot run the tests it is told to run. Rule syntax is `Bash(cmd:*)` —
  the colon before `*` matters.
- `templates/CLAUDE.md` → `<repo>/CLAUDE.md`
  This is a **template with placeholders**. Fill it in by actually reading the
  repo: real build/test/lint commands, the real directory layout, the real
  conventions you can infer from the code. A CLAUDE.md full of `TODO` is worse
  than none.
- `templates/agent-journal.md` → `<repo>/docs/agent-journal/README.md`
  (entries are per-issue files in that directory, so concurrent PRs never
  conflict on a shared journal)

Commit these to each repo on a normal branch and let the human review. Do not
push to default branches directly.

The **planning repo** (the central route's `workdir`) needs its own
`CLAUDE.md` carrying the triage rules the central agent follows, and a
`.claude/settings.json` that allows the `gh issue` / `gh project` commands the
agent needs while running headless. If both exist, verify them and do not
overwrite them. If they are missing, ask the human — the triage rules are
theirs to write.

### 6. Dry run

```sh
python3 ~/bin/agentd.py --once -v
```

Expected: it connects, pages through the full board, reports the routes it
resolved, records every card's current column, marks existing comments as
seen, and exits 0 **without launching any agent** — the first poll is a
priming pass by design.

Run it a second time after commenting on a throwaway card in a routed column.
*Now* it should trigger. Have the human create a throwaway issue to test with —
do not test against real work.

### 7. Daemonise

macOS: `config/com.local.agentd.plist` → `~/Library/LaunchAgents/`, replacing
every `CHANGEME` with the real home directory, then bootstrap it. The plist
points at Homebrew's python3 — confirm the path with `which python3`.

Linux: `config/agentd.service` → `~/.config/systemd/user/`, then
`systemctl --user enable --now agentd`.

Both files carry their own load/unload commands in comments. Note the warning in
the systemd unit about `enable-linger` — enabling it would defeat the entire
laptop-off requirement.

### 8. Verify and hand back

Run `scripts/verify.sh` and report its output verbatim. Then write the human a
short summary: what you installed and where, what you had to change from the
defaults and why, and anything from steps 2–3 they still need to do themselves.

---

## Constraints on you during this session

- **Ask before anything destructive.** Do not delete worktrees, force-push,
  merge PRs, or modify board fields.
- **Do not run `gh auth` commands.** Report what is missing; let the human
  authenticate.
- **Do not weaken the security posture.** `--permission-mode acceptEdits` in
  `agentd.py` is deliberate. Do not substitute
  `--dangerously-skip-permissions` to make a test pass.
- **If a preflight check fails, stop and report.** A half-installed daemon that
  polls a misconfigured board will post confusing comments on real issues.
- If you change `src/agentd.py`, say so explicitly and explain why. Its config
  loading, routing, session-ID derivation, and poll state machine are
  smoke-tested and have run against a live board since 2026-08-14.
