#!/usr/bin/env python3
"""
agentd — drive local Claude Code agents from a GitHub Projects board.

The board's columns are routes: the planning column (`In plan`) routes to
the central planning agent in the planning repo; each `agent: <track>`
column routes to that track's own repo and agent. A new comment on a routed card
starts (first time) or resumes (after that) the issue's session; moving a card
into an `agent: *` column is the handover that starts work.

One long-lived Claude Code session per issue *per route*, keyed by a
deterministic UUID. Project routes get a git worktree per issue; the central
route runs in the planning checkout itself.

Polls (never listens), so the laptop being off simply means nothing happens
and nothing is lost: on wake, the next poll sees the current board state.

Requires: gh (authenticated, with `project` scope), git, claude, python >= 3.11.
    gh auth refresh -s project,read:project
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

LOG = logging.getLogger("agentd")

# Marker so the daemon never treats its own comments as commands.
MARKER = "<!-- agentd -->"

SESSION_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

@dataclass
class Route:
    """One agent identity: which columns it owns and where it works."""
    name: str                     # session namespace + workspace dir name
    columns: list[str]            # Status options that route here, spelled exactly
    workdir: str                  # the repo checkout this agent works in
    mode: str = "worktree"        # "worktree" (branch + draft PR) or "in_place"
    repo: str = ""                # code repo slug for PRs; worktree mode only
    base_branch: str = "main"
    # Planning columns get reshuffled constantly; a drag between them must not
    # launch anything. Project columns treat a drag-in as the handover.
    trigger_on_move: bool = True
    # Give this route's HEADLESS runs the Chrome bridge (--chrome). Verified
    # working, but off by default: an unattended agent drives the real
    # logged-in browser — enable per route only on a machine/profile set up
    # for it, and allow "mcp__claude-in-chrome" in that repo's settings.
    browser: bool = False
    # Scope stamped onto this route's cards when their Scope is unset
    # (personal-project routes set "personal"). Empty = the global
    # scope_default. Hand-set values are never overwritten either way.
    scope: str = ""


@dataclass
class Config:
    repo: str                     # the repo the board's issues live in
    project_owner: str
    project_number: int
    owner_type: str = "user"      # "user" or "organization"
    workspace_root: str = "~/agents"
    poll_interval_seconds: int = 60
    max_concurrent_agents: int = 2
    status_field: str = "Status"
    # Optional: park a card here when the agent reports BLOCKED or crashes.
    # Empty = leave the card where it is (comments keep reaching the agent).
    blocked_status: str = ""
    # Optional second single-select field the daemon writes run-state to
    # (options spelled exactly: working / done / blocked), so the Status
    # column keeps modelling ownership only. Empty = never written.
    # Task completion is NOT a column either way: closing the issue is "done",
    # and closed issues are invisible to the daemon.
    agent_status_field: str = ""
    claude_max_turns: int = 120
    claude_max_budget_usd: float = 10.0
    claude_model: str = "sonnet"
    state_db: str = "~/.agentd/state.db"

    # Optional single-select field separating work streams (e.g. contract vs
    # personal). The daemon stamps scope_default onto any card whose value is
    # UNSET — a hand-set value is never overwritten, so marking a card
    # personal always sticks. Empty = feature off.
    scope_field: str = ""
    scope_default: str = "contract"

    # The daemon's own comments/reactions can be authored by a separate bot
    # account: set bot_login here and put its token in the AGENTD_BOT_TOKEN
    # environment variable (LaunchAgent plist). Because the bot is a
    # different author, GitHub then push-notifies the owner about agent
    # comments — impossible with self-authored ones. Comments FROM this
    # login are never treated as instructions. Empty = everything runs as
    # the human's own account (marker discipline carries the load).
    bot_login: str = ""

    # Desktop notifications for run lifecycle (picked up / done / blocked /
    # crashed). "macos" = Notification Center banners via osascript; "" = off.
    # This is the only push channel that can exist here: every issue comment
    # the daemon posts is authored by YOUR account, and GitHub never notifies
    # you about your own activity.
    notify: str = ""

    # Runs interrupted mid-flight (sleep, shutdown) are detected on boot.
    #   "note"   = post a comment; a human re-triggers by commenting. Safe.
    #   "resume" = re-enqueue automatically, bounded by max_runs_per_issue.
    reconcile_on_start: str = "note"
    max_runs_per_issue: int = 20      # circuit breaker per issue+route

    routes: list[Route] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Config":
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        route_dicts = raw.pop("route", [])
        known = {f for f in cls.__dataclass_fields__} - {"routes"}
        unknown = set(raw) - known
        if unknown:
            raise SystemExit(f"unknown config keys: {sorted(unknown)}")
        cfg = cls(**raw, routes=[Route(**r) for r in route_dicts])
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.routes:
            raise SystemExit("config has no [[route]] blocks; nothing would ever run")
        seen_cols: dict[str, str] = {}
        seen_names: set[str] = set()
        for route in self.routes:
            if route.name in seen_names:
                raise SystemExit(f"duplicate route name {route.name!r}")
            seen_names.add(route.name)
            if route.mode not in ("worktree", "in_place"):
                raise SystemExit(f"route {route.name!r}: bad mode {route.mode!r}")
            if route.mode == "worktree" and not route.repo:
                raise SystemExit(f"route {route.name!r}: worktree mode needs repo = \"owner/name\"")
            for col in route.columns:
                if col in seen_cols:
                    raise SystemExit(
                        f"column {col!r} claimed by both {seen_cols[col]!r} and {route.name!r}")
                seen_cols[col] = route.name

    def route_for(self, status: str | None) -> Route | None:
        for route in self.routes:
            if status in route.columns:
                return route
        return None

    def expand(self, value: str) -> Path:
        return Path(os.path.expanduser(value)).resolve()


# --------------------------------------------------------------------------- #
# thin shell helpers
# --------------------------------------------------------------------------- #

def _bot_token() -> str:
    """The bot's PAT: AGENTD_BOT_TOKEN env var, or ~/.agentd/bot_token
    (chmod 600) so the secret never has to live inside the plist."""
    token = os.environ.get("AGENTD_BOT_TOKEN", "")
    if not token:
        path = Path(os.path.expanduser("~/.agentd/bot_token"))
        if path.exists():
            token = path.read_text().strip()
    return token


def run(cmd: list[str], cwd: Path | None = None, check: bool = True,
        timeout: int | None = 120, input_text: str | None = None,
        as_bot: bool = False) -> subprocess.CompletedProcess:
    LOG.debug("exec %s", " ".join(cmd))
    env = None
    if as_bot:
        token = _bot_token()
        if token:
            env = {**os.environ, "GH_TOKEN": token}
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        input=input_text, env=env,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}"
        )
    return proc


def gh_graphql(query: str, **variables) -> dict:
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        flag = "-F" if isinstance(value, (int, float, bool)) else "-f"
        cmd += [flag, f"{key}={value}"]
    return json.loads(run(cmd).stdout)


# --------------------------------------------------------------------------- #
# GitHub: board reads and writes
# --------------------------------------------------------------------------- #

PROJECT_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  OWNER_TYPE(login: $owner) {
    projectV2(number: $number) {
      id
      fields(first: 50) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id
            name
            options { id name }
          }
        }
      }
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2SingleSelectField { name } }
              }
            }
          }
          content {
            ... on Issue {
              number
              title
              body
              url
              state
              repository { nameWithOwner }
            }
          }
        }
      }
    }
  }
}
"""

