# Rules matrix — where every agent rule lives, and how to change one

Reference for future rule changes. The fleet's behavior is spread across
**several surfaces** (seven in the example setup below: four track repos,
the planner, the runtime prompts, the templates); a rule that isn't on all of
its surfaces silently diverges. Keep a "last verified" date here and bump it
each time you check every repo's `origin/main`: **<date>**.

## The surfaces

| # | Surface | Path | Loaded when |
|---|---|---|---|
| 1 | web agent | `you/web-app` → `CLAUDE.md` + `.claude/settings.json` | every run, from the worktree (cut from `origin/main`) — **pushes to `main` need `[skip ci]` or the deploy fires** |
| 2 | docs agent | `you/docs` → same files | same |
| 3 | infra agent | `you/infra` → same files | same |
| 4 | blog agent | `you/blog` → same files | same — personal scope; **hosts a LIVE site — deploy scripts denied** |
| 5 | central planner | the planning repo's `CLAUDE.md` + `.claude/settings.json` | every run, in place |
| 6 | runtime prompts | `src/agentd.py` (`WORKTREE_PROMPT`, `CENTRAL_PROMPT`, …) | baked into each run; reinstall to `~/bin/agentd.py` + `launchctl kickstart -k gui/$(id -u)/com.local.agentd` |
| 7 | templates | `templates/CLAUDE.md` + `claude-settings.json` (+ `FLEET-RULES.md`, copied into every worktree on every run) | equipping future repos (`ADDING-A-PROJECT.md`) |

## The rules and where each must exist

