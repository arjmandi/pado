#!/usr/bin/env python3
"""
weekly-report.py — what happened last week, plus the live board state.

Sources, deliberately independent of board hygiene:
  1. Issues CLOSED in the week's Mon-Sun window (repo search — includes cards
     already archived off the board).
  2. Cards recorded in that week's `week NN` column, if it still exists —
     a manual done-ritual may keep some of these open.
  3. The board right now: working / awaiting review (done, open) / blocked /
     queued (routed column, no agent status yet).

Writes reports/weekly/<YYYY>-W<NN>.md in the planning repo, commits, pushes,
and raises a macOS notification. Scheduled Mondays 09:00 via
config/com.local.agentd.weekly.plist; run by hand with --date to rebuild any
week (default: the week containing today-minus-7-days).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

MARKER = "<!-- agentd -->"


def run(cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(map(str, cmd))}\n{p.stderr.strip()}")
    return p.stdout


def closed_issues(repo: str, start: dt.date, end: dt.date) -> list[dict]:
    out = run(["gh", "api", "-X", "GET", "search/issues", "--paginate",
               "-f", f"q=repo:{repo} is:issue closed:{start}..{end}",
               "-F", "per_page=100", "--jq", ".items[]"])
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def last_agent_outcome(repo: str, number: int) -> str:
    comments = json.loads(run([
        "gh", "api", f"repos/{repo}/issues/{number}/comments?per_page=100"]) or "[]")
    for c in reversed(comments):
        if MARKER in (c.get("body") or ""):
            text = c["body"].replace(MARKER, "").strip()
            text = re.sub(r"<sub>.*?</sub>", "", text, flags=re.S)
            first = next((l for l in text.splitlines() if l.strip()), "")
            return first.strip()[:180]
    return ""


BOARD_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  OWNER_TYPE(login: $owner) {
    projectV2(number: $number) {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          status: fieldValueByName(name: "STATUS_FIELD") {
            ... on ProjectV2ItemFieldSingleSelectValue { name } }
          agent: fieldValueByName(name: "AGENT_FIELD") {
            ... on ProjectV2ItemFieldSingleSelectValue { name } }
          scope: fieldValueByName(name: "Scope") {
            ... on ProjectV2ItemFieldSingleSelectValue { name } }
          content { ... on Issue { number title state url } }
        }
      }
    }
  }
}
"""


def board_items(cfg: dict) -> list[dict]:
    query = (BOARD_QUERY
             .replace("OWNER_TYPE", cfg.get("owner_type", "user"))
             .replace("STATUS_FIELD", cfg.get("status_field", "Status"))
             .replace("AGENT_FIELD", cfg.get("agent_status_field", "Agent status")))
    items, cursor = [], None
    while True:
        cmd = ["gh", "api", "graphql", "-f", f"query={query}",
               "-f", f"owner={cfg['project_owner']}",
               "-F", f"number={cfg['project_number']}"]
        if cursor:
            cmd += ["-f", f"cursor={cursor}"]
        data = json.loads(run(cmd))
        page = data["data"][cfg.get("owner_type", "user")]["projectV2"]["items"]
        for n in page["nodes"]:
            c = n.get("content") or {}
            if "number" not in c:
                continue
            items.append({
                "number": c["number"], "title": c["title"], "state": c["state"],
                "url": c["url"],
                "column": (n.get("status") or {}).get("name"),
                "agent": (n.get("agent") or {}).get("name"),
                "scope": (n.get("scope") or {}).get("name") or "contract",
            })
        if not page["pageInfo"]["hasNextPage"]:
            return items
        cursor = page["pageInfo"]["endCursor"]


def issue_scope(repo: str, number: int) -> str:
    """Scope of a closed issue via its project item — includeArchived, so
    tidied cards still report correctly. Unset counts as contract."""
    owner, name = repo.split("/")
    q = ('query($n: Int!) { repository(owner: "%s", name: "%s") { issue(number: $n) {'
         ' projectItems(first: 5, includeArchived: true) { nodes {'
         ' project { number } scope: fieldValueByName(name: "Scope") {'
         ' ... on ProjectV2ItemFieldSingleSelectValue { name } } } } } } }'
         % (owner, name))
    try:
        d = json.loads(run(["gh", "api", "graphql", "-f", f"query={q}",
                            "-F", f"n={number}"]))
        for node in d["data"]["repository"]["issue"]["projectItems"]["nodes"]:
            scope = (node.get("scope") or {}).get("name")
            if scope:
                return scope
    except SystemExit:
        pass
    return "contract"