SET_FIELD_MUTATION = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field,
    value: { singleSelectOptionId: $option }
  }) { projectV2Item { id } }
}
"""


@dataclass
class BoardItem:
    item_id: str
    issue: int
    title: str
    body: str
    url: str
    status: str | None
    scope: str | None = None


class Board:
    """Read the project board; write status back to it."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.project_id: str | None = None
        self.field_id: str | None = None
        self.options: dict[str, str] = {}
        self.agent_field_id: str | None = None
        self.agent_options: dict[str, str] = {}
        self.scope_field_id: str | None = None
        self.scope_options: dict[str, str] = {}
        self._warned_repos: set[str] = set()

    def fetch(self) -> list[BoardItem]:
        query = PROJECT_QUERY.replace("OWNER_TYPE", self.cfg.owner_type)
        items: list[BoardItem] = []
        cursor: str | None = None

        while True:
            variables = {"owner": self.cfg.project_owner,
                         "number": self.cfg.project_number}
            if cursor:
                variables["cursor"] = cursor
            data = gh_graphql(query, **variables)
            project = data["data"][self.cfg.owner_type]["projectV2"]
            self.project_id = project["id"]

            for fld in project["fields"]["nodes"]:
                if not fld:
                    continue
                if fld.get("name") == self.cfg.status_field:
                    self.field_id = fld["id"]
                    self.options = {o["name"]: o["id"] for o in fld["options"]}
                elif self.cfg.agent_status_field and \
                        fld.get("name") == self.cfg.agent_status_field:
                    self.agent_field_id = fld["id"]
                    self.agent_options = {o["name"]: o["id"] for o in fld["options"]}
                elif self.cfg.scope_field and \
                        fld.get("name") == self.cfg.scope_field:
                    self.scope_field_id = fld["id"]
                    self.scope_options = {o["name"]: o["id"] for o in fld["options"]}

            for node in project["items"]["nodes"]:
                content = node.get("content") or {}
                if "number" not in content or content.get("state") != "OPEN":
                    continue  # drafts, PRs, closed issues
                repo = (content.get("repository") or {}).get("nameWithOwner")
                if repo and repo != self.cfg.repo:
                    # Comment polling only covers cfg.repo; a card from another
                    # repo would trigger but never hear replies. Skip loudly, once.
                    if repo not in self._warned_repos:
                        self._warned_repos.add(repo)
                        LOG.warning("board has issues from %s; only %s is handled",
                                    repo, self.cfg.repo)
                    continue
                status = scope = None
                for fv in node["fieldValues"]["nodes"]:
                    fld = (fv or {}).get("field") or {}
                    if fld.get("name") == self.cfg.status_field:
                        status = fv.get("name")
                    elif self.cfg.scope_field and \
                            fld.get("name") == self.cfg.scope_field:
                        scope = fv.get("name")
                items.append(
                    BoardItem(
                        item_id=node["id"],
                        issue=content["number"],
                        title=content["title"],
                        body=content.get("body") or "",
                        url=content["url"],
                        status=status,
                        scope=scope,
                    )
                )

            page = project["items"]["pageInfo"]
            if not page["hasNextPage"]:
                break
            cursor = page["endCursor"]

        if not self.field_id:
            raise SystemExit(
                f"no single-select field named {self.cfg.status_field!r} on the project"
            )
        return items

    def set_status(self, item_id: str, status: str) -> None:
        option = self.options.get(status)
        if not option:
            LOG.warning("board has no status option %r; skipping write", status)
            return
        gh_graphql(
            SET_FIELD_MUTATION,
            project=self.project_id,
            item=item_id,
            field=self.field_id,
            option=option,
        )
        LOG.info("board: item -> %s", status)

    def set_agent_status(self, item_id: str, value: str) -> None:
        """Write run-state (working/done/blocked) to the optional second
        field. Never touches the Status column, so routing is unaffected."""
        if not self.cfg.agent_status_field:
            return
        if not self.agent_field_id or value not in self.agent_options:
            LOG.warning("board is missing field %r or its option %r; skipping",
                        self.cfg.agent_status_field, value)
            return
        gh_graphql(
            SET_FIELD_MUTATION,
            project=self.project_id,
            item=item_id,
            field=self.agent_field_id,
            option=self.agent_options[value],
        )
        LOG.info("board: %s -> %s", self.cfg.agent_status_field, value)

    def set_default_scope(self, item_id: str, value: str) -> None:
        """Stamp a scope onto a card with NO scope value. Never called for
        cards that have one — a hand-set value always wins."""
        option = self.scope_options.get(value)
        if not (self.scope_field_id and option):
            LOG.warning("scope field %r or option %r missing; skipping",
                        self.cfg.scope_field, value)
            return
        gh_graphql(
            SET_FIELD_MUTATION,
            project=self.project_id,
            item=item_id,
            field=self.scope_field_id,
            option=option,
        )


def recent_comments(cfg: Config, per_page: int = 100) -> list[dict]:
    """Repo-wide recent issue comments: one API call for every tracked issue."""
    proc = run([
        "gh", "api",
        f"repos/{cfg.repo}/issues/comments"
        f"?sort=created&direction=desc&per_page={per_page}",
    ])
    return json.loads(proc.stdout)


def post_comment(cfg: Config, issue: int, body: str) -> None:
    run([
        "gh", "issue", "comment", str(issue),
        "--repo", cfg.repo,
        "--body-file", "-",
    ], input_text=f"{MARKER}\n{body}", as_bot=True)


def react_received(cfg: Config, comment_id: int) -> None:
    """👀 on the triggering comment: instant, in-place receipt signal."""
    run(["gh", "api", "-X", "POST",
         f"repos/{cfg.repo}/issues/comments/{comment_id}/reactions",
         "-f", "content=eyes"], check=False, timeout=30, as_bot=True)


_NOTIFY_PLAIN = """\
on run argv
  display notification (item 2 of argv) with title (item 1 of argv)
end run"""

_NOTIFY_SOUND = """\
on run argv
  display notification (item 2 of argv) with title (item 1 of argv) \
sound name "Glass"
end run"""


