#!/usr/bin/env bash
# bootstrap.sh — install agentd's local files. Idempotent; safe to re-run.
#
# Does NOT: authenticate gh, edit your board, install the service, or touch
# your target repo. Those need decisions; see START_HERE.md.

set -euo pipefail

PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${HOME}/bin"
CFG_DIR="${HOME}/.agentd"
WORKSPACE="${HOME}/agents"

say()  { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

printf '\nagentd bootstrap\n────────────────\n'

# ---------------------------------------------------------------- preflight --
say "checking prerequisites"

command -v git    >/dev/null || die "git not found"
command -v gh     >/dev/null || die "gh not found — https://cli.github.com"
command -v claude >/dev/null || die "claude not found — https://code.claude.com"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || die "python3 not found"
"$PY" - <<'EOF' || exit 1
import sys
if sys.version_info < (3, 11):
    sys.exit(f"python 3.11+ required for tomllib (found {sys.version.split()[0]})")
EOF
ok "git, gh, claude, $($PY --version)"

if ! gh auth status >/dev/null 2>&1; then
  die "gh is not authenticated — run: gh auth login"
fi

if gh auth status 2>&1 | grep -q "project"; then
  ok "gh has the 'project' scope"
else
  warn "gh is MISSING the 'project' scope — Projects v2 reads will fail."
  warn "run this yourself, then re-run bootstrap:"
  warn "    gh auth refresh -s project,read:project"
fi

# ------------------------------------------------------------------ install --
printf '\n'
say "installing files"

mkdir -p "$BIN_DIR" "$CFG_DIR" "$WORKSPACE/logs"
install -m 755 "$PACK_DIR/src/agentd.py" "$BIN_DIR/agentd.py"
ok "$BIN_DIR/agentd.py"
install -m 644 "$PACK_DIR/templates/FLEET-RULES.md" "$CFG_DIR/FLEET-RULES.md"
ok "$CFG_DIR/FLEET-RULES.md (fleet-wide agent rules, synced into every worktree)"

if [[ -f "$CFG_DIR/config.toml" ]]; then
  warn "$CFG_DIR/config.toml exists — left untouched"
  cp "$PACK_DIR/config/config.example.toml" "$CFG_DIR/config.example.toml"
  say "reference copy written to $CFG_DIR/config.example.toml"
else
  cp "$PACK_DIR/config/config.example.toml" "$CFG_DIR/config.toml"
  ok "$CFG_DIR/config.toml (from example — EDIT IT)"
fi

ok "$WORKSPACE/logs"

case "$(uname -s)" in
  Darwin) SVC="config/com.local.agentd.plist → ~/Library/LaunchAgents/" ;;
  Linux)  SVC="config/agentd.service → ~/.config/systemd/user/" ;;
  *)      SVC="(unrecognised OS — install the service by hand)" ;;
esac

# ------------------------------------------------------------------ summary --
cat <<EOF

next
────
  1. edit ${CFG_DIR}/config.toml — confirm each [[route]]'s workdir and repo;
     the routed columns must match the board's Status options exactly
  2. copy templates/ into each worktree route's repo (see START_HERE.md step 5)
  3. dry run — the first poll primes and launches no agents:
       ${PY} ${BIN_DIR}/agentd.py --once -v
  4. run scripts/verify.sh to check every layer
  5. install the service: ${SVC}

  full instructions: ${PACK_DIR}/START_HERE.md
  design rationale:  ${PACK_DIR}/docs/DESIGN.md

EOF
