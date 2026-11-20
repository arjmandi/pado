<!--
  Single source for rules that apply to every route. bootstrap.sh installs
  this file to ~/.agentd/FLEET-RULES.md, and the daemon copies it into every
  worktree as .claude/FLEET-RULES.md on every run (_sync_fleet_rules in
  agentd.py). A rule added there reaches every route on its next run, with
  no per-repo commit. Rules only, never task context: this is the one file
  the daemon injects itself, so keep it that way.

  Each track's own CLAUDE.md references this file with one line (added once,
  during onboarding; see docs/ADDING-A-PROJECT.md).
-->

# Fleet-wide rules

## Never chain a probe in front of the command you need

`Bash(gh auth:*)` is denied in every repo, correctly — `gh auth` can print
tokens and change auth state. But a probe chained in front of the real
command denies the *whole* compound command, so the run never gets to the
part it actually needed:

```
gh auth status 2>&1; echo "---"; gh issue view 42 --repo OWNER/board --comments
```

Run the command you need on its own line. If it fails, read the actual
error instead of pre-flighting it with a diagnostic.

## Scratch files go in `.scratch/`, never `/tmp`

Writes inside the worktree are auto-accepted under `acceptEdits`; `/tmp` is
outside the worktree and is not, so a scratch file written there gets denied
and the run stalls. Put scratch files in `.scratch/` inside your worktree
instead. If `.scratch/` isn't already in this repo's `.gitignore`, add it
before you write into it, so scratch files never reach a PR.

## The browser is read-and-verify only in headless runs

Navigate, read, screenshot, and verify — never post or send. Typing or
submitting a message into a human channel (Slack, Discord, email, X, a form
addressed to a person) is denied outright in unattended runs by Claude
Code's own auto-mode classifier — a permission layer independent of
`permissionMode` or `.claude/settings.json` that fires even under
`acceptEdits`. Don't attempt to work around it: if a step needs this, stop
with `STATUS: BLOCKED`, put any paste-ready text in your summary, and name
exactly what you saw — the human sends it, or the attended lane covers the
rest.