def notify_user(cfg: Config, title: str, message: str, sound: bool = False) -> None:
    """Best-effort desktop banner. Never allowed to break a run."""
    if cfg.notify != "macos":
        return
    try:
        run(["osascript", "-e", _NOTIFY_SOUND if sound else _NOTIFY_PLAIN,
             f"agentd — {title}", message], check=False, timeout=15)
    except Exception:
        LOG.debug("notification failed", exc_info=True)


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    number      INTEGER PRIMARY KEY,
    last_status TEXT,
    updated_at  REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    number          INTEGER NOT NULL,
    route           TEXT    NOT NULL,
    session_started INTEGER NOT NULL DEFAULT 0,
    runs            INTEGER NOT NULL DEFAULT 0,
    running         INTEGER NOT NULL DEFAULT 0,
    workdir         TEXT,
    updated_at      REAL,
    PRIMARY KEY (number, route)
);
CREATE TABLE IF NOT EXISTS seen_comments (
    id         INTEGER PRIMARY KEY,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS attended (
    sid       TEXT PRIMARY KEY,
    number    INTEGER NOT NULL,
    route     TEXT    NOT NULL,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    number     INTEGER NOT NULL,
    route      TEXT    NOT NULL,
    kind       TEXT,
    started_at REAL,
    seconds    INTEGER,
    turns      INTEGER,
    cost       REAL,
    ok         INTEGER,
    blocked    INTEGER
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        for col in ("model", "effort"):   # additive migration for older dbs
            try:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
        self.db.commit()
        self.lock = threading.Lock()

    # -- cards -------------------------------------------------------------- #

    def last_status(self, issue: int) -> str | None:
        with self.lock:
            row = self.db.execute(
                "SELECT last_status FROM cards WHERE number = ?", (issue,)
            ).fetchone()
        return row[0] if row else None

    def set_last_status(self, issue: int, status: str | None) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO cards (number, last_status, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(number) DO UPDATE SET last_status=excluded.last_status,"
                " updated_at=excluded.updated_at",
                (issue, status, time.time()))
            self.db.commit()

    # -- sessions ----------------------------------------------------------- #

    def session(self, issue: int, route: str) -> dict:
        with self.lock:
            row = self.db.execute(
                "SELECT session_started, runs, running, workdir FROM sessions "
                "WHERE number = ? AND route = ?", (issue, route)).fetchone()
        if not row:
            return {"session_started": 0, "runs": 0, "running": 0, "workdir": None}
        return dict(zip(("session_started", "runs", "running", "workdir"), row))

    def upsert_session(self, issue: int, route: str, **fields) -> None:
        current = self.session(issue, route) | fields
        with self.lock:
            self.db.execute(
                "INSERT INTO sessions (number, route, session_started, runs,"
                " running, workdir, updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(number, route) DO UPDATE SET"
                " session_started=excluded.session_started, runs=excluded.runs,"
                " running=excluded.running, workdir=excluded.workdir,"
                " updated_at=excluded.updated_at",
                (issue, route, current["session_started"], current["runs"],
                 current["running"], current["workdir"], time.time()))
            self.db.commit()

    def stuck_running(self) -> list[tuple[int, str, int]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT number, route, runs FROM sessions WHERE running = 1"
            ).fetchall()
        return [(int(n), r, int(runs)) for n, r, runs in rows]

    # -- comments ----------------------------------------------------------- #

    def comment_is_new(self, comment_id: int) -> bool:
        with self.lock:
            if self.db.execute("SELECT 1 FROM seen_comments WHERE id = ?",
                               (comment_id,)).fetchone():
                return False
            self.db.execute(
                "INSERT INTO seen_comments (id, created_at) VALUES (?,?)",
                (comment_id, time.time()))
            self.db.commit()
        return True

    def mark_seen(self, comment_id: int) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR IGNORE INTO seen_comments (id, created_at) VALUES (?,?)",
                (comment_id, time.time()))
            self.db.commit()

    # -- run ledger ---------------------------------------------------------- #

    def record_run(self, number: int, route: str, kind: str, started_at: float,
                   seconds: int, turns: int, cost: float, ok: bool,
                   blocked: bool, model: str = "", effort: str = "") -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO runs (number, route, kind, started_at, seconds,"
                " turns, cost, ok, blocked, model, effort)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (number, route, kind, started_at, seconds, turns, cost,
                 int(ok), int(blocked), model, effort))
            self.db.commit()

    # -- attended-session tracking ------------------------------------------ #

    def started_sessions(self) -> list[tuple[int, str]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT number, route FROM sessions WHERE session_started = 1"
            ).fetchall()
        return [(int(n), r) for n, r in rows]

    def attended_seen(self, sid: str, number: int, route: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO attended (sid, number, route, last_seen) VALUES (?,?,?,?) "
                "ON CONFLICT(sid) DO UPDATE SET last_seen=excluded.last_seen",
                (sid, number, route, time.time()))
            self.db.commit()

    def attended_rows(self) -> list[tuple[str, int, str]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT sid, number, route FROM attended").fetchall()
        return [(s, int(n), r) for s, n, r in rows]

    def attended_delete(self, sid: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM attended WHERE sid = ?", (sid,))
            self.db.commit()

    # -- first boot --------------------------------------------------------- #

    def is_primed(self) -> bool:
        with self.lock:
            return self.db.execute(
                "SELECT 1 FROM meta WHERE key = 'primed'").fetchone() is not None

    def mark_primed(self) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('primed', '1')")
            self.db.commit()


# --------------------------------------------------------------------------- #
# worktrees
# --------------------------------------------------------------------------- #

# Two concurrent jobs on the same route share one .git; parallel fetch /
# worktree-add there collide on repo locks. Serialize creation per repo —
# the agent *runs* stay parallel, only the setup is brief and exclusive.
_REPO_LOCKS: dict[str, threading.Lock] = {}
_REPO_LOCKS_GUARD = threading.Lock()

# There is exactly one Chrome. Two agents driving it at once interleave tab
# operations into nonsense, so browser-flagged runs execute one at a time.
_BROWSER_LOCK = threading.Lock()


def _repo_lock(repo_path: Path) -> threading.Lock:
    with _REPO_LOCKS_GUARD:
        return _REPO_LOCKS.setdefault(str(repo_path), threading.Lock())


GUARDRAIL_FILES = ("CLAUDE.md", ".claude/settings.json", ".gitattributes",
                   "docs/agent-journal/README.md")

# The subset that must exist on origin before a worktree-mode route's first
# run: without these an agent runs uninformed and with no push guardrail
# (a route once went live with none of them; its agents ran blind).
REQUIRED_GUARDRAIL_FILES = ("CLAUDE.md", ".claude/settings.json",
                            "docs/agent-journal/README.md")

# Single source for rules that apply to every route (never task context —
# see the file's own header). Lives with the daemon, not in any track's repo,
# so this is the one guardrail the daemon injects itself rather than pulling
# from the track's own origin. bootstrap.sh installs it to ~/.agentd/; running
# straight from a clone falls back to the repo's templates/ copy.
FLEET_RULES_CANDIDATES = (
    Path(os.path.expanduser("~/.agentd/FLEET-RULES.md")),
    Path(__file__).resolve().parent.parent / "templates" / "FLEET-RULES.md",
)
FLEET_RULES_DEST = ".claude/FLEET-RULES.md"

# Per-repo standing instructions that are NOT versioned: the repo's committed
# CLAUDE.md holds rules shared with every contributor, while INSTRUCT.md holds
# the agent-only instructions, gitignored in the route's main checkout. The
# daemon copies it into each worktree and appends it to the system prompt of
# every run, so it applies like a CLAUDE.md without being committed. Routes
# without the file behave exactly as before.
INSTRUCT_FILE = "INSTRUCT.md"


def _sync_instructions(repo_path: Path, workspace: Path) -> None:
    """Copy <workdir>/INSTRUCT.md into the worktree (plain copy, never committed)."""
    try:
        src = repo_path / INSTRUCT_FILE
        dest = workspace / INSTRUCT_FILE
        if src.exists() and src.resolve() != dest.resolve():
            content = src.read_text()
            if not dest.exists() or dest.read_text() != content:
                dest.write_text(content)
                LOG.info("%s: %s synced from %s", workspace.name, INSTRUCT_FILE, repo_path)
    except Exception:
        LOG.exception("instructions sync failed; run continues with existing file")


def instruct_args(cwd: Path) -> list[str]:
    """--append-system-prompt with the worktree's INSTRUCT.md, if there is one."""
    path = cwd / INSTRUCT_FILE
    return ["--append-system-prompt", path.read_text()] if path.exists() else []


class GuardrailsMissing(Exception):
    """Raised by ensure_worktree() when a worktree-mode route's origin branch
    lacks the files an unattended agent needs. Caught in execute() so the
    card is parked with a specific comment instead of a generic crash."""
    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(f"missing guardrails: {', '.join(missing)}")


def missing_guardrails(route: Route, repo_path: Path) -> list[str]:
    """Which REQUIRED_GUARDRAIL_FILES are absent from origin/<base_branch>,
    checked against the route's own checkout — no worktree needed yet."""
    missing = []
    for f in REQUIRED_GUARDRAIL_FILES:
        probe = run(["git", "cat-file", "-e", f"origin/{route.base_branch}:{f}"],
                    cwd=repo_path, check=False)
        if probe.returncode != 0:
            missing.append(f)
    return missing


def _sync_fleet_rules(workspace: Path) -> None:
    """Fleet-wide rules (probe-chaining, scratch files, browser read-vs-post)
    are single-sourced in one FLEET-RULES.md (see FLEET_RULES_CANDIDATES)
    instead of pasted into every track's repo. Copy the current file into the
    worktree at FLEET_RULES_DEST so a rule change reaches every
    route on its next run — no per-repo commit needed. Unlike
    _sync_guardrails this is a plain file copy, not a git checkout: the
    source lives with the daemon, not on the track's own origin."""
    try:
        src = next((p for p in FLEET_RULES_CANDIDATES if p.exists()), None)
        if src is None:
            LOG.warning("no FLEET-RULES.md found (looked in %s); skipping fleet "
                        "rules", ", ".join(map(str, FLEET_RULES_CANDIDATES)))
            return
        content = src.read_text()
        dest = workspace / FLEET_RULES_DEST
        if dest.exists() and dest.read_text() == content:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
        if run(["git", "status", "--porcelain", "--", FLEET_RULES_DEST],
               cwd=workspace).stdout.strip():
            run(["git", "add", "--", FLEET_RULES_DEST], cwd=workspace)
            run(["git", "commit", "-q", "-m",
                 "chore: sync fleet rules",
                 "--", FLEET_RULES_DEST], cwd=workspace)
            LOG.info("%s: fleet rules synced from %s", workspace.name, src)
    except Exception:
        LOG.exception("fleet-rules sync failed; run continues with existing file")


def _sync_guardrails(route: Route, workspace: Path) -> None:
    """A worktree snapshots the rules from its creation day; long-lived cards
    would follow outdated instructions forever (hit live: an agent
    cited a rule that had been replaced on main weeks of commits earlier).
    Pull the current guardrail files from origin at every run start. The sync
    commit is main's own content, so the eventual PR merges clean."""
    try:
        run(["git", "fetch", "origin", route.base_branch], cwd=workspace)
        for f in GUARDRAIL_FILES:
            run(["git", "checkout", f"origin/{route.base_branch}", "--", f],
                cwd=workspace, check=False)
        if run(["git", "status", "--porcelain", "--", *GUARDRAIL_FILES],
               cwd=workspace).stdout.strip():
            run(["git", "commit", "-q", "-m",
                 "chore: sync agent guardrails from main",
                 "--", *GUARDRAIL_FILES], cwd=workspace)
            LOG.info("%s: guardrails synced from origin/%s",
                     workspace.name, route.base_branch)
    except Exception:
        LOG.exception("guardrail sync failed; run continues with existing files")
    _sync_fleet_rules(workspace)


def ensure_worktree(cfg: Config, route: Route, issue: int) -> Path:
    repo_path = cfg.expand(route.workdir)
    workspace = cfg.expand(cfg.workspace_root) / route.name / f"issue-{issue}"
    branch = f"agent/issue-{issue}"

    with _repo_lock(repo_path):
        if (workspace / ".git").exists():
            _sync_guardrails(route, workspace)
            _sync_instructions(repo_path, workspace)
            return workspace

        run(["git", "fetch", "origin", route.base_branch], cwd=repo_path)
        missing = missing_guardrails(route, repo_path)
        if missing:
            raise GuardrailsMissing(missing)

        workspace.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "worktree", "add", "-B", branch, str(workspace),
             f"origin/{route.base_branch}"], cwd=repo_path)
    _sync_fleet_rules(workspace)
    _sync_instructions(repo_path, workspace)
    LOG.info("issue #%s: worktree at %s on %s", issue, workspace, branch)
    return workspace


# --------------------------------------------------------------------------- #
# Claude Code invocation
# --------------------------------------------------------------------------- #

def session_id_for(cfg: Config, route: Route, issue: int) -> str:
    """Deterministic per-issue-per-route session UUID — no bookkeeping needed.

    Route-scoped because Claude Code stores sessions per working directory:
    a card re-routed to another column gets a fresh session in the new cwd.
    """
    return str(uuid.uuid5(
        SESSION_NAMESPACE, f"agentd://{cfg.repo}/issue/{issue}/route/{route.name}"))