| Rule | 1–4 repos | 5 central | 6 prompts | 7 template |
|---|---|---|---|---|
| STATUS: DONE/BLOCKED contract; summary posted by daemon | ✓ | ✓ | ✓ | ✓ |
| Done = review-me; closed = task over; close/merge only on explicit instruction (web: merge always the owner's — merge = deploy; blog: merge-on-instruction WITH stated deploy consequence + Actions verification) | ✓ | ✓ | — | ✓ |
| Board writes: every agent comment starts `<!-- agentd -->` | ✓ | ✓ | ✓ | ✓ |
| Unmarked comment = instruction (central's deliberate hand-off only) | — | ✓ | ✓ | — |
| Boards other people read (a shared team tracker, …): post only on explicit instruction | ✓ | ✓ | ✓ | ✓ |
| Every posted comment linked by URL in the final summary | ✓ | ✓ | ✓ | ✓ |
| Attended browser lane: file-side prep → `STATUS: BLOCKED` asking for it | ✓ | ✓ | — | ✓ |
| Attended self-settle: marked wrap-up + `Agent status` via `item-edit` | ✓ | — | — | ✓ |
| `Agent status` daemon-owned in headless runs | ✓ | ✓ | — | ✓ |
| Worktree discipline: own branch, commit, never push (daemon publishes) | ✓ | n/a | ✓ | ✓ |
| Central: triage rules, drafts to a drafts file (e.g. `MESSAGE-DRAFTS.md`), context in project repos, commit-don't-push | n/a | ✓ | ✓ | n/a |
| Permissions: `gh issue view/comment/close`, `gh project item-list/item-edit`; deny push/merge/secrets | ✓ | ✓ (+create/edit/search) | n/a | ✓ |
| WebFetch/WebSearch (research, no browser): web, docs, blog + template | those 3 | — | — | ✓ |
| Headless browser (Chrome bridge): central, web, blog — read/verify only; send-shaped actions blocked by the classifier (layer 5, see below), not by route config | per route | ✓ | — | — |
| Optional: a writing-voice skill mandatory for anything written in the owner's name (skill lives machine-level in `~/.claude/skills/`) | ✓ | ✓ | — | ✓ |
| Repo-specific extras (not fleet rules): docs — `python3` for xlsx via openpyxl; infra — ssh/scp denies | per repo | — | — | — |

Board constants (in repo CLAUDE.md files, needed for `item-edit`): the
project node id, the Status field id, the `Agent status` field id and its
three option ids (working · done · blocked). Look them up once with
`gh project view <number> --owner <OWNER> --format json` and
`gh project field-list <number> --owner <OWNER> --format json`.

## Permission enforcement layers

What a headless run can actually do is gated by **five** independent layers,
not the three (route config / settings allow / settings deny) it looks like
from the surfaces table above:

1. **Route config** (`~/.agentd/config.toml`) — whether the route has a
   capability at all (browser flag, WebFetch/WebSearch, `max_concurrent_agents`).
2. **`.claude/settings.json` allow list** — the Bash/tool patterns a route may
   invoke without a prompt.
3. **`.claude/settings.json` deny list** — patterns denied outright (push,
   merge, secrets, `gh auth`, …), same file.
4. **`permissionMode`** — the daemon always passes `--permission-mode
   acceptEdits` on the CLI, which wins over any `defaultMode` in
   `~/.claude/settings.json`; that global setting does not leak into headless
   runs (verified against every run log in `~/agents/logs`).
5. **Claude Code's own auto-mode classifier** — fires *inside* `acceptEdits`,
   independent of layers 1–4; no config controls it. It denies send-shaped
   actions in unattended runs — typing or submitting a message into a human
   channel (Slack, Discord, email, X, a form addressed to a person) — while
   still allowing read-only use of the same channel (navigate, screenshot,
   read). Surfaces as: "Permission for this action was denied by the Claude
   Code auto mode classifier… you should not attempt to work around this
   denial." First observed blocking Slack/Discord posts from a
   headless run. A second instance: typing replacement content into GitHub's own web file-editor, mid-commit
   to a real branch, was blocked the same way — the classifier treats
   "content headed for a commit on a shared repo" as send-shaped too, not
   just messages to a human channel. The only correct response is `STATUS:
   BLOCKED` with paste-ready text — never a workaround, and never routing a
   send-shaped deliverable to a headless run in the first place (make
   that a triage rule in the planning repo's `CLAUDE.md`).

## Writing to a checkout-forbidden branch without a worktree switch

A repo's `CLAUDE.md` can forbid checking out a branch (e.g. a `docs/plans`
branch on the web repo — agents keep planning docs there instead of `main`)
without forbidding *writing* to it. Two independent runs read "don't check
out `docs/plans`" as "can't land a fix there at all" and stopped instead of
finding the actual gap: `git push` isn't on any repo's settings-allow list
(layer 2), so it always needs an approval prompt — which a headless run has
no human to answer. That's an ordinary permission-layer gate, not a policy
rule, and it clears in an **attended** session (`agentd.py attend <issue>`),
where the owner is present to approve the push. No worktree switch needed —
plumbing writes a single blob straight into the target branch:

```bash
git show origin/docs/plans:<path>              # read the current file
# edit a copy outside the repo tree
git hash-object -w <copy>                      # blob
git read-tree <base> && git update-index --cacheinfo 100644,<blob>,<path>
git commit-tree $(git write-tree) -p <base> -m "..."
git push origin "${C}:refs/heads/docs/plans"   # braces required — zsh eats ":r"
```

Operational notes: a push to `docs/plans` triggers no CI on the web repo
(`deploy.yml` only fires on `main`), so this can't deploy by accident.
The browser-edit route (previous section) hits the classifier instead and is
not a substitute for this — it's a genuinely different, harder-blocked path.

## How to change a rule everywhere

1. **Template first** (`templates/`), so future repos inherit it. A rule
   that belongs in `templates/FLEET-RULES.md` stops here — edit the live copy
   at `~/.agentd/FLEET-RULES.md` too (or re-run bootstrap); the daemon copies
   it into every worktree on every run.
2. **Central** (the planning repo's `CLAUDE.md`, `.claude/settings.json`) if
   it applies to the planner.
3. **Runtime prompts** (`src/agentd.py`) if the rule must hold even
   when a repo's CLAUDE.md is stale — then reinstall + kickstart (surface 6).
4. **Roll to the track repos**: per repo, temp worktree from `origin/main`,
   apply the same edit, commit, `git push origin HEAD:main`.
   **On a repo where a push to `main` deploys, the commit message must start
   with `[skip ci]`.** Never work in the owner's checkout directly — several are dirty or on
   feature branches.
5. **Verify, don't trust the roll**: read each repo's `origin/main` files and
   check the rule text is present — **normalize whitespace before matching**
   (bullets wrap across lines; a literal phrase-grep gives false negatives).
6. Update this matrix's "last verified" date.

Changes take effect on each agent's **next headless run** — no re-equipping
needed: new cards get fresh worktrees from `origin/main`, and existing
worktrees sync `CLAUDE.md` + `.claude/settings.json` from origin at every run
start (the daemon commits the sync on the agent branch). Only surface 6 needs
a daemon restart. One residual gap: an **attended** session resumed without an
intervening headless run still sees the worktree's current files — if the
agent cites an outdated rule there, tell it to re-read CLAUDE.md after a
`git fetch && git checkout origin/main -- CLAUDE.md`.
