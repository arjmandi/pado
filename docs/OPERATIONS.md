# agentd

Drive local Claude Code agents from a GitHub Projects board. The columns route:
planning columns to the central agent in the planning repo, `agent: <track>`
columns to that track's own repo. One durable session per issue per route,
nothing running when your laptop is off.

```
  your Projects board                      your laptop
  ┌─────────────────────┐                  ┌──────────────────────────────┐
  │                     │   poll           │ agentd (launchd LaunchAgent) │
  │ In plan ─── central │─────────────────▶│   ├─ worker 1                │
  │                     │                  │   └─ worker 2                │
  │ agent: web  ─┐      │                  └──────┬───────────────────────┘
  │ agent: docs  ├ per- │                         │ spawn per issue+route
  │ agent: ...  ─┘ track│                  ┌──────▼───────────────────────┐
  └─────────────────────┘                  │ claude -p --session-id uuid5 │
        ▲       │                          │ central: cwd planning repo   │
        │       │ your comment             │ track: worktree agent/issue-N│
        └── agent's comment ───────────────┴──────────────────────────────┘
```

## Setup

1. **Board.** Nothing to add: the daemon routes on the `Status` columns you
   already use (`In plan`, `agent: web`, …). The
   `[[route]]` blocks in the config must spell them exactly — mind the space in
   `agent: web`. Unrouted columns (`archive`, `personal`, `Blocked`,
   `No priority yet`, `Next 3 months`) are invisible to the daemon.

2. **gh scopes.** Projects v2 is org/user-scoped, so the default token isn't enough:
   ```sh
   gh auth refresh -s project,read:project
   ```

3. **Install.**
   ```sh
   mkdir -p ~/bin ~/.agentd ~/agents/logs
   install -m 755 src/agentd.py ~/bin/agentd.py
   cp config/config.example.toml ~/.agentd/config.toml   # then edit it
   python3 ~/bin/agentd.py --once -v              # first run primes, launches nothing
   ```