WORKTREE_PROMPT = """\
You are working on issue #{issue} from the {board_repo} board (column \
"{column}"), in a dedicated git worktree on branch agent/issue-{issue} of \
{code_repo}.

Your standing instructions for this repo (INSTRUCT.md, when it has one) are \
already in your system prompt. Read CLAUDE.md and the docs/agent-journal/ \
entries first if they exist. Then read the \
full issue thread — `gh issue view {issue} --repo {board_repo} --comments` — \
the task and its context links live there.

## Issue: {title}
{url}

{body}

## Rules
- Work only in this worktree. Do not touch other branches.
- Commit in logical units with clear messages referencing {board_repo}#{issue}.
- If the project has tests, run them before you consider the work done.
- Do not push and do not open a pull request; the supervising daemon does both.
- If this run taught you something a future agent would otherwise rediscover
  the hard way, record it as a NEW file docs/agent-journal/{issue}-<slug>.md —
  never append to a shared journal file (concurrent PRs conflict on it).
  Most runs need no entry.
- The daemon posts your final summary automatically — don't duplicate it as a
  comment. Any comment you do post, on any board, must START with the literal
  marker <!-- agentd --> (an unmarked comment is treated as an instruction
  from @{owner} and re-triggers an agent), and its URL must appear in your
  final summary. Boards that people outside the owner can see: post only
  when @{owner}'s instruction in this task explicitly says to post —
  otherwise deliver the comment text as a paste-ready draft.
- End your final message with a 3-6 line summary suitable for posting as a
  GitHub comment, then the literal token STATUS: DONE if the issue is fully
  addressed, or STATUS: BLOCKED with the reason if you need a human.
"""

CENTRAL_PROMPT = """\
You are the central planning agent, working in the live planning repo (not a \
scratch copy). A task has arrived from the "{column}" column of the \
{board_repo} board.

Read CLAUDE.md first — it holds the triage rules for incoming tasks; they \
decide whether you do the task here, draft a message, or route it to a \
project agent's column. Then read the full issue thread: \
`gh issue view {issue} --repo {board_repo} --comments`.

## Issue: {title}
{url}

{body}

## Rules
- Follow the triage rules in CLAUDE.md. Message drafts go into
  handoffs/MESSAGE-DRAFTS.md — never send anything external yourself.
- You may update planning files, and move or create cards on the board with
  `gh`. When you hand a task to a project agent, its context must live in that
  project's own repo and the card must carry the GitHub link to it —
  the planning repo is not readable from the project repos.
- Commit your file changes with a clear message; do not push.
- Your final summary is posted automatically — never comment it on THIS issue
  yourself. Any other comment you post must start with the literal marker
  <!-- agentd --> UNLESS you intend it as an instruction: an unmarked comment
  on a routed card wakes that card's agent (that is how you hand work off —
  do it deliberately, never by accident). Link the URL of every comment you
  post in your final summary. Boards that outsiders can see: only on
  @{owner}'s explicit instruction.
- End your final message with a 3-6 line summary suitable for posting as a
  GitHub comment, then STATUS: DONE, or STATUS: BLOCKED with what you need
  from @{owner}.
"""

FOLLOWUP_PROMPT = """\
New instruction from the issue thread on #{issue} ({author}):

{comment}

Same working directory and rules as before. End with a short summary and
STATUS: DONE or STATUS: BLOCKED.
"""

TRIGGER_COMMENT_SUFFIX = """

## Triggering comment ({author})
{comment}
"""

SETTLE_PROMPT = """\
The attended session on issue #{issue} has just ended (the terminal was
closed). You are now resuming the same conversation headless to settle the
board record. Do NOT redo or extend the work. Review the working tree and
your own conversation, then report: what the attended session accomplished,
what (if anything) remains, and end with a 3-6 line summary suitable for a
GitHub comment plus STATUS: DONE or STATUS: BLOCKED. If there are uncommitted
changes from the attended session that clearly belong to finished work,
commit them first.
"""

RECONCILE_PROMPT = """\
Your previous run on issue #{issue} was interrupted (laptop slept or the
daemon was stopped). Take stock of the working directory, finish what was in
progress, and end with a short summary and STATUS: DONE or STATUS: BLOCKED.
"""


def first_prompt(cfg: Config, route: Route, item: BoardItem) -> str:
    template = CENTRAL_PROMPT if route.mode == "in_place" else WORKTREE_PROMPT
    return template.format(
        issue=item.issue, board_repo=cfg.repo, code_repo=route.repo,
        owner=cfg.project_owner,
        column=item.status, title=item.title, url=item.url,
        body=item.body.strip() or "(no description)")


@dataclass
class AgentResult:
    ok: bool
    text: str
    cost_usd: float
    turns: int
    blocked: bool
    model: str = ""       # actual model(s) reported by the run
    effort: str = ""      # effort requested for the run


# Board directive: a line like `pado: model=opus effort=high` in the issue
# body (default for the whole card) or the triggering comment (override for
# that run). Unknown values are ignored with a log warning, never fatal.
DIRECTIVE_RE = re.compile(r"pado:\s*((?:\w+\s*=\s*[\w.-]+\s*)+)", re.I)
VALID_MODELS_RE = re.compile(r"^(sonnet|opus|haiku|claude-[\w.-]+)$", re.I)
VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def parse_directives(*texts: str) -> dict:
    """Later texts win (comment overrides body)."""
    out: dict = {}
    for text in texts:
        for m in DIRECTIVE_RE.finditer(text or ""):
            for pair in re.findall(r"(\w+)\s*=\s*([\w.-]+)", m.group(1)):
                key, value = pair[0].lower(), pair[1]
                if key == "model" and VALID_MODELS_RE.match(value):
                    out["model"] = value.lower()
                elif key == "effort" and value.lower() in VALID_EFFORTS:
                    out["effort"] = value.lower()
                else:
                    LOG.warning("ignoring invalid directive %s=%s", key, value)
    return out


def _claude_once(cfg: Config, sid: str, prompt: str, cwd: Path,
                 resume: bool, log_path: Path,
                 browser: bool = False, model: str = "",
                 effort: str = "") -> tuple[dict, str, int, int]:
    """One claude -p invocation. Returns (result event, non-JSON tail,
    json event count, returncode)."""
    cmd = [
        "claude", "-p", prompt,
        *(["--chrome"] if browser else []),
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "acceptEdits",
        "--max-turns", str(cfg.claude_max_turns),
        "--max-budget-usd", str(cfg.claude_max_budget_usd),
        "--model", model or cfg.claude_model,
        *(["--effort", effort] if effort else []),
        "--resume" if resume else "--session-id", sid,
    ]
    # Headless runs silently ignore an untrusted directory's .claude/settings.json
    # (no trust dialog was ever accepted there — true of every fresh worktree),
    # which auto-denies every Bash command the agent needs. Passing the file
    # explicitly applies it regardless of trust.
    settings = cwd / ".claude" / "settings.json"
    if settings.exists():
        cmd += ["--settings", str(settings)]
    cmd += instruct_args(cwd)

    final: dict = {}
    events = 0
    tail: list[str] = []
    # Agents never hold the bot token: it exists solely so the daemon's own
    # comment/reaction calls carry a different author. Strip it from the
    # child's environment.
    child_env = {k: v for k, v in os.environ.items() if k != "AGENTD_BOT_TOKEN"}
    with log_path.open("a", encoding="utf-8") as log_file:
        # stderr merged into stdout: reading one pipe while the other fills
        # is a deadlock; interleaved lines just fail the JSON parse harmlessly.
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=child_env,
        )
        for line in proc.stdout:
            log_file.write(line)
            log_file.flush()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                tail.append(line.rstrip())
                del tail[:-40]
                continue
            events += 1
            if event.get("type") == "result":
                final = event
        proc.wait()
    return final, "\n".join(tail), events, proc.returncode


def run_agent(cfg: Config, route: Route, issue: int, prompt: str, cwd: Path,
              resume: bool, log_dir: Path, model: str = "",
              effort: str = "") -> AgentResult:
    sid = session_id_for(cfg, route, issue)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{route.name}-issue-{issue}.jsonl"

    LOG.info("issue #%s [%s]: launching agent (resume=%s, browser=%s, "
             "model=%s, effort=%s)", issue, route.name, resume, route.browser,
             model or cfg.claude_model, effort or "default")
    if route.browser:
        _BROWSER_LOCK.acquire()
    try:
        final, tail, events, rc = _claude_once(cfg, sid, prompt, cwd, resume,
                                               log_path, browser=route.browser,
                                               model=model, effort=effort)

        # Startup-argument failures die before emitting any JSON. The two known
        # cases are state.db and ~/.claude disagreeing about whether the session
        # exists — flipping the flag once repairs both.
        if not final and events == 0:
            LOG.warning("issue #%s [%s]: no output with resume=%s; retrying with %s",
                        issue, route.name, resume, not resume)
            final, tail, events, rc = _claude_once(
                cfg, sid, prompt, cwd, not resume, log_path, browser=route.browser)
    finally:
        if route.browser:
            _BROWSER_LOCK.release()

    if not final:
        return AgentResult(False, f"agent produced no result.\n{tail[-1500:]}",
                           0.0, 0, False)

    text = final.get("result") or ""
    ok = rc == 0 and not final.get("is_error")
    if not text.strip():
        # Cap-hit runs (max turns / max budget) end with is_error and NO text;
        # an empty failure comment on the board helps nobody.
        subtype = final.get("subtype") or ""
        reason = {
            "error_max_turns":
                f"hit the per-run turn limit ({cfg.claude_max_turns} turns)",
            "error_max_budget_usd":
                f"hit the per-run budget (${cfg.claude_max_budget_usd:.2f})",
        }.get(subtype, f"ended without a result (subtype {subtype or '?'}, exit {rc})")
        text = (f"Run {reason} after {int(final.get('num_turns') or 0)} turns, "
                f"${float(final.get('total_cost_usd') or 0.0):.2f} spent. "
                f"The session survives — comment on this issue to continue with "
                f"a fresh run budget, split the task into smaller cards, or "
                f"retire this session if its transcript has grown huge.")
        if tail:
            text += f"\n\nlast raw output:\n{tail[-600:]}"
    used = ",".join(sorted((final.get("modelUsage") or {}).keys())) \
        or (model or cfg.claude_model)
    return AgentResult(
        ok=ok,
        text=text,
        cost_usd=float(final.get("total_cost_usd") or 0.0),
        turns=int(final.get("num_turns") or 0),
        blocked="STATUS: BLOCKED" in text,
        model=used,
        effort=effort,
    )


