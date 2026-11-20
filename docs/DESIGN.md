# agentd — design

## Problem

Drive Claude Code agents from a GitHub Projects board, with four hard constraints:

1. Agents run **on the laptop**, not in CI.
2. Laptop off ⇒ nothing runs.
3. The board is the interface: one conversation per issue, reachable by commenting.
4. **The columns route to different agents.** The planning column (`In plan`)
   belongs to the central planning agent working in the
   planning repo; each `agent: <track>` column belongs to that track's own repo
   and agent. All the issues live in one board repo (e.g. `you/board`); the
   *work* happens in many.

## Shape

```
  your Projects board                        the laptop
  ┌──────────────────────┐                   ┌─────────────────────────────────┐
  │                      │                   │ agentd (LaunchAgent / user svc) │
  │ In plan ─── central  │                   │  poll ─┬─ moves into agent:*    │
  │                      │◀── comment poll ──│        └─ comments on routed    │
  │ agent: web   ─┐      │                   │  ┌─────▼─────┐    cards         │
  │ agent: docs   ├ per- │                   │  │ job queue │ bounded          │
  │ agent: ...   ─┘ track│                   │  └─────┬─────┘                  │
  │ archive, personal:   │                   │   worker 1..N                   │
  │   ignored            │                   └────────┬────────────────────────┘
  └──────────────────────┘                            │ spawn per issue+route
        ▲          │                     central: claude -p in ~/workspace/planning
        │          │ human comment       track:   claude -p in worktree ~/agents/<route>/issue-N
        └── agent comment ◀──────────────         branch agent/issue-N, draft PR
```

## The load-bearing decisions

### 1. Polling, not webhooks

GitHub has no Actions trigger for Projects v2 anyway, but that is not why. The
reason is constraint 2. A webhook needs a public endpoint, so a tunnel; while the
laptop is closed GitHub retries a handful of times and then **drops the delivery
permanently**. You would silently lose board moves and have no way to know.

Polling reads *current state*, which makes offline periods free. Closed for a
week? The next poll sees the board as it is now. The cost is up to
`poll_interval_seconds` of latency, which for a human dragging cards is
imperceptible.

This also makes the daemon crash-safe for free: there is no event backlog to
replay, because there are no events.

### 2. The columns are the routing table, not a state machine

Each `[[route]]` in the config binds a set of Status options to one agent
identity: a working directory, a mode, and a session namespace. The card's
column *is* the durable record of who owns it — which is why the daemon never
moves cards between columns as a lease. A card that left `agent: web` for
`Agent working` would stop being web's, and follow-up comments would have
nowhere to route.

Two triggers, both diffed against `state.db`:

- **A card moves into a routed column** (`route_for(new) ≠ route_for(old)`).
  This is the handover gesture for `agent: *` columns. The planning column sets
  `trigger_on_move = false`, because cards are dragged into and out of `In plan`
  constantly during planning and that must not launch anything — also, a move
  *between two columns of the same route* never re-triggers.
- **A new human comment on a card sitting in a routed column.** First comment
  starts the session; every later one is a follow-up turn in it. Comments made
  while that issue's agent is mid-run are left unmarked and picked up on the
  next poll after it finishes — deferred, never dropped.

The daemon's own comments carry an HTML marker and are never treated as
commands. Unrouted columns (`archive`, `personal`, `Blocked`, `No priority
yet`, `Next 3 months`) are invisible to the daemon.

The one column write the daemon can make is `blocked_status`: park a card in an
existing column (e.g. `Blocked`) when its agent reports BLOCKED. Off by
default, because a parked card is outside every route — comments stop reaching
the agent until a human drags it back.

### Ownership, run-state, and "done" are three different things

A single-select column can hold one value, so it must model exactly one
dimension. Here it models **ownership** (whose queue is this card in). The
other two dimensions live elsewhere:

- **Completion is a property of the issue, not the board: closed = done.**
  The human closes the issue after reviewing the result. Agents never close on
  their own judgment — `STATUS: DONE` means "ready for review", not "task
  over" — but an explicit "close it" from the human in the thread is their
  decision already made, and the agent executes it. A closed issue is skipped by
  `Board.fetch`, so it drops out of the daemon's world instantly and its card
  can be project-archived (`--tidy`) — or left anywhere — without ever
  colliding with routing. Reopening the issue puts it back under whatever column it
  happens to sit in.