4. **Daemonise.** Edit the plist (replace every `CHANGEME`; confirm the python3
   path — it must be 3.11+, not Apple's /usr/bin/python3), then:
   ```sh
   cp config/com.local.agentd.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.agentd.plist
   ```
   On Linux, use a systemd **user** unit with `WantedBy=default.target` — do *not*
   enable `loginctl enable-linger`, or it will survive your logout.

## Daily use

| You do | Daemon does |
| --- | --- |
| Comment on a card in **In plan** | Central agent wakes in the planning repo, triages per `CLAUDE.md`, drafts/moves cards, replies on the issue |
| Drag a card into **agent: web** (etc.) | That track's agent starts in its own repo: worktree, commits, draft PR, summary comment |
| Comment on a routed card again | Resumes *that issue's* session with your comment as the next turn |
| Comment while its agent is mid-run | Deferred, not lost — picked up right after the run finishes |
| Move a card into or out of **In plan** | Nothing — planning moves never launch agents |
| Review the result and **close the issue** | The card vanishes from the daemon's world; `--tidy` archives it later |
| Comment **"close it"** on a routed card | The agent closes the issue for you — explicit instruction is your decision already made; agents never close on their own judgment |
| Merge the PR | Nothing — see cleanup below |

**Agents writing to boards** — allowed, with three rules baked into every
agent's instructions: (1) any comment an agent posts starts with
`<!-- agentd -->`, so the daemon never mistakes it for one of yours (an
unmarked comment is reserved for you — and for the central agent's deliberate
hand-offs, since unmarked = wake-up); (2) boards other people read (e.g. a
shared team tracker) get posts only when your instruction in the task
explicitly says to post — otherwise the agent delivers a paste-ready draft;
(3) every posted comment's URL must appear in the agent's final summary — the
audit trail. Attended sessions settle their own card before ending: marked
wrap-up comment + `Agent status` set directly.

**Run caps** — a run that hits `claude_max_turns` or `claude_max_budget_usd`
ends with a failure comment naming the cap and the spend. The session
survives: comment on the issue to continue with a fresh run budget, split the
task into smaller cards, or retire a session whose transcript has grown so
large that every turn is expensive.

**Choosing model and effort per task** — write a directive line anywhere in
the card's body (default for every run of that card) or in a triggering
comment (override for that run; comment beats body):

```
pado: model=opus effort=high
```

`model`: `sonnet` (fleet default), `opus`, `haiku`, or a full `claude-*` id.
`effort`: `low` · `medium` · `high` · `xhigh` · `max` (omit for the model's
default). Invalid values are ignored with a daemon-log warning — the run
proceeds on defaults rather than failing. Directives apply to headless runs;
internal runs (settle, reconcile) always use the defaults.

**Who reads the directive — and why it works on every column.** The directive
is not addressed to any agent: it is parsed by the **daemon** while preparing
the launch, and passed as `--model`/`--effort` flags to whichever route's
agent owns that column. The agent never interprets the line — it simply *is*
that model at that effort from its first token. So the same directive works
identically on an `In plan` card (central agent) and an `agent: web` card (web
agent): **the column decides who runs, the directive decides on what engine.**
The `pado:` prefix names the system you're instructing, not a recipient.

Two practical rules:

- **Put the directive inside your instruction comment**, not as a separate
  comment — every unmarked comment on a routed card is a trigger, so a
  directive-only comment wakes the agent with no task text (it still runs and
  bills). Good form:
  `Draft the release notes for v2.1 — pado: model=opus effort=high`
- **Mid-conversation switches work.** A resume with a new model keeps all
  session memory, so "try again — pado: model=opus effort=max" genuinely
  escalates the *same* conversation. The footer confirms what actually ran —
  it reports the run's real usage, not the request.

**Monitoring which model/effort ran**: every run's footer on the issue shows
it — `web · comment · claude-opus-5 · effort high · 12 turns · …` — with the
model taken from the run's actual usage report, not the request. The run
ledger records both per run; per-issue history:
`sqlite3 ~/.agentd/state.db "SELECT kind, model, effort, turns, cost FROM runs WHERE number=NNN ORDER BY id"`.

**Cost & time accounting — what the numbers mean.** Every headless run's
footer (and the `runs` ledger in `state.db`) records:

- **Cost** = the Claude CLI's `total_cost_usd` for that run: actual token
  counts (input, output, cache writes, cache reads — each at its own rate)
  priced at current API list prices for the models used. It is computed by
  the CLI at run end, never estimated by the daemon. **If the machine
  authenticates via a Claude subscription seat (no API key), this is an
  API-equivalent value — consumption against the plan, not a per-run
  invoice.** If an `ANTHROPIC_API_KEY` were ever configured, the same number
  would become real billed dollars.
- **Time** = daemon-measured wall clock from launch to exit of the run.
- Per-issue totals = the sum of that issue's runs in the window. Long-lived
  sessions cost more per run (each run re-reads the transcript as cached
  input) — a fat session showing high cost per turn is the signal to retire
  it.
- **Not counted:** attended sessions (interactive, unsupervised by the
  daemon) and your own interactive planning sessions. The weekly report states this.

**Weekly report** — every Monday 09:00 (`com.local.agentd.weekly` LaunchAgent;
late-on-wake if the laptop is closed) `~/bin/weekly-report.py` writes
`reports/weekly/<Y>-W<NN>.md`: issues closed in the Mon–Sun window (sourced
from the repo, so archiving can't lose them), plus the live board — in
progress, awaiting review, blocked, and the queue grouped per column. It
commits the report (path-scoped) and pushes — note that push carries along any
other unpushed local commits on main. Rebuild any week by hand:
`python3 ~/bin/weekly-report.py --date <any-day-in-that-week>`.

**Decluttering** — `python3 ~/bin/agentd.py --tidy` archives every card that is
fully settled: issue **closed** *and* `Agent status: done`. Only-closed cards
(never reviewed as done) and only-done cards (still open — your review queue)
stay visible. Archiving is a project-level hide, reversible from the project's
"Archived items" page; run it whenever the board feels noisy. A view filter
cannot express this (project filters can't OR across qualifiers), which is why
it's a command.

**Annotating without waking the agent** — every comment on a routed card
triggers a run, whatever it says; there is no "wait" keyword. If a card isn't
ready for its agent, that's an ownership statement: drag it to an unrouted
column (`Blocked`, `No priority yet`, a planning column), comment freely
there, and drag it back when it's specified — the move re-triggers and the
agent reads the whole thread. Escape hatch for a quick note in place: a
comment containing `<!-- agentd -->` anywhere is invisible to the daemon
(that's its own-comment marker; the rest of the text renders normally).

**`done` vs `closed`** — the states are deliberately different. `Agent status:
done` is the *agent's claim*: "my last run finished, result awaits your
review" — the issue is still open, the card is still live, and a follow-up
comment flips it back to `working`. *Closed* is *your verdict*: the task is
over and the daemon forgets the card. Rule of thumb: **done = review me,
closed = it's over.** A swimlane grouped by `Agent status` makes the `done`
lane your review queue.

If `agent_status_field` is configured, the card face also shows run-state —
`working` while an agent runs, `done` when it finished and awaits your review,
`blocked` when it needs you. The daemon writes only that field; your columns
stay yours: a column says *whose* card it is, the field says *how the run went*,
and closing the issue says *it's over*.

## How you find out what's happening

With `bot_login` set, the daemon's comments are authored by a separate bot
account (e.g. `your-bot`), so GitHub notifies you about them — without it,
they are your own comments and GitHub stays silent, leaving the desktop
banners below as the only push channel. Delivery tiers, observed in practice:
**direct @mentions push a phone banner; other subscribed comments only land
in the GitHub app inbox (badge, no buzz)**. The daemon therefore @mentions
you on every action-needed comment — BLOCKED, failed, crashed, refused,
interrupted — and leaves routine "done" reports quiet in the inbox. Keep the
Watching subscription email-free: GitHub's email tracking pixel + Gmail's
prefetch marks notifications read before they can push. The signals, per stage:

| Stage | Signal |
| --- | --- |
| Comment received | 👀 reaction lands on your comment (within one poll interval) + banner "picked up" |
| Card-move received | Banner "picked up (card moved)" |
| Working | `Agent status: working` on the card face; per-run log in `~/agents/logs/` |
| Finished | Banner "done" with turns/cost (sound) + summary comment + `Agent status: done` |
| Needs you | Banner "BLOCKED" (sound) + comment with the question + `Agent status: blocked` |
| Run failed / agent crashed | Banner "FAILED"/"CRASHED" (sound) + error comment + `Agent status: blocked` |
| Run-limit breaker | Banner "REFUSED" (sound) + comment |
| Laptop slept mid-run | On next daemon start: banner "interrupted" (sound) + comment saying how to resume |

Banners are `notify = "macos"` in the config (Notification Center, sender
"Script Editor"/osascript — allow it in System Settings → Notifications if
banners don't appear). A comment posted while that issue's agent is mid-run is
**deferred, not dropped**: it gets its own 👀 + "picked up" when the run ends.
No 👀 within two poll intervals means the daemon didn't take it — check the
card is in a routed column and the daemon is alive (`launchctl list | grep
agentd`).

Logs: `~/agents/logs/<route>-issue-N.jsonl` is the full stream-json transcript of
every run on that issue. `jq -r 'select(.type=="assistant")' web-issue-42.jsonl`
to read along.

**Fleet overview** — `python3 ~/bin/agentd.py --status` prints, without touching
the board: whether the daemon is alive, how many `claude -p` processes exist,
which issues are running right now (with route, start time, workdir), and the
last ten sessions. It also flags a held lease with no live process — the
signature of a run that died mid-flight.

**Stopping a run** — `python3 ~/bin/agentd.py --stop <issue>`.
Covers both states a card can be in that `--status` itself can't fully tell
apart: merely **queued** (accepted by a poll, sitting in the daemon's
in-memory queue, no process yet — print_status's own note says this looks
identical to "not active at all" from outside) and already **running** (a
live `claude -p` process). One command handles either: it always drops a
15-minute file marker that `execute()` checks before doing any work — catching
the queued case — and it always looks up whether the issue is `running = 1` in
`state.db`, in which case it derives the session id and `pgrep -f`'s the
process, SIGTERM first, SIGKILL after 5s if it's still alive. The board
comment distinguishes a deliberate stop ("stopped per your `--stop` request")
from a genuine crash — the session survives either way; comment again to
resume where it left off. Verified live: stopped a real in-flight run and
watched it report correctly instead of reading as a crash.

## Browser-dependent steps — the attended lane

Headless agents have **no browser, by design**. Claude in Chrome bridges an
*interactive* session to your Chrome extension; a daemon-spawned `claude -p`
gets no bridge — and shouldn't: an unattended agent driving your logged-in
browser collides with your own browsing and takes its instructions from page
content while holding your session cookies (GitHub, SaaS dashboards, …).

When an agent reports it needs a browser (portal uploads, web round-trips),
resume **the same conversation** attended — one command, route auto-detected
(every BLOCKED comment carries it ready to paste):

```sh
python3 ~/bin/agentd.py attend 42         # or: alias attend='python3 ~/bin/agentd.py attend'
```

It launches straight into the session in the right directory with the browser
available. The agent continues exactly where it stopped, with you watching.

**Exit = hand-back.** When you close the terminal, the daemon notices the
attended process is gone (within one poll) and launches a short settle run in
the same conversation: it posts the outcome on the issue, sets `Agent status`,
commits leftover finished work, and pushes. No "read the attended session and
update the board" comment, no manual settling — closing the terminal *is* the
ritual. (Don't comment on the issue while the terminal is still open — the
collision guard will refuse the headless run and tell you.)

`--session <route> <issue>` still exists for printing the raw resume command,
e.g. when an issue has sessions on several routes.

## Cleanup after merge

The daemon deliberately doesn't do this, because deleting worktrees and retiring
sessions is destructive and you may want the history:

```sh
git -C ~/workspace/web-app worktree remove ~/agents/web/issue-42
git -C ~/workspace/web-app branch -D agent/issue-42
```

Retire the session too — a session that has been resumed a dozen times carries a
large transcript, and after other PRs merge its picture of the repo is stale.
Fresh session + `docs/agent-journal/` beats an ancient session.

## Failure modes worth knowing

- **Two agents, one file.** Worktrees prevent *git* collisions, not *semantic*
  ones. Two agents refactoring the same module produce two PRs that can't both
  merge. Keep concurrent issues in disjoint areas, or set
  `max_concurrent_agents = 1`.
- **The central agent works in your live planning checkout.** If a headless
  central run and your own planning session overlap, `git status` shows what it
  left behind; it commits but never pushes.
- **Sleep mid-run.** The `claude` child dies with the machine. On the next boot
  `reconcile()` finds the orphaned `running` lease in `state.db` and posts a
  comment on the issue (`reconcile_on_start = "note"`), or re-enqueues it
  (`"resume"`). The worktree and session survive either way.
- **Polling is the feature.** Webhooks + a tunnel would drop events while the
  laptop is closed. Polling re-reads current board state, so being offline for a
  week costs you nothing but latency.
- **Comment replay.** On first boot the daemon marks all existing comments as
  seen and launches nothing. Delete `state.db` and it will re-prime the same
  way — old comments are never replayed. Agents are told to read the full issue
  thread themselves, so pre-daemon comments still reach them as context.
- **`--permission-mode acceptEdits`**, not `--dangerously-skip-permissions`.
  Each repo's `.claude/settings.json` carries the allow/deny rules; in headless
  runs every Bash command *not* allowed is auto-denied, so the repo's own
  test/build commands must be in the allow list. The daemon passes the file via
  `--settings` explicitly, because untrusted directories (every fresh worktree)
  silently ignore their project settings otherwise. Rule syntax is
  `Bash(cmd:*)` — a bare trailing `*` matches nothing.