def publish(cfg: Config, route: Route, issue: int, cwd: Path) -> str:
    """Push the branch and ensure a draft PR exists. Never merges the PR, and
    never force-pushes: attended sessions may also push this branch, so the
    remote is shared — divergence gets merged in and retried, or reported
    honestly, never overwritten."""
    branch = f"agent/issue-{issue}"
    dirty = run(["git", "status", "--porcelain"], cwd=cwd).stdout.strip()
    if dirty:
        LOG.warning("issue #%s: uncommitted changes left behind", issue)

    run(["git", "fetch", "origin", branch], cwd=cwd, check=False)

    ahead = run(["git", "rev-list", "--count",
                 f"origin/{route.base_branch}..HEAD"], cwd=cwd).stdout.strip()
    if ahead == "0":
        return "_No commits produced._"

    # Keep the PR mergeable: fold origin/<base> into the branch, resolving
    # guardrail-only conflicts with the base's version. Guardrail syncs are
    # commits on this branch, so every fleet rule-roll would otherwise turn
    # every open PR unmergeable (hit live in production).
    conflict_note = ""
    run(["git", "fetch", "origin", route.base_branch], cwd=cwd, check=False)
    if run(["git", "merge", "--no-edit", f"origin/{route.base_branch}"],
           cwd=cwd, check=False).returncode != 0:
        conflicted = run(["git", "diff", "--name-only", "--diff-filter=U"],
                         cwd=cwd, check=False).stdout.split()
        harmless = set(GUARDRAIL_FILES) | {"docs/agent-journal.md", FLEET_RULES_DEST, INSTRUCT_FILE}
        if conflicted and set(conflicted) <= harmless:
            for f in conflicted:
                run(["git", "checkout", "--theirs", "--", f], cwd=cwd, check=False)
                run(["git", "add", "--", f], cwd=cwd, check=False)
            run(["git", "commit", "--no-edit"], cwd=cwd, check=False)
        else:
            run(["git", "merge", "--abort"], cwd=cwd, check=False)
            conflict_note = (f"\n_Branch has real conflicts with "
                             f"origin/{route.base_branch} in: "
                             f"{', '.join(conflicted) or '(unknown)'} — the PR "
                             f"will not merge until a human or an attended "
                             f"session reconciles._")

    push = run(["git", "push", "-u", "origin", branch], cwd=cwd, check=False)
    if push.returncode != 0:
        merge = run(["git", "merge", "--no-edit", f"origin/{branch}"],
                    cwd=cwd, check=False)
        if merge.returncode != 0:
            run(["git", "merge", "--abort"], cwd=cwd, check=False)
            return (f"**Could not push `{branch}`:** it diverged from the remote "
                    f"(attended pushes) and the merge conflicts. The local "
                    f"commits are safe in the worktree — reconcile in an "
                    f"attended session.")
        push = run(["git", "push", "-u", "origin", branch], cwd=cwd, check=False)
        if push.returncode != 0:
            return (f"**Push of `{branch}` failed** even after merging the "
                    f"remote:\n```\n{push.stderr.strip()[-400:]}\n```")

    existing = run(["gh", "pr", "list", "--repo", route.repo, "--head", branch,
                    "--json", "url"], cwd=cwd, check=False).stdout
    urls = json.loads(existing or "[]")
    if urls:
        return f"Updated PR: {urls[0]['url']}{conflict_note}"

    proc = run(["gh", "pr", "create", "--repo", route.repo, "--draft",
                "--base", route.base_branch, "--head", branch,
                "--title", f"agent: {cfg.repo}#{issue}",
                "--body", f"Automated work for {cfg.repo}#{issue}."],
               cwd=cwd, check=False)
    return (f"Opened PR: {proc.stdout.strip()}{conflict_note}"
            if proc.returncode == 0 else
            f"Pushed `{branch}` ({ahead} commits); PR creation failed."
            f"{conflict_note}")


# --------------------------------------------------------------------------- #
# stop / cancel
# --------------------------------------------------------------------------- #

# There is no external visibility into a job merely sitting in the live
# daemon's in-memory queue (print_status's own comment says so: "queued in
# daemon memory" is indistinguishable from "not active at all"). A one-shot
# file marker sidesteps that: --stop always drops one, execute() always
# checks for one at the top before doing any work. Time-bounded so a --stop
# call against an issue with nothing active can't silently eat a real,
# unrelated instruction commented minutes later.
STOP_MARKER_MAX_AGE_S = 900  # 15 min — generous vs. observed queue waits


def _stop_marker_path(cfg: Config, issue: int) -> Path:
    return cfg.expand(cfg.workspace_root) / "stop-requested" / str(issue)


def _stop_requested(cfg: Config, issue: int) -> bool:
    """True, and consumes the marker, if a --stop for this issue is still
    fresh. Always deletes what it finds, stale or not, so a marker is a
    one-shot signal for the very next thing that would launch — never a
    standing block on the issue."""
    p = _stop_marker_path(cfg, issue)
    if not p.exists():
        return False
    try:
        age = time.time() - p.stat().st_mtime
    finally:
        p.unlink(missing_ok=True)
    return age <= STOP_MARKER_MAX_AGE_S


def cmd_stop(cfg: Config, issue: int) -> int:
    """Stop issue N, whatever state it's in. Always drops the marker first
    (catches it if it's merely queued, or about to be re-triggered by a
    comment already in flight), then looks for a live process to signal
    (catches it if it's already running). One command, no need to know which
    state it's in — that distinction usually isn't visible from here anyway.
    """
    marker = _stop_marker_path(cfg, issue)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    print(f"marker set for issue #{issue}: the next not-yet-started run will "
          f"cancel cleanly (expires in {STOP_MARKER_MAX_AGE_S // 60} min if unused)")

    db = sqlite3.connect(cfg.expand(cfg.state_db))
    rows = db.execute(
        "SELECT route FROM sessions WHERE number = ? AND running = 1", (issue,)
    ).fetchall()
    if not rows:
        print(f"issue #{issue}: no live process right now — if it's queued, "
              f"the marker above will catch it before it launches; if it's "
              f"neither queued nor running, this is a harmless no-op.")
        return 0

    for (route_name,) in rows:
        route = next((r for r in cfg.routes if r.name == route_name), None)
        if not route:
            print(f"! route {route_name!r} not in current config — "
                  f"can't derive its session id to find the process")
            continue
        sid = session_id_for(cfg, route, issue)
        pids = run(["pgrep", "-f", sid], check=False).stdout.split()
        if not pids:
            print(f"issue #{issue} [{route_name}]: state.db says running, but no "
                  f"matching process — it may have just finished; check --status")
            continue
        for pid in pids:
            print(f"issue #{issue} [{route_name}]: pid {pid} — sending SIGTERM")
            run(["kill", "-TERM", pid], check=False)
        for _ in range(10):
            time.sleep(0.5)
            if not run(["pgrep", "-f", sid], check=False).stdout.split():
                print(f"issue #{issue} [{route_name}]: stopped")
                break
        else:
            for pid in run(["pgrep", "-f", sid], check=False).stdout.split():
                print(f"issue #{issue} [{route_name}]: pid {pid} still alive "
                      f"after 5s — sending SIGKILL")
                run(["kill", "-KILL", pid], check=False)
    return 0


# --------------------------------------------------------------------------- #
# supervisor
# --------------------------------------------------------------------------- #

@dataclass
class Job:
    item: BoardItem
    route: Route
    prompt: str
    resume: bool
    kind: str
    model: str = ""
    effort: str = ""


