<!--
  TEMPLATE — installing agent: fill this in by reading the repo, then delete
  this comment. Every {{placeholder}} must be replaced with something real.
  A CLAUDE.md full of TODOs is worse than no CLAUDE.md, because the agent will
  trust it. If you cannot determine something, ask the human rather than guess.

  Keep this file short. It is loaded on every single run, including every
  follow-up comment turn, so length here is a recurring cost. Long-lived detail
  belongs in docs/agent-journal/ instead.
-->

# {{project name}}

{{One or two sentences: what this project is and who uses it.}}

## Commands

```sh
{{install}}        # e.g. pnpm install
{{dev}}            # e.g. pnpm dev
{{test}}           # e.g. pnpm test
{{test-single}}    # how to run ONE test file — agents need this constantly
{{lint}}
{{typecheck}}
{{build}}
```

Run `{{test}}` and `{{lint}}` before considering any task done. If either fails
for reasons unrelated to your change, say so in your summary rather than fixing
unrelated breakage.

## Layout

```
{{tree of the directories that matter, 1-2 levels, with a note on each}}
```

## Conventions

- {{language/framework version and any strictness settings}}
- {{formatting: is it enforced by a tool? which?}}
- {{testing style: framework, where tests live, naming pattern}}
- {{error handling / logging pattern used here}}
- {{commit message format, if any}}

## Constraints that are not obvious from the code

<!-- This is the most valuable section. Things that have bitten a human before. -->

- {{e.g. the migration runner is not idempotent; never re-run one}}
- {{e.g. src/legacy/ is frozen; route changes through the adapter in src/adapters/}}
- {{e.g. integration tests need a local Postgres on 5433, not 5432}}

## Working under agentd

You may be running unattended, triggered by a board move or an issue comment.

- **Read `.claude/FLEET-RULES.md` first.** It carries rules that apply to
  every route on every track — never chain a probe command in front of the
  one you actually need (a denied probe denies the whole compound command),
  scratch files go in `.scratch/` inside the worktree and never `/tmp`, and
  the browser (where equipped) is read-and-verify only in headless runs. The
  daemon refreshes this file from its own install on every run, so it is always current
  — you don't need to check anywhere else for it.
- You are in a **git worktree** on branch `agent/issue-N`. Stay in it. Do not
  check out other branches; other agents may be working in sibling worktrees.
- **Commit** your work in logical units, referencing the issue number.
- **Do not push and do not open a PR.** The supervising daemon does both. Your
  `git push` is denied by `.claude/settings.json` on purpose.
- **Journal entries are per-issue files.** If — and only if — this run taught
  you something a future agent would otherwise rediscover the hard way, write
  it as a NEW file `docs/agent-journal/<issue>-<short-slug>.md`. Never append
  to a shared journal file: concurrent PRs all touching one file conflict by
  construction (the legacy `docs/agent-journal.md` is frozen — don't edit it).
  Most runs need no entry at all; routine work is already in git history.
- End your final message with a 3–6 line summary suitable for a GitHub comment,
  then either `STATUS: DONE` or `STATUS: BLOCKED` with the reason. The daemon
  parses these tokens to set the board status, so the spelling matters.
- **Don't close the issue or merge a PR on your own judgment.** `STATUS: DONE`
  means "ready for review", not "task over"; the human reviews, merges, and
  closes. The one exception: an explicit instruction in the issue thread
  ("close it", "merge it") is the human's decision already made — execute it
  (`gh issue close` / `gh pr merge`, both permitted here). This includes the
  BOARD issue (`{{board repo, e.g. you/board}}#N`) — closing it on
  instruction is yours to do, not reserved to the daemon. If a needed command is denied in this repo,
  say so in your summary instead of silently skipping the instruction.
- **Board writes.** You may comment on the `{{board repo}}` board, but every
  comment must START with the literal marker `<!-- agentd -->` — an unmarked
  comment is treated as an instruction from the human and re-triggers an agent
  (yourself included). The daemon posts your headless final summary
  automatically; don't duplicate it.
- **Settling after an attended session:** before it ends, update the board
  yourself — post a marked wrap-up comment on the issue and set the card's
  `Agent status` with `gh project item-edit` (project `{{project node id}}`,
  field `{{Agent status field id}}`, options: working `{{option id}}` · done
  `{{option id}}` · blocked `{{option id}}` — look them up once with
  `gh project view` and `gh project field-list` (`--format json`);
  find the item id via
  `gh project item-list {{project number}} --owner {{owner}} --format json --limit 1000`
  filtered by issue number). In headless runs never touch that field — the daemon owns it.
- **Boards other people read (e.g. `{{a shared team tracker}}`) are different:**
  post there ONLY when the human's instruction in this task explicitly says to
  post. Otherwise deliver the comment text as a paste-ready draft in your
  summary.
- **Every comment you post on ANY board must be linked by URL in your final
  summary.** The audit trail is non-negotiable.
- **Anything browser-bound (portal uploads, web round-trips, logged-in sites)
  cannot happen in a headless run** — there is no browser bridge here. Take
  the task as far as files allow (paste-ready drafts, exact steps written
  out), then end with `STATUS: BLOCKED` saying the task "needs the attended
  browser lane"; the human resumes this very session with a browser
  (`agentd.py --session <route> <issue>` prints the command). If this route
  is later granted the headless browser, the read-vs-post rule in
  `.claude/FLEET-RULES.md` still applies once equipped.
- If the task is underspecified, prefer `STATUS: BLOCKED` with a specific
  question over guessing. A blocked card costs a human one comment; a wrong
  implementation costs a review cycle.

- {{Optional — delete if you have no writing-voice skill.}} **Anything
  written in the human's name uses the `{{your voice skill}}` skill** —
  emails, posts, form answers, chat messages, outreach drafts, replies they
  will send. Invoke the skill before drafting; if it is unavailable in your
  session, say so in your summary instead of imitating their style from
  guesswork.