def notify(title: str, message: str) -> None:
    script = ('on run argv\n  display notification (item 2 of argv) '
              'with title (item 1 of argv) sound name "Glass"\nend run')
    subprocess.run(["osascript", "-e", script, title, message],
                   capture_output=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--config", default="~/.agentd/config.toml")
    ap.add_argument("--date", help="any YYYY-MM-DD inside the target week "
                                   "(default: 7 days ago)")
    ap.add_argument("--repo-dir", default="~/workspace/pado")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    with open(os.path.expanduser(args.config), "rb") as fh:
        cfg = tomllib.load(fh)
    repo = cfg["repo"]
    routed_columns = {col for r in cfg.get("route", []) for col in r["columns"]}

    anchor = (dt.date.fromisoformat(args.date) if args.date
              else dt.date.today() - dt.timedelta(days=7))
    monday = anchor - dt.timedelta(days=anchor.weekday())
    sunday = monday + dt.timedelta(days=6)
    year, week, _ = monday.isocalendar()

    closed = sorted(closed_issues(repo, monday, sunday),
                    key=lambda i: i["closed_at"])
    closed_numbers = {i["number"] for i in closed}

    # Per-issue agent effort from the daemon's run ledger: exact wall time and
    # the CLI's token-priced cost per run, summed over the week's window.
    import sqlite3, time as _time
    start_ts = _time.mktime(monday.timetuple())
    end_ts = _time.mktime((sunday + dt.timedelta(days=1)).timetuple())
    effort: dict[int, dict] = {}
    try:
        db = sqlite3.connect(os.path.expanduser(cfg.get("state_db",
                                                        "~/.agentd/state.db")))
        for num, n, secs, cost in db.execute(
                "SELECT number, COUNT(*), SUM(seconds), SUM(cost) FROM runs "
                "WHERE started_at >= ? AND started_at < ? GROUP BY number",
                (start_ts, end_ts)):
            effort[int(num)] = {"runs": n, "seconds": int(secs or 0),
                                "cost": float(cost or 0.0)}
    except Exception:
        pass

    def spend(num: int) -> str:
        e = effort.get(num)
        if not e:
            return ""
        mins = round(e["seconds"] / 60)
        return f" · {e['runs']} run(s), {mins}m agent time, ${e['cost']:.2f}"
    board = board_items(cfg)
    week_cards = [b for b in board if b["column"] == f"week {week}"
                  and b["number"] not in closed_numbers]
    open_items = [b for b in board if b["state"] == "OPEN"]
    working = [b for b in open_items if b["agent"] == "working"]
    review = [b for b in open_items if b["agent"] == "done"]
    blocked = [b for b in open_items if b["agent"] == "blocked"]
    queued = [b for b in open_items
              if b["column"] in routed_columns and not b["agent"]]

    L = [f"# Week {week}, {year} — {monday} → {sunday}", "",
         f"_Generated {dt.date.today()}. Sources: issues closed in the window "
         f"(archive-proof) + the `week {week}` column ritual + the live board._",
         "",
         "_Cost figures are the Claude CLI's token-priced value per run "
         "(API-equivalent; consumption against a Claude subscription seat, not a "
         "per-run invoice). Time is daemon-measured wall time of headless "
         "runs. Attended sessions are not metered. Full semantics:_ "
         "[COST-ACCOUNTING.md](COST-ACCOUNTING.md)",
         ""]

    by_scope = {"contract": [], "personal": []}
    for i in closed:
        by_scope.setdefault(issue_scope(repo, i["number"]), []).append(i)
    for scope_name, label in (("contract", "Contract"),
                              ("personal", "Personal")):
        rows = by_scope.get(scope_name, [])
        L.append(f"## Done — {label} — closed this week ({len(rows)})\n")
        if not rows:
            L.append("- none\n")
        for i in rows:
            outcome = last_agent_outcome(repo, i["number"])
            line = (f"- **#{i['number']}** {i['title']} — closed "
                    f"{i['closed_at'][:10]}{spend(i['number'])}")
            if outcome:
                line += f"\n  - outcome: {outcome}"
            L.append(line)
        L.append("")

    if week_cards:
        L.append(f"\n## Done — recorded in the `week {week}` column, "
                 f"issue still open ({len(week_cards)})\n")
        for b in sorted(week_cards, key=lambda x: x["number"]):
            L.append(f"- **#{b['number']}** {b['title']}")

    if effort:
        title_map = {i["number"]: i["title"] for i in closed}
        scope_map = {}
        for s, rows in by_scope.items():
            for i in rows:
                scope_map[i["number"]] = s
        for b in board:
            title_map.setdefault(b["number"], b["title"])
            scope_map.setdefault(b["number"], b.get("scope") or "contract")
        totals: dict[str, list] = {}
        for num, e in effort.items():
            s = scope_map.get(num, "contract")
            totals.setdefault(s, [0, 0.0])
            totals[s][0] += e["seconds"]
            totals[s][1] += e["cost"]
        L.append(f"\n## Agent effort this week ({len(effort)} issues touched)\n")
        for s, label in (("contract", "Contract"),
                         ("personal", "Personal")):
            secs, cost = totals.get(s, [0, 0.0])
            L.append(f"- **{label}**: {secs // 3600}h{(secs % 3600) // 60:02d}m "
                     f"agent time · ${cost:.2f}")
        L.append("\nTop issues by cost (open ones included):\n")
        for num, e in sorted(effort.items(), key=lambda kv: -kv[1]["cost"])[:10]:
            L.append(f"- **#{num}** {title_map.get(num, '')[:55]} — "
                     f"{e['runs']} run(s), {round(e['seconds'] / 60)}m, "
                     f"${e['cost']:.2f}")
        L.append("")

    def section(title, rows, show_column=True):
        L.append(f"\n## {title} ({len(rows)})\n")
        if not rows:
            L.append("- none")
        for b in sorted(rows, key=lambda x: (x["column"] or "", x["number"])):
            col = f" `{b['column']}`" if show_column else ""
            tag = " · _personal_" if b.get("scope") == "personal" else ""
            L.append(f"- **#{b['number']}**{col} {b['title']}{tag}")

    section("In progress right now", working)
    section("Awaiting your review (agent done, issue open)", review)
    section("Blocked — needs you", blocked)

    # Queue grouped per column, in route-config order:
    # the planning column (In plan), then each agent: * column separately.
    L.append(f"\n## Queue — routed, no agent run yet ({len(queued)})\n")
    ordered_cols = [col for r in cfg.get("route", []) for col in r["columns"]]
    for col in ordered_cols:
        rows = sorted((b for b in queued if b["column"] == col),
                      key=lambda x: x["number"])
        L.append(f"### {col} ({len(rows)})\n")
        if not rows:
            L.append("- empty\n")
        else:
            L.extend(f"- **#{b['number']}** {b['title']}"
                     + (" · _personal_" if b.get("scope") == "personal" else "")
                     for b in rows)
            L.append("")

    repo_dir = Path(os.path.expanduser(args.repo_dir))
    out = repo_dir / "reports" / "weekly" / f"{year}-W{week:02d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"wrote {out}")

    run(["git", "add", str(out.relative_to(repo_dir))], cwd=repo_dir)
    if run(["git", "status", "--porcelain", str(out.relative_to(repo_dir))],
           cwd=repo_dir).strip():
        # Path-scoped commit: anything else sitting staged in the planning
        # repo on a Monday morning must not be swept into this commit.
        run(["git", "commit", "-q", "-m",
             f"Weekly report {year}-W{week:02d}\n\n"
             f"Generated by agentd/scripts/weekly-report.py",
             "--", str(out.relative_to(repo_dir))], cwd=repo_dir)
        if not args.no_push:
            run(["git", "push", "-q", "origin", "HEAD"], cwd=repo_dir)
    notify(f"agentd — weekly report W{week}",
           f"{len(closed)} closed · {len(review)} awaiting review · "
           f"{len(blocked)} blocked · {len(queued)} queued")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