class Supervisor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.board = Board(cfg)
        self.state = State(cfg.expand(cfg.state_db))
        self.jobs: queue.Queue[Job | None] = queue.Queue()
        self.inflight: set[int] = set()
        self.inflight_lock = threading.Lock()
        self.log_dir = cfg.expand(cfg.workspace_root) / "logs"
        self.stop = threading.Event()

    def inflight_has(self, issue: int) -> bool:
        with self.inflight_lock:
            return issue in self.inflight

    # -- workers ----------------------------------------------------------- #

    def worker(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                return
            try:
                self.execute(job)
            except Exception:
                LOG.exception("issue #%s: job failed", job.item.issue)
                try:
                    self.park_blocked(job.item)
                    self.mark_run_state(job.item, "blocked")
                    notify_user(self.cfg, f"CRASHED — #{job.item.issue} ({job.route.name})",
                                job.item.title, sound=True)
                    post_comment(self.cfg, job.item.issue,
                                 f"@{self.cfg.project_owner} — agent crashed. "
                                 f"See the daemon log on the laptop.")
                except Exception:
                    LOG.exception("could not report failure")
            finally:
                self.state.upsert_session(job.item.issue, job.route.name, running=0)
                with self.inflight_lock:
                    self.inflight.discard(job.item.issue)
                self.jobs.task_done()

    def park_blocked(self, item: BoardItem) -> None:
        """Optionally move a card to the blocked column. Off by default:
        a parked card leaves every route, so comments stop reaching the agent
        until a human drags it back."""
        if self.cfg.blocked_status:
            self.board.set_status(item.item_id, self.cfg.blocked_status)

    def mark_run_state(self, item: BoardItem, value: str) -> None:
        """Best-effort write of working/done/blocked to the optional
        agent-status field. Run-state is cosmetic; never let it kill a run."""
        try:
            self.board.set_agent_status(item.item_id, value)
        except Exception:
            LOG.exception("could not write agent status %r", value)

    def execute(self, job: Job) -> None:
        cfg, item, route = self.cfg, job.item, job.route

        if _stop_requested(cfg, item.issue):
            LOG.info("issue #%s [%s]: stop requested before launch — skipping",
                     item.issue, route.name)
            notify_user(cfg, f"stopped — #{item.issue} ({route.name})",
                        "cancelled before it started, per --stop")
            post_comment(cfg, item.issue,
                         f"@{cfg.project_owner} — cancelled before it started, "
                         f"per your `--stop` request. Comment again to run it.")
            return

        # Two processes must never hold one conversation: if an attended
        # terminal is still open on this issue's session, refuse to launch
        # rather than corrupt the shared transcript.
        sid = session_id_for(cfg, route, item.issue)
        holder = run(["pgrep", "-f", sid], check=False).stdout.split()
        if holder:
            LOG.warning("issue #%s [%s]: session held by pid %s; not launching",
                        item.issue, route.name, ",".join(holder))
            notify_user(cfg, f"skipped — #{item.issue} ({route.name})",
                        "an attended terminal still holds this card's session; "
                        "close it and comment again", sound=True)
            post_comment(cfg, item.issue,
                         "Not starting a headless run: an attended session for this "
                         "card is still open on the laptop. Close that terminal, "
                         "then comment again.")
            return

        sess = self.state.session(item.issue, route.name)

        if route.mode == "worktree":
            try:
                cwd = ensure_worktree(cfg, route, item.issue)
            except GuardrailsMissing as exc:
                LOG.error("issue #%s [%s]: refusing to launch — missing guardrails: %s",
                          item.issue, route.name, ", ".join(exc.missing))
                self.park_blocked(item)
                self.mark_run_state(item, "blocked")
                notify_user(cfg, f"BLOCKED — #{item.issue} ({route.name})",
                            "route has no guardrails; see the issue comment", sound=True)
                missing_list = ", ".join(f"`{f}`" for f in exc.missing)
                post_comment(
                    cfg, item.issue,
                    f"@{cfg.project_owner} — refusing to launch: route `{route.name}` "
                    f"(`{route.repo}`) is missing guardrail file(s) on "
                    f"`origin/{route.base_branch}`: {missing_list}. An unattended agent "
                    f"can't run safely without them — see "
                    f"docs/ADDING-A-PROJECT.md in the agentd repo.")
                return
        else:
            cwd = cfg.expand(route.workdir)
        # `running` is the lease: set before launch, cleared in the worker's
        # finally. Anything still 1 at boot was interrupted mid-run.
        self.state.upsert_session(item.issue, route.name,
                                  running=1, workdir=str(cwd))
        self.mark_run_state(item, "working")

        started = time.time()
        result = run_agent(cfg, route, item.issue, prompt=job.prompt, cwd=cwd,
                           resume=job.resume, log_dir=self.log_dir,
                           model=job.model, effort=job.effort)
        elapsed = int(time.time() - started)

        self.state.upsert_session(item.issue, route.name, session_started=1,
                                  runs=sess["runs"] + 1, running=0)
        self.state.record_run(item.issue, route.name, job.kind, started,
                              elapsed, result.turns, result.cost_usd,
                              result.ok, result.blocked,
                              model=result.model, effort=result.effort)

        if not result.ok:
            stopped = _stop_requested(cfg, item.issue)
            self.park_blocked(item)
            self.mark_run_state(item, "blocked")
            if stopped:
                notify_user(cfg, f"stopped — #{item.issue} ({route.name})", item.title)
                post_comment(cfg, item.issue,
                             f"@{cfg.project_owner} — stopped per your `--stop` "
                             f"request after {elapsed}s. The session survives — "
                             f"comment again to resume.")
            else:
                notify_user(cfg, f"FAILED — #{item.issue} ({route.name})",
                            item.title, sound=True)
                post_comment(cfg, item.issue,
                             f"@{cfg.project_owner} — **agent run failed** after {elapsed}s.\n\n"
                             f"{result.text[-1200:]}")
            return

        footer = ""
        publish_failed = False
        if route.mode == "worktree":
            try:
                published = publish(cfg, route, item.issue, cwd)
            except Exception as exc:
                # A failed push is a delivery problem, not an agent crash:
                # the work sits committed in the worktree either way.
                LOG.exception("issue #%s: publish failed", item.issue)
                published = (f"**Publish failed** — work is committed locally "
                             f"in the worktree, but pushing/PR creation "
                             f"errored:\n```\n{str(exc)[-400:]}\n```")
            publish_failed = published.startswith(("**Publish failed",
                                                   "**Could not push",
                                                   "**Push of"))
            footer = "\n" + published
        blocked = result.blocked or publish_failed
        if blocked:
            self.park_blocked(item)
        self.mark_run_state(item, "blocked" if blocked else "done")
        if blocked:
            notify_user(cfg, f"BLOCKED — #{item.issue} ({route.name})",
                        f"{item.title} — needs you; see the issue comment", sound=True)
        else:
            notify_user(cfg, f"done — #{item.issue} ({route.name})",
                        f"{item.title} · {result.turns} turns · {elapsed}s · "
                        f"${result.cost_usd:.2f}", sound=True)

        attend_hint = ""
        if blocked:
            attend_hint = (f"\n@{cfg.project_owner} — needs you. Attended lane: "
                           f"`python3 ~/bin/agentd.py attend {item.issue}` — when "
                           f"done, just close the terminal; the board settles itself.")
        post_comment(
            cfg, item.issue,
            f"{result.text.strip()}\n\n---{footer}{attend_hint}\n"
            f"<sub>{route.name} · {job.kind} · {result.model}"
            f"{' · effort ' + result.effort if result.effort else ''}"
            f" · {result.turns} turns · {elapsed}s · "
            f"${result.cost_usd:.2f} · session "
            f"`{session_id_for(cfg, route, item.issue)[:8]}`</sub>"
        )

    # -- polling ----------------------------------------------------------- #

    def enqueue(self, job: Job) -> None:
        issue = job.item.issue
        sess = self.state.session(issue, job.route.name)
        if sess["runs"] >= self.cfg.max_runs_per_issue:
            LOG.error("issue #%s [%s]: hit max_runs_per_issue; refusing to run",
                      issue, job.route.name)
            self.park_blocked(job.item)
            self.mark_run_state(job.item, "blocked")
            notify_user(self.cfg, f"REFUSED — #{issue} ({job.route.name})",
                        "hit max_runs_per_issue; see the issue comment", sound=True)
            post_comment(self.cfg, issue,
                         f"@{self.cfg.project_owner} — refusing to run: issue has had "
                         f"{sess['runs']} agent runs on route `{job.route.name}` "
                         f"(max_runs_per_issue). Raise the limit or retire the card.")
            return
        with self.inflight_lock:
            if issue in self.inflight:
                LOG.debug("issue #%s already in flight; skipping", issue)
                return
            self.inflight.add(issue)
        self.jobs.put(job)

    def reconcile(self) -> None:
        """On boot, report sessions whose `running` lease was never cleared —
        the claude child died with the machine mid-run."""
        stuck = self.state.stuck_running()
        if not stuck:
            return

        routed = {i.issue: i for i in self.board.fetch()}
        for issue, route_name, runs in stuck:
            self.state.upsert_session(issue, route_name, running=0)
            route = next((r for r in self.cfg.routes if r.name == route_name), None)
            item = routed.get(issue)
            resumable = (self.cfg.reconcile_on_start == "resume" and route and item
                         and self.cfg.route_for(item.status) is route
                         and runs < self.cfg.max_runs_per_issue)
            LOG.warning("issue #%s [%s]: interrupted mid-run; %s", issue, route_name,
                        "re-enqueueing" if resumable else "noting on the issue")
            if resumable:
                self.enqueue(Job(item=item, route=route,
                                 prompt=RECONCILE_PROMPT.format(issue=issue),
                                 resume=True, kind="reconcile"))
                continue
            if item:
                self.mark_run_state(item, "blocked")
            notify_user(self.cfg, f"interrupted — #{issue} ({route_name})",
                        "a previous run died mid-flight; comment on the issue "
                        "to resume", sound=True)
            post_comment(
                self.cfg, issue,
                f"@{self.cfg.project_owner} — agent run was interrupted (laptop slept, shut down, or the "
                f"daemon was killed) after {runs} run(s) on route `{route_name}`. "
                f"The working directory and session are intact — comment on this "
                f"issue to resume from where it stopped."
            )

    def poll(self) -> None:
        items = self.board.fetch()

        # First boot: record where every card sits and which comments already
        # exist, and launch nothing. Otherwise cards long since parked in
        # routed columns would all fire at once.
        if not self.state.is_primed():
            for item in items:
                self.state.set_last_status(item.issue, item.status)
            comments = recent_comments(self.cfg)
            for comment in comments:
                self.state.mark_seen(comment["id"])
            self.state.mark_primed()
            LOG.info("first boot: recorded %d cards, primed %d comments; "
                     "launching nothing", len(items), len(comments))
            return

        # Default-scope stamping: new cards arrive unset; the default becomes
        # real board data within one poll. Hand-set values are never touched.
        if self.cfg.scope_field:
            for item in items:
                if item.scope is None:
                    route = self.cfg.route_for(item.status)
                    value = (route.scope if route and route.scope
                             else self.cfg.scope_default)
                    try:
                        self.board.set_default_scope(item.item_id, value)
                        LOG.info("issue #%s: scope defaulted to %s",
                                 item.issue, value)
                    except Exception:
                        LOG.exception("could not default scope on #%s", item.issue)

        routed: dict[int, tuple[BoardItem, Route]] = {}
        for item in items:
            route = self.cfg.route_for(item.status)
            if route:
                routed[item.issue] = (item, route)

        # 1. cards moved into a routed column (the handover gesture)
        for item in items:
            if self.inflight_has(item.issue):
                continue  # keep stored status stale so the move re-fires next poll
            prev = self.state.last_status(item.issue)
            if prev == item.status:
                continue
            route = self.cfg.route_for(item.status)
            prev_route = self.cfg.route_for(prev)
            if route and route.trigger_on_move and route is not prev_route:
                sess = self.state.session(item.issue, route.name)
                LOG.info("issue #%s: %s -> %s [route %s]",
                         item.issue, prev, item.status, route.name)
                notify_user(self.cfg, f"picked up — #{item.issue} ({route.name})",
                            f"{item.title} (card moved into {item.status})")
                directives = parse_directives(item.body)
                self.enqueue(Job(
                    item=item, route=route,
                    prompt=first_prompt(self.cfg, route, item),
                    resume=bool(sess["session_started"]),
                    kind="move" if not sess["session_started"] else "move/resume",
                    model=directives.get("model", ""),
                    effort=directives.get("effort", ""),
                ))
            self.state.set_last_status(item.issue, item.status)

        # 1b. attended sessions: notice them while open; when one's process
        # disappears, the human closed the terminal — that is the hand-back
        # event. Resume the same conversation headless to settle the board.
        try:
            self.watch_attended(routed)
        except Exception:
            LOG.exception("attended watch failed; continuing")

        # 2. new comments on routed cards: first comment starts the session,
        #    later ones are follow-up turns in it
        for comment in reversed(recent_comments(self.cfg)):  # oldest first
            issue = int(comment["issue_url"].rsplit("/", 1)[-1])
            body = comment.get("body") or ""
            author = comment["user"]["login"]
            if self.cfg.bot_login and author == self.cfg.bot_login:
                self.state.mark_seen(comment["id"])
                continue
            if issue not in routed or MARKER in body:
                # Not actionable now and never will be: replaying old comments
                # when a card is later routed would be chaos. The prompts tell
                # agents to read the full thread instead.
                self.state.mark_seen(comment["id"])
                continue
            if self.inflight_has(issue):
                continue  # NOT marked seen: picked up after the run finishes
            if not self.state.comment_is_new(comment["id"]):
                continue
            item, route = routed[issue]
            author = comment["user"]["login"]
            sess = self.state.session(issue, route.name)
            LOG.info("issue #%s [%s]: comment from @%s", issue, route.name, author)
            react_received(self.cfg, comment["id"])
            notify_user(self.cfg, f"picked up — #{issue} ({route.name})",
                        f"{item.title} (your comment)")
            if sess["session_started"]:
                prompt = FOLLOWUP_PROMPT.format(
                    issue=issue, author=author, comment=body.strip())
            else:
                prompt = first_prompt(self.cfg, route, item) + \
                    TRIGGER_COMMENT_SUFFIX.format(author=author, comment=body.strip())
            directives = parse_directives(item.body, body)
            self.enqueue(Job(item=item, route=route, prompt=prompt,
                             resume=bool(sess["session_started"]),
                             kind="comment",
                             model=directives.get("model", ""),
                             effort=directives.get("effort", "")))

    def watch_attended(self, routed: dict[int, tuple[BoardItem, Route]]) -> None:
        """Detect attended terminals by their session id in the process table;
        when one vanishes, launch a settle run in the same conversation so the
        board updates itself — the human's whole exit ritual is closing the
        terminal."""
        ps = run(["ps", "-axo", "command"], check=False, timeout=30).stdout
        routes = {r.name: r for r in self.cfg.routes}

        for number, route_name in self.state.started_sessions():
            route = routes.get(route_name)
            if not route or self.inflight_has(number):
                continue  # a pgrep hit while inflight is our own child
            sid = session_id_for(self.cfg, route, number)
            if sid in ps:
                self.state.attended_seen(sid, number, route_name)

        for sid, number, route_name in self.state.attended_rows():
            if sid in ps:
                continue  # still open
            self.state.attended_delete(sid)
            if self.inflight_has(number):
                continue
            entry = routed.get(number)
            route = routes.get(route_name)
            if not entry or not route or entry[1] is not route:
                LOG.info("attended session on #%s [%s] ended; card no longer "
                         "routed there — no settle run", number, route_name)
                continue
            LOG.info("attended session on #%s [%s] ended; settling the board",
                     number, route_name)
            notify_user(self.cfg, f"settling — #{number} ({route_name})",
                        "attended session closed; updating the board record")
            self.enqueue(Job(item=entry[0], route=route,
                             prompt=SETTLE_PROMPT.format(issue=number),
                             resume=True, kind="attended-settle"))

    def serve(self, once: bool = False) -> None:
        workers = [
            threading.Thread(target=self.worker, daemon=True, name=f"worker-{i}")
            for i in range(self.cfg.max_concurrent_agents)
        ]
        for thread in workers:
            thread.start()

        LOG.info("agentd up: board %s project #%s, routes [%s], %d worker(s), %ss poll",
                 self.cfg.repo, self.cfg.project_number,
                 ", ".join(r.name for r in self.cfg.routes),
                 self.cfg.max_concurrent_agents, self.cfg.poll_interval_seconds)

        try:
            self.reconcile()
        except Exception:
            LOG.exception("reconcile failed; continuing to poll")

        while not self.stop.is_set():
            try:
                self.poll()
            except Exception:
                LOG.exception("poll failed; will retry")
            if once:
                break
            self.stop.wait(self.cfg.poll_interval_seconds)

        self.jobs.join()
        for _ in workers:
            self.jobs.put(None)


# --------------------------------------------------------------------------- #

def attend(cfg: Config, issue: int) -> int:
    """Launch the attended session for an issue — route auto-detected from
    its existing sessions, disambiguated by the card's current column. Execs
    straight into claude; when the human closes the terminal, the daemon's
    attended-watcher settles the board automatically."""
    db = sqlite3.connect(cfg.expand(cfg.state_db))
    rows = db.execute("SELECT route FROM sessions WHERE number = ? AND "
                      "session_started = 1", (issue,)).fetchall()
    routes = {r.name: r for r in cfg.routes}
    candidates = [r for (r,) in rows if r in routes]

    route = routes[candidates[0]] if len(candidates) == 1 else None
    if route is None:
        # zero or several candidates: let the card's current column decide
        try:
            owner, name = cfg.repo.split("/")
            q = ('query($n: Int!) { repository(owner: "%s", name: "%s") {'
                 ' issue(number: $n) { projectItems(first: 5) { nodes {'
                 ' project { number } col: fieldValueByName(name: "%s") {'
                 ' ... on ProjectV2ItemFieldSingleSelectValue { name } } } } } } }'
                 % (owner, name, cfg.status_field))
            data = gh_graphql(q, n=issue)
            for node in data["data"]["repository"]["issue"]["projectItems"]["nodes"]:
                if node["project"]["number"] == cfg.project_number:
                    col_route = cfg.route_for((node.get("col") or {}).get("name"))
                    if col_route and (not candidates or col_route.name in candidates):
                        route = col_route
        except Exception:
            pass
    if route is None:
        if candidates:
            raise SystemExit(f"issue #{issue} has sessions on several routes "
                             f"({', '.join(candidates)}) and the column lookup "
                             f"failed — use: agentd.py --session <route> {issue}")
        raise SystemExit(f"issue #{issue} has no agent session yet — comment on "
                         f"its card (or move it into an agent column) so a "
                         f"headless run creates one, then attend.")

    sid = session_id_for(cfg, route, issue)
    cwd = (cfg.expand(cfg.workspace_root) / route.name / f"issue-{issue}"
           if route.mode == "worktree" else cfg.expand(route.workdir))
    if not cwd.exists():
        raise SystemExit(f"working directory {cwd} does not exist — has this "
                         f"issue ever run on route {route.name!r}?")
    print(f"attending #{issue} on route {route.name} — when you're done, just "
          f"close the terminal; the daemon settles the board automatically.")
    os.chdir(cwd)
    os.execvp("claude", ["claude", "--chrome", "--resume", sid, *instruct_args(cwd)])


def print_status(cfg: Config) -> int:
    """Live report from the state db and the process table — no board reads."""
    now = time.time()

    def ago(ts: float | None) -> str:
        if not ts:
            return "?"
        mins = int((now - ts) / 60)
        return f"{mins // 60}h{mins % 60:02d}m ago" if mins >= 60 else f"{mins}m ago"

    me = str(os.getpid())
    daemon = [l for l in run(["pgrep", "-fl", "bin/agentd.py"],
                             check=False).stdout.splitlines()
              if not l.startswith(me + " ")]
    # PIDs only (-l would echo full command lines, whose embedded multi-line
    # prompts would inflate the count)
    agents = run(["pgrep", "-f", "claude -p"], check=False).stdout.split()
    # Attended-lane terminals (the printed `claude --chrome --resume` recipe).
    # The daemon neither launches nor leases these; they are counted only.
    attended = run(["pgrep", "-f", "claude --chrome --resume"],
                   check=False).stdout.split()

    print(f"daemon   : {'running (pid ' + daemon[0].split()[0] + ')' if daemon else 'NOT RUNNING'}")
    print(f"headless : {len(agents)} daemon-spawned claude -p process(es)")
    print(f"attended : {len(attended)} claude --chrome --resume terminal(s) "
          f"(yours; untracked by the daemon)")

    db = sqlite3.connect(cfg.expand(cfg.state_db))
    running = db.execute(
        "SELECT number, route, workdir, updated_at FROM sessions "
        "WHERE running = 1 ORDER BY updated_at").fetchall()
    print(f"\nrunning now ({len(running)}):" if running else "\nrunning now: none")
    for number, route, workdir, ts in running:
        print(f"  #{number:<5} {route:<10} started {ago(ts):<10} {workdir}")
    if running and not agents:
        print("  ! lease(s) held but no claude process — a run died mid-flight;"
              " the next daemon start reconciles it")

    recent = db.execute(
        "SELECT number, route, runs, updated_at FROM sessions "
        "WHERE running = 0 ORDER BY updated_at DESC LIMIT 10").fetchall()
    if recent:
        print("\nlast sessions:")
        for number, route, runs, ts in recent:
            print(f"  #{number:<5} {route:<10} {runs} run(s), last {ago(ts)}")

    print("\nnote: a card with 👀 but not listed here is queued in daemon memory"
          " or already finished — the issue comment / Agent status has the outcome.")
    print("      --stop <issue> cancels either case: a queued job is dropped "
          "before it launches, a running one is sent SIGTERM (then SIGKILL).")
    return 0


TIDY_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  OWNER_TYPE(login: $owner) {
    projectV2(number: $number) {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          agent: fieldValueByName(name: "AGENT_FIELD") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          content { ... on Issue { number title state } }
        }
      }
    }
  }
}
"""


def tidy(cfg: Config) -> int:
    """Archive fully settled cards: issue CLOSED *and* agent-status done.
    Only-closed (never reviewed as done) and only-done (still open, in the
    review queue) stay visible. Archiving is reversible from the project's
    archived-items page."""
    if not cfg.agent_status_field:
        raise SystemExit("--tidy needs agent_status_field configured")
    query = (TIDY_QUERY.replace("OWNER_TYPE", cfg.owner_type)
                       .replace("AGENT_FIELD", cfg.agent_status_field))
    cursor: str | None = None
    archived = kept_closed = 0
    while True:
        variables = {"owner": cfg.project_owner, "number": cfg.project_number}
        if cursor:
            variables["cursor"] = cursor
        data = gh_graphql(query, **variables)
        items = data["data"][cfg.owner_type]["projectV2"]["items"]
        for node in items["nodes"]:
            content = node.get("content") or {}
            if content.get("state") != "CLOSED":
                continue
            if ((node.get("agent") or {}).get("name")) != "done":
                kept_closed += 1
                continue
            run(["gh", "project", "item-archive", str(cfg.project_number),
                 "--owner", cfg.project_owner, "--id", node["id"]])
            archived += 1
            print(f"archived #{content['number']}  {content.get('title', '')[:64]}")
        page = items["pageInfo"]
        if not page["hasNextPage"]:
            break
        cursor = page["endCursor"]
    print(f"\n{archived} card(s) archived (closed + done); "
          f"{kept_closed} closed-but-not-done left visible for review")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", default="~/.agentd/config.toml")
    parser.add_argument("--once", action="store_true",
                        help="poll a single time and exit (for testing)")
    parser.add_argument("--session", nargs=2, metavar=("ROUTE", "ISSUE"),
                        help="print an issue's session id and the command to "
                             "resume it attended (e.g. with the browser)")
    parser.add_argument("--status", action="store_true",
                        help="report the daemon and running agents, then exit")
    parser.add_argument("--tidy", action="store_true",
                        help="archive fully settled cards (issue CLOSED and "
                             "agent-status done), then exit")
    parser.add_argument("--stop", metavar="ISSUE",
                        help="stop issue N: cancels it if merely queued, "
                             "SIGTERMs (then SIGKILLs) it if already running")
    parser.add_argument("cmd", nargs="?", choices=["attend"],
                        help="attend <issue>: launch the attended session for "
                             "an issue, route auto-detected")
    parser.add_argument("issue", nargs="?")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(threadName)-9s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = Config.load(Path(os.path.expanduser(args.config)))

    if args.status:
        return print_status(cfg)

    if args.tidy:
        return tidy(cfg)

    if args.stop:
        if not args.stop.isdigit():
            raise SystemExit("usage: agentd.py --stop <issue-number>")
        return cmd_stop(cfg, int(args.stop))

    if args.cmd == "attend":
        if not args.issue or not args.issue.isdigit():
            raise SystemExit("usage: agentd.py attend <issue-number>")
        return attend(cfg, int(args.issue))

    if args.session:
        route_name, issue_str = args.session
        route = next((r for r in cfg.routes if r.name == route_name), None)
        if not route:
            raise SystemExit(f"no route {route_name!r}; "
                             f"have: {', '.join(r.name for r in cfg.routes)}")
        try:
            issue = int(issue_str)
        except ValueError:
            raise SystemExit(
                f"{issue_str!r} is not an issue number — usage: "
                f"agentd.py --session {route_name} <number>  (e.g. 677)")
        sid = session_id_for(cfg, route, issue)
        cwd = (cfg.expand(cfg.workspace_root) / route.name / f"issue-{issue}"
               if route.mode == "worktree" else cfg.expand(route.workdir))
        # Guard against the wrong route: sessions are per issue+route, so a
        # typo here prints a command for a conversation that never happened.
        db = sqlite3.connect(cfg.expand(cfg.state_db))
        rows = db.execute("SELECT route, runs FROM sessions WHERE number = ? "
                          "AND session_started = 1", (issue,)).fetchall()
        started = {r: n for r, n in rows}
        if route.name not in started:
            others = ", ".join(f"{r} ({n} runs)" for r, n in started.items())
            print(f"! issue #{issue} has NO session on route {route.name!r}."
                  + (f" It has sessions on: {others} — did you mean one of those?"
                     if others else " No route has run it yet."))
        if route.mode == "worktree" and not cwd.exists():
            print(f"! worktree {cwd} does not exist — nothing to resume there.")
        print(f"session : {sid}")
        print(f"cwd     : {cwd}")
        print(f"resume attended (same conversation, browser available):")
        print(f"  cd {cwd} && claude --chrome --resume {sid}")
        return 0

    supervisor = Supervisor(cfg)

    def shutdown(signum, _frame):
        LOG.info("signal %s: finishing in-flight work then exiting", signum)
        supervisor.stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    supervisor.serve(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
