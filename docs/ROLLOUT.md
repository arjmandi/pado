# Rollout plan — from pack to daemon

The sequence used for the first production rollout (2026-08-14): install the
core → dry run → central throwaway test → **equip all track repos together** →
throwaway test per route → daemonize. Each phase has a hard exit criterion; do not start the next phase
until it is met.

## Phase 1 — install the daemon core

```sh
mkdir -p ~/bin ~/.agentd ~/agents/logs
install -m 755 src/agentd.py ~/bin/agentd.py
cp config/config.example.toml ~/.agentd/config.toml   # then fill in your board + routes
```

Dry run twice:

```sh
python3 ~/bin/agentd.py --once -v    # 1st: primes every card + comment, launches nothing
python3 ~/bin/agentd.py --once -v    # 2nd: idle — no transitions, no new comments
```

**Exit:** both runs exit 0; run 1 logs the priming counts; run 2 launches nothing.

## Phase 2 — central throwaway test

1. Create a throwaway issue in your board repo (`[throwaway] agentd end-to-end
   test`) whose body says explicitly: reply only, modify no files.
2. Add it to the board in **In plan**, then comment an instruction on it.
3. `python3 ~/bin/agentd.py --once -v` → expect: session starts in
   the planning repo (e.g. `~/workspace/planning`), a summary comment appears with the footer,
   the card's `Agent status` turns `done`.
4. Comment a follow-up; run `--once` again → expect the **same** session
   resumes (same session id in the footer).

**Exit:** two runs, one session, two comments, correct `Agent status`.
Close the issue (that's the completion gesture) and archive the card.

## Phase 3 — equip all track repos together

For each worktree route's repo (in the example config: **web, docs, infra,
blog**), in its own clone under `~/workspace/`:

- `templates/CLAUDE.md` → `<repo>/CLAUDE.md`, placeholders filled by actually
  reading the repo, plus the track's known traps (wherever you keep them —
  e.g. a `handoffs/README.md` in the planning repo). The kind of trap worth
  writing down: an external tool, not this repo, is the source of truth for
  some data; the issues live in one repo and the code in another;
  `deploy.yml` has no paths filter, so any push to `main` redeploys
  everything.
- `templates/claude-settings.json` → `<repo>/.claude/settings.json` (merge,
  union deny arrays).
- `templates/agent-journal.md` → `<repo>/docs/agent-journal/README.md`.
- Commit on branch `agent/setup`, push, open a **draft PR**. Never touch the
  default branch.

**Exit:** one draft PR open per repo. The owner reviews and merges them —
**on a repo where a push to `main` deploys, merge only at a moment when a
full redeploy is acceptable** (no paths filter).
The guardrails must be on each repo's default branch before Phase 4, because
worktrees are cut from `origin/main`.

## Phase 4 — throwaway test per route

One throwaway issue per track, dragged into its `agent: <track>`
column. The body: a deliberately trivial task — "add a one-line file
`docs/agent-journal/0-test.md`, commit, report STATUS: DONE". Expect per
route: worktree at `~/agents/<route>/issue-N`, one commit, a draft PR, a
summary comment, `Agent status: done`.

Cleanup per route (the daemon never cleans up):

```sh
gh pr close <pr> --repo <code-repo> --delete-branch
git -C ~/workspace/<repo> worktree remove ~/agents/<route>/issue-N
```

Close the test issues, archive the cards.

**Exit:** every route produced a PR and a comment; every route cleaned up.

## Phase 5 — daemonize

1. Edit `config/com.local.agentd.plist`: every `CHANGEME` → your absolute
   home directory (`echo $HOME`); confirm the python path with
   `which python3` (Homebrew on Apple silicon: `/opt/homebrew/bin/python3`).
2. `cp` to `~/Library/LaunchAgents/`, then
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.agentd.plist`.
3. `scripts/verify.sh` → everything green including the service section.
4. Watch one live cycle: `tail -f ~/agents/logs/agentd.err.log`.

**Exit:** daemon polls on its own; a comment on a routed card triggers without
any manual run.

After any config edit: `launchctl kickstart -k gui/$(id -u)/com.local.agentd`.

## Adding projects later

See `ADDING-A-PROJECT.md` — the standing checklist for introducing a new
repo/track to this setup.
