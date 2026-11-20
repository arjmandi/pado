# Adding a project to the board-agent setup

The standing checklist for introducing a new track. Everything here is
additive — nothing existing changes. Order matters: the board column and the
repo guardrails must exist before the daemon learns the route, or `verify.sh`
fails and the first run works with an uninformed agent.

## 1. The repo

- A GitHub repo for the track (`owner/name`), and a local clone at
  `~/workspace/<name>` with `origin/<default-branch>` fetched.
- The three guardrail files, committed to the default branch:
  - `CLAUDE.md` — from `templates/CLAUDE.md`, **placeholders filled by reading
    the repo** (real commands, real layout, the track's known traps). This is
    what makes the agent informed rather than generic. Keep the "Read
    `.claude/FLEET-RULES.md` first" bullet from the template as-is — that
    file itself is never committed here; the daemon copies the current copy
    from `~/.agentd/FLEET-RULES.md` (installed by bootstrap from
    `templates/FLEET-RULES.md`) into every worktree on
    every run, so a fleet-wide rule reaches this track without a
    second commit.
  - `.claude/settings.json` — from `templates/claude-settings.json`; merge if
    one exists, union the `deny` arrays.
  - `docs/agent-journal/README.md` — from `templates/agent-journal.md`. (Not
    `docs/agent-journal.md` — that flat-file layout is frozen legacy; entries
    are per-issue files in the `docs/agent-journal/` directory.)

## 2. The board

- Add a `Status` option named `agent: <name>` — lowercase, **space after the
  colon**, exactly like the existing ones. Adding an option is safe; never
  rename existing ones.
- Nothing else: `Agent status` already exists board-wide, and completion is
  closing the issue, not a column.

## 3. Context for the agent

- Task context lives **in the track's own repo** (the agent cannot read
  the planning repo). A briefs folder (e.g. `docs/briefs/`) is the
  pattern.
- Cards carry the GitHub URL of their context file, so it's browsable from
  the board.

## 4. The daemon

- Append a `[[route]]` block to `~/.agentd/config.toml`:

  ```toml
  [[route]]
  name    = "<name>"                 # session namespace; don't change it later
  columns = ["agent: <name>"]
  workdir = "~/workspace/<name>"
  repo    = "<owner>/<name>"         # where the draft PRs go
  # base_branch = "main"             # only if the default branch differs
  ```

- Run `scripts/verify.sh` and expect it **green** — including the per-route
  guardrail check (`CLAUDE.md`, `.claude/settings.json` with a `git push`
  deny, `docs/agent-journal/README.md`, all present on `origin/<base>`), the
  workdir, and the column. It reads `config.toml` straight off disk, so this
  works even before the daemon is restarted. Fix anything red **before**
  restarting — a route that reaches the daemon unequipped fails loudly on
  its first card instead of silently running an uninformed agent. (This
  check exists because a route once went live without its guardrail files.)
- Restart the daemon: `launchctl kickstart -k gui/$(id -u)/com.local.agentd`
  (config is read once at startup).

Renaming a route's `name` later orphans its sessions (session IDs derive from
it) — pick it once. Moving in-flight cards between agent columns starts a
fresh session on the new route by design.

## 5. Throwaway test

Create a throwaway issue in your board repo, drag it into the new column, and
expect: worktree at `~/agents/<name>/issue-N`, a trivial commit, a draft PR, a
summary comment, `Agent status: done`. Then close the PR unmerged, remove the
worktree, close the issue.

## 6. Planning-repo bookkeeping

Whatever your planning repo tracks per project, add the new track there too.
For example, a planning repo might hold a portfolio overview (`PORTFOLIO.md`)
with a row, materials and hard dates per track, and decisions in
`decisions/`. Two entries matter to the central agent specifically:

- `CLAUDE.md` → add the new `agent: <name>` column to the board section, so
  the central agent can route cards to it.
- a briefs index (e.g. `handoffs/README.md`) → a row pointing at the track's
  briefs folder on GitHub.