- **Run-state is the optional `agent_status_field`** — a *second* single-select
  field (`working` / `done` / `blocked`) that only the daemon writes and that
  never affects routing. It answers "is the agent on it, finished, or stuck?"
  at a glance without moving the card: a card showing `done` in `agent: web` is
  web's finished work awaiting human review, and follow-up comments still reach
  the web agent because the card never left the column. Off by default; enable
  by creating the field on the board and naming it in the config.

This is also why the daemon has no "done column": moving a finished card would
be the lease-column mistake again — encoding lifecycle into the axis that
encodes ownership.

### 3. Two modes: worktree and in-place

**Project routes** (`mode = "worktree"`) get
`git worktree add -B agent/issue-N ~/agents/<route>/issue-N origin/main` in
their track's repo. A single checkout has one `HEAD`; two concurrent agents in
one checkout fight over the index and the branch pointer. Worktrees give each
agent a real directory on its own branch, sharing one object store, so
parallelism is actually safe rather than nominally allowed. The daemon pushes
the branch and opens a **draft** PR when the run ends.

Worktrees prevent *git* collisions, not *semantic* ones. Two agents refactoring
the same module still produce two PRs that cannot both merge. That is a work-
scheduling problem for the human, not something the daemon can fix.

**The central route** (`mode = "in_place"`) runs in the planning checkout
itself — deliberately. Its job is not to produce a reviewable diff; it is to
triage, draft replies (e.g. into a `handoffs/MESSAGE-DRAFTS.md` file), and move cards on the
board, following the triage rules in the planning repo's `CLAUDE.md`. It
commits but never pushes; no branch, no PR. The cost is that a headless central
run and a human planning session can touch the same working tree — acceptable
because central tasks are small and additive, and `git status` shows anything
it left behind.

### 4. Deterministic session IDs, scoped per issue *and* route

Claude Code's `--session-id` takes a UUID. Rather than capturing the session ID
from a run's JSON output and storing it, derive it:

```python
uuid.uuid5(SESSION_NAMESPACE, f"agentd://{repo}/issue/{n}/route/{name}")
```

Issue #42 on route `web` always maps to the same UUID. First run passes
`--session-id`, every subsequent run passes `--resume` with the identical value.
The mapping cannot drift out of sync with the state database because it is not
stored anywhere — it is recomputed. Losing `state.db` loses your place in the
board diffing but never loses which session belongs to which issue; if the two
disagree about whether a session exists, the runner retries once with the flag
flipped.

The route is in the derivation because Claude Code stores sessions **per
working directory**: a session started in the web worktree cannot be resumed
from the docs one. Re-routing a card therefore starts a fresh conversation in
the new cwd — correct, since the new agent has a different context anyway. Columns
that share one route share the conversation, so a card drifting between two
planning columns of the same route keeps it.

Note this only works locally: session transcripts live in `~/.claude/projects/`,
so in CI on an ephemeral runner there is nothing to resume.

## The worktree lifecycle of an issue

Agents never manage their own isolation — the daemon imposes the whole
lifecycle before an agent wakes up. Two tasks on the same project cannot
corrupt each other's git state; they can still compete semantically (below).

1. **Create & isolate** — on first trigger, the daemon cuts
   `~/agents/<route>/issue-N` on branch `agent/issue-N` from `origin/main`
   and launches the agent inside it. Per *issue*, not per project: sibling
   worktrees share one object store but have separate HEADs, indexes, and
   branches. The agent's only rule is "stay in your worktree."
2. **During work** — commits accumulate on the issue's branch. Each run also
   folds `origin/main` into the branch (guardrail/journal conflicts
   auto-resolve; real ones are reported), so parallel branches don't drift
   into unmergeable territory while they wait.
3. **Done ≠ merged, by design** — a finished run gets its branch pushed and a
   **draft PR** opened/updated. "Merging back" happens on GitHub through the
   human's review — never by an agent merging locally into the main checkout.
   Merge is a human act or an explicit "merge it" instruction; on repos where
   merge = deploy (e.g. a site that deploys from `main`) it is exclusively
   the human's.
4. **Blocked** — a blocked or failed run leaves everything committed in its
   worktree (usually pushed too). Nothing merges, nothing is lost; the next
   comment resumes in the same worktree from the same state.
