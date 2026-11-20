#!/usr/bin/env bash
# verify.sh — check every layer of an agentd install. Read-only; no side effects.
# Exit 0 if everything needed to run is present.

set -uo pipefail

CFG="${AGENTD_CONFIG:-$HOME/.agentd/config.toml}"
PY="${PYTHON:-python3}"
PASS=0; FAIL=0; SKIP=0

pass() { printf '  \033[32m✓\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
skip() { printf '  \033[33m-\033[0m %s\n' "$*"; SKIP=$((SKIP+1)); }
head() { printf '\n\033[1m%s\033[0m\n' "$*"; }

head "binaries"
for tool in git gh claude "$PY"; do
  if command -v "$tool" >/dev/null 2>&1; then
    pass "$tool → $(command -v "$tool")"
  else
    fail "$tool not on PATH"
  fi
done

if command -v "$PY" >/dev/null 2>&1; then
  if "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then
    pass "$PY is $("$PY" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))') (>= 3.11)"
  else
    fail "$PY is $("$PY" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))') — tomllib needs 3.11+"
  fi
fi

head "github auth"
if gh auth status >/dev/null 2>&1; then
  pass "gh authenticated as $(gh api user --jq .login 2>/dev/null || echo '?')"
  if gh auth status 2>&1 | grep -q project; then
    pass "'project' scope present"
  else
    fail "'project' scope missing — gh auth refresh -s project,read:project"
  fi
else
  fail "gh not authenticated"
fi

head "install"
[[ -x "$HOME/bin/agentd.py" ]] && pass "~/bin/agentd.py executable" \
                               || fail "~/bin/agentd.py missing or not executable"
[[ -f "$CFG" ]] && pass "config at $CFG" || fail "config missing at $CFG"
[[ -d "$HOME/agents/logs" ]] && pass "~/agents/logs" || fail "~/agents/logs missing"

if [[ ! -f "$CFG" ]]; then
  head "summary"; printf '  %d passed, %d failed — cannot continue without config\n\n' "$PASS" "$FAIL"
  exit 1
fi

# Config, routes and board are checked together in python: the [[route]]
# array does not survive a flat shell key=value extraction.
head "config, routes and board"
RESULT=$("$PY" - "$CFG" <<'EOF'
import json, os, subprocess, sys
try:
    import tomllib
except ImportError:
    print("FAIL config needs python 3.11+ (tomllib)"); sys.exit(0)

def emit(status, msg): print(f"{status} {msg}")

try:
    with open(sys.argv[1], "rb") as fh:
        cfg = tomllib.load(fh)
except Exception as exc:
    emit("FAIL", f"config does not parse: {exc}"); sys.exit(0)
emit("PASS", "config parses as TOML")

for key in ("repo", "project_owner", "project_number"):
    val = cfg.get(key, "")
    if not val or "CHANGEME" in str(val) or str(val).startswith("your"):
        emit("FAIL", f"{key} still a placeholder ({val!r})")
    else:
        emit("PASS", f"{key} = {val}")

routes = cfg.get("route", [])
if not routes:
    emit("FAIL", "no [[route]] blocks — nothing would ever run"); sys.exit(0)
emit("PASS", f"{len(routes)} route(s): " + ", ".join(r.get("name", "?") for r in routes))

home = os.path.expanduser("~")
wanted_columns = []
for r in routes:
    name = r.get("name", "?")
    wanted_columns += r.get("columns", [])
    workdir = os.path.expanduser(r.get("workdir", ""))
    if os.path.isdir(os.path.join(workdir, ".git")):
        emit("PASS", f"route {name}: workdir is a git repo ({workdir})")
    else:
        emit("FAIL", f"route {name}: workdir is not a git repo ({workdir})")
    if r.get("mode", "worktree") == "worktree":
        if not r.get("repo"):
            emit("FAIL", f"route {name}: worktree mode but no repo = \"owner/name\"")
        base = r.get("base_branch", "main")
        probe = subprocess.run(["git", "-C", workdir, "rev-parse", "--verify",
                                f"origin/{base}"], capture_output=True)
        if probe.returncode != 0:
            emit("FAIL", f"route {name}: origin/{base} not found — git fetch, or fix base_branch")
            continue
        emit("PASS", f"route {name}: origin/{base} resolves")

        # The daemon fails closed on these before launching a worktree run
        # too; audit them here as well so a skipped setup step is caught
        # before the route is added, not three days later on its first card.
        guardrail_files = ("CLAUDE.md", ".claude/settings.json",
                           "docs/agent-journal/README.md")
        missing = [f for f in guardrail_files
                   if subprocess.run(["git", "-C", workdir, "cat-file", "-e",
                                      f"origin/{base}:{f}"],
                                     capture_output=True).returncode != 0]
        if missing:
            emit("FAIL", f"route {name}: missing guardrail file(s) on origin/{base}: "
                          f"{', '.join(missing)}")
        else:
            emit("PASS", f"route {name}: guardrail files present on origin/{base}")

        if ".claude/settings.json" not in missing:
            settings = subprocess.run(["git", "-C", workdir, "show",
                                       f"origin/{base}:.claude/settings.json"],
                                      capture_output=True, text=True)
            try:
                deny = json.loads(settings.stdout).get("permissions", {}).get("deny", [])
            except Exception:
                deny = None
            if deny is None:
                emit("FAIL", f"route {name}: .claude/settings.json does not parse as JSON")
            elif not any("git push" in d for d in deny):
                emit("FAIL", f"route {name}: .claude/settings.json has no 'git push' deny entry")
            else:
                emit("PASS", f"route {name}: 'git push' denied in .claude/settings.json")

owner = cfg.get("project_owner", ""); number = cfg.get("project_number", 0)
otype = cfg.get("owner_type", "user"); sfield = cfg.get("status_field", "Status")
query = ("query($owner:String!,$number:Int!){ %s(login:$owner){ projectV2(number:$number){"
         " title field(name:\"%s\"){ ... on ProjectV2SingleSelectField { options { name } } } } } }"
         % (otype, sfield))
probe = subprocess.run(["gh", "api", "graphql", "-f", f"query={query}",
                        "-f", f"owner={owner}", "-F", f"number={number}"],
                       capture_output=True, text=True)
try:
    project = json.loads(probe.stdout)["data"][otype]["projectV2"]
    emit("PASS", f"project reachable: {project['title']}")
    options = {o["name"] for o in (project.get("field") or {}).get("options", [])}
    for col in wanted_columns:
        if col in options:
            emit("PASS", f"routed column present: '{col}'")
        else:
            emit("FAIL", f"routed column MISSING from board: '{col}'")
    blocked = cfg.get("blocked_status", "")
    if blocked and blocked not in options:
        emit("FAIL", f"blocked_status '{blocked}' is not a board column")
except Exception:
    emit("FAIL", f"cannot read project: {probe.stdout[:200]}{probe.stderr[:200]}")

asf = cfg.get("agent_status_field", "")
if asf:
    query2 = ("query($owner:String!,$number:Int!){ %s(login:$owner){ projectV2(number:$number){"
              " field(name:\"%s\"){ ... on ProjectV2SingleSelectField { options { name } } } } } }"
              % (otype, asf))
    probe2 = subprocess.run(["gh", "api", "graphql", "-f", f"query={query2}",
                             "-f", f"owner={owner}", "-F", f"number={number}"],
                            capture_output=True, text=True)
    try:
        fld = json.loads(probe2.stdout)["data"][otype]["projectV2"]["field"] or {}
        opts = {o["name"] for o in fld.get("options", [])}
        missing = {"working", "done", "blocked"} - opts
        if not fld:
            emit("FAIL", f"agent_status_field '{asf}' does not exist on the board")
        elif missing:
            emit("FAIL", f"agent_status_field '{asf}' is missing options: {sorted(missing)}")
        else:
            emit("PASS", f"agent_status_field '{asf}' present with working/done/blocked")
    except Exception:
        emit("FAIL", f"cannot read agent_status_field '{asf}': {probe2.stdout[:200]}")
EOF
)
while IFS= read -r line; do
  case "$line" in
    PASS\ *) pass "${line#PASS }" ;;
    FAIL\ *) fail "${line#FAIL }" ;;
    *) [[ -n "$line" ]] && skip "$line" ;;
  esac
done <<< "$RESULT"

head "service"
case "$(uname -s)" in
  Darwin)
    if launchctl list 2>/dev/null | grep -q com.local.agentd; then
      pass "LaunchAgent com.local.agentd loaded"
    else
      skip "LaunchAgent not loaded (fine if running by hand)"
    fi ;;
  Linux)
    if systemctl --user is-enabled agentd >/dev/null 2>&1; then
      pass "systemd user unit agentd enabled"
      if loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
        fail "LINGER IS ON — agents will run after logout; run: loginctl disable-linger $USER"
      else
        pass "linger off (agents stop when you log out)"
      fi
    else
      skip "systemd unit not enabled (fine if running by hand)"
    fi ;;
  *) skip "unrecognised OS" ;;
esac

head "summary"
printf '  %d passed, %d failed, %d skipped\n' "$PASS" "$FAIL" "$SKIP"
if (( FAIL == 0 )); then
  printf '  ready. dry run:  %s ~/bin/agentd.py --once -v\n\n' "$PY"
  exit 0
fi
printf '  fix the failures above before starting the daemon.\n\n'
exit 1