5. **The caveat worktrees cannot solve** — two tasks editing the *same files*
   produce two PRs that can't both merge. Git isolation is not semantic
   isolation: the second PR reports its real conflicts after the first
   merges. Keeping concurrent same-project cards on disjoint areas is a
   scheduling judgment for the human and the central agent's triage.
6. **End of life** — after PR merge and issue close, the worktree and branch
   remain on disk deliberately (history preservation; see OPERATIONS for the
   two-command manual cleanup).

## Interruption handling

The `claude` child dies with the machine. The lease is `running = 1` on the
issue's session row, set before launch and cleared when the worker finishes —
anything still set at boot was interrupted mid-run. `reconcile()` then, per
`reconcile_on_start`:

- `"note"` (default) — post a comment saying the run was interrupted; a human
  resumes by replying. Safe: cannot loop.
- `"resume"` — re-enqueue automatically if the card still sits in the same
  route, guarded by `max_runs_per_issue`.

Either way the working directory and the session survive, so a resumed run
continues from where it stopped rather than restarting. The circuit breaker in
`enqueue()` refuses any issue+route past `max_runs_per_issue` regardless of path.

On the **very first poll** (empty `state.db`) the daemon records where every
card sits, marks existing comments as seen, and launches nothing — otherwise
every card already parked in a routed column would fire at once.

## Memory model

The session is not the memory. The layers, deliberately separate:

| Layer | Holds | Lifetime |
| --- | --- | --- |
| each repo's `CLAUDE.md` | architecture, commands, conventions — and in the planning repo, the triage rules | stable, loaded every run |
| `.claude/settings.json` | tool permissions and deny rules | stable |
| `docs/agent-journal/` | one file per entry: decisions, dead ends, gotchas | grows |
| the session (uuid5) | this issue's working conversation | one issue's lifetime |
| git history | the actual result | forever |

Sessions are per-issue and should be **retired after the PR merges**. A session
resumed twenty times carries a huge transcript, and once other PRs land its
picture of the repo is stale. Fresh session plus a good journal beats an ancient
session — which is exactly why the journal exists and why every prompt tells the
agent to write to it.

## Rejected alternatives

**Webhook + ngrok/cloudflared tunnel.** Real-time, but drops deliveries while
offline and adds a public ingress to a laptop. Rejected on constraint 2.

**GitHub Actions with a self-hosted runner on the laptop.** Closest thing to
"CI but local", and it gets the `issues`/`issue_comment` triggers for free. But a
self-hosted runner still needs the queue to exist on GitHub's side, the runner
appears offline rather than the work being skipped, and you inherit the whole
Actions security model for something that only ever serves one person. Also loses
session continuity: each job is a fresh workspace unless you hand-cache
`~/.claude`.

**Lease columns on the board (`Ready for agent` → `Agent working` → …).** The
original design; rejected once the board became the router. Moving a card out
of its column to mark it "working" destroys the very information that routes
follow-up comments, requires four new options on a board whose columns already
carry meaning, and fights the human's own use of the board. The lease lives in
`state.db` instead, and "which agent owns this" stays visible as the column.

**One daemon-wide session with issue numbers in the prompt.** Simpler, but every
issue's context pollutes every other issue's, and cost grows without bound.

**Storing session IDs in SQLite.** Works, but adds a failure mode (db and
`~/.claude` disagreeing) that `uuid5` eliminates entirely.

**Triggering on labels instead of Status.** Labels *do* fire real webhooks and
would enable a CI path, but they are a second thing to keep in sync with the
board. Since we are polling anyway, read the board directly — one source of truth.

**Auto-merging.** Never. The daemon pushes a *draft* PR and stops.

## Deliberate omissions

- **No merging.** Draft PR is the terminus.
- **No worktree cleanup.** Destructive, and you may want the history. Manual.
- **No secrets handling.** The daemon inherits the human's `gh` credentials and
  keychain; it never reads, stores, or passes a token — the one exception is
  the optional bot account's token (`bot_login`), read from `AGENTD_BOT_TOKEN`
  or `~/.agentd/bot_token`.
- **No prompt-injection hardening on issue bodies.** Acceptable only because this
  is a single-user private board on a private repo. If the repo ever accepts
  outside issues or comments, gate on author before this becomes a remote code
  execution path.
