#!/usr/bin/env bash
# Claude Code workflow bootstrap / parity check for this repository.
#
# Usage:
#   .claude/portable/bootstrap.sh [--check]   report parity, change nothing (default)
#   .claude/portable/bootstrap.sh --apply     install what is missing, never overwrite a differing file
#   .claude/portable/bootstrap.sh --apply --force
#                                             also replace differing user-level files (old copy is
#                                             saved under $CLAUDE_HOME/backups/portable-<timestamp>/)
#   --skip-plugin                             do not call the `claude plugin` CLI
#
# Does not log in, copy credentials, or edit ~/.claude/settings.json. Safe to re-run.
# Env: CLAUDE_HOME overrides the user config directory (default ~/.claude).

set -u

MODE=check
FORCE=0
SKIP_PLUGIN=0
for arg in "$@"; do
  case "$arg" in
    --check) MODE=check ;;
    --apply) MODE=apply ;;
    --force) FORCE=1 ;;
    --skip-plugin) SKIP_PLUGIN=1 ;;
    -h | --help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SRC/../.." && pwd)"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"
STAMP="$(date +%Y%m%d-%H%M%S)"
PLUGIN_ID="ecc@everything-claude-code"
PLUGIN_SOURCE="affaan-m/everything-claude-code"
FAILS=0
WARNS=0

ok() { printf '  OK    %s\n' "$1"; }
warn() { printf '  WARN  %s\n' "$1"; WARNS=$((WARNS + 1)); }
fail() { printf '  FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); }
section() { printf '\n[%s]\n' "$1"; }
# Same content, ignoring CRLF vs LF (git's autocrlf stores LF; a checkout or editor may use CRLF).
# Compares in memory only; neither file is modified.
same_content() { cmp -s <(tr -d '\r' <"$1") <(tr -d '\r' <"$2"); }

printf 'mode=%s force=%s repo=%s claude_home=%s\n' "$MODE" "$FORCE" "$REPO" "$CLAUDE_HOME"

# --- 1. Tooling ------------------------------------------------------------
section "tooling"
if command -v claude >/dev/null 2>&1; then
  ok "claude CLI: $(claude --version 2>/dev/null | head -1) (tested with 2.1.299)"
else
  fail "claude CLI not on PATH (install Claude Code, then run 'claude' once to log in)"
fi
for tool in uv python3 git; do
  if command -v "$tool" >/dev/null 2>&1; then ok "$tool present"; else fail "$tool missing"; fi
done

# --- 2. Plugin -------------------------------------------------------------
section "plugin $PLUGIN_ID"
if [ "$SKIP_PLUGIN" -eq 1 ]; then
  warn "skipped (--skip-plugin)"
elif ! command -v claude >/dev/null 2>&1; then
  warn "cannot check plugin without the claude CLI"
elif claude plugin list 2>/dev/null | grep -q "$PLUGIN_ID"; then
  ok "installed"
elif [ "$MODE" = apply ]; then
  echo "  installing via the supported plugin CLI (requires a logged-in account)"
  if claude plugin marketplace add "$PLUGIN_SOURCE" && claude plugin install "$PLUGIN_ID"; then
    ok "installed"
  else
    fail "install failed; log in with 'claude', then re-run"
  fi
else
  fail "not installed. Run: claude plugin marketplace add $PLUGIN_SOURCE && claude plugin install $PLUGIN_ID"
fi

# --- 3. User-level rules and CLAUDE.md (canonical copy lives in the repo) ---
section "user-level rules (canonical: .claude/portable/user -> $CLAUDE_HOME)"
backup_dir="$CLAUDE_HOME/backups/portable-$STAMP"
while IFS= read -r -d '' file; do
  rel="${file#"$SRC"/user/}"
  dest="$CLAUDE_HOME/$rel"
  if [ ! -e "$dest" ]; then
    if [ "$MODE" = apply ]; then
      mkdir -p "$(dirname "$dest")" && cp "$file" "$dest" && ok "installed $rel"
    else
      fail "missing $rel"
    fi
  elif same_content "$file" "$dest"; then
    ok "$rel"
  elif [ "$MODE" = apply ] && [ "$FORCE" -eq 1 ]; then
    mkdir -p "$backup_dir/$(dirname "$rel")" && cp "$dest" "$backup_dir/$rel" \
      && cp "$file" "$dest" && ok "replaced $rel (backup in $backup_dir)"
  else
    fail "differs $rel (diff -u \"$dest\" \"$file\"; reconcile, or re-run with --apply --force)"
  fi
done < <(find "$SRC/user" -type f -print0 | sort -z)

# Local-only rule files are not an error, but they are unmanaged instructions.
if [ -d "$CLAUDE_HOME/rules" ]; then
  while IFS= read -r -d '' dest; do
    rel="${dest#"$CLAUDE_HOME"/}"
    [ -e "$SRC/user/$rel" ] || warn "unmanaged local rule file: $rel (not in the repo; check it for conflicts)"
  done < <(find "$CLAUDE_HOME/rules" -type f -name '*.md' -print0 | sort -z)
fi

# --- 4. Duplicate / conflicting instruction sources -------------------------
section "duplicate instruction sources"
dups=0
if [ -d "$REPO/.claude/rules" ]; then
  while IFS= read -r -d '' file; do
    base="$(basename "$file")"
    if find "$SRC/user/rules" -name "$base" | grep -q .; then
      warn "$base exists in repo .claude/rules and user rules (loaded twice)"; dups=1
    fi
  done < <(find "$REPO/.claude/rules" -type f -name '*.md' -print0)
fi
if compgen -G "$CLAUDE_HOME/agents/*.md" >/dev/null; then
  warn "loose agent files in $CLAUDE_HOME/agents may duplicate ecc:* agents (policy: keep this directory empty)"; dups=1
fi
[ "$dups" -eq 0 ] && ok "none found"

# --- 5. Project settings (gitignored; generated from the tracked template) ---
section "project settings (.claude/settings.json)"
proj="$REPO/.claude/settings.json"
tmpl="$SRC/settings.project.template.json"
if [ ! -e "$proj" ]; then
  if [ "$MODE" = apply ]; then cp "$tmpl" "$proj" && ok "created from template"; else fail "missing (created from template by --apply)"; fi
else
  if python3 -I - "$proj" "$tmpl" <<'PY'
import json, sys
have, want = (json.load(open(p)) for p in sys.argv[1:3])
def cmds(d):
    return {h["command"] for ev in d.get("hooks", {}).values() for m in ev for h in m.get("hooks", [])}
missing = cmds(want) - cmds(have)
for c in sorted(missing):
    print("  missing hook command:", c)
sys.exit(1 if missing else 0)
PY
  then ok "all template hooks registered"; else fail "template hooks missing from .claude/settings.json (merge them by hand; file left untouched)"; fi
fi

# --- 6. Repository instructions, skills, hooks -------------------------------
section "repository discovery"
[ -s "$REPO/CLAUDE.md" ] && ok "CLAUDE.md present" || fail "CLAUDE.md missing"
skills=0; bad=0
for f in "$REPO"/.claude/skills/*/SKILL.md; do
  [ -e "$f" ] || continue
  skills=$((skills + 1))
  head -5 "$f" | grep -q '^name:' || { fail "no name: frontmatter in ${f#"$REPO"/}"; bad=1; }
done
[ "$skills" -gt 0 ] && [ "$bad" -eq 0 ] && ok "$skills project skills with valid frontmatter"
[ "$skills" -eq 0 ] && fail "no project skills found under .claude/skills"
for hook in "$REPO"/.claude/hooks/*.sh; do
  [ -e "$hook" ] || continue
  if [ -x "$hook" ]; then ok "executable ${hook#"$REPO"/}"
  elif [ "$MODE" = apply ]; then chmod +x "$hook" && ok "made executable ${hook#"$REPO"/}"
  else fail "not executable ${hook#"$REPO"/}"; fi
done

# --- 7. Per-account / per-machine settings (check only, never edited) --------
section "user settings.json expectations (check only)"
python3 -I - "$CLAUDE_HOME/settings.json" <<'PY'
import json, os, sys
path = sys.argv[1]
if not os.path.exists(path):
    print("  WARN  no settings.json; create it with the expected keys below")
    d = {}
else:
    d = json.load(open(path))
expect = [
    ("model", d.get("model"), "sonnet"),
    ("env.CLAUDE_CODE_SUBAGENT_MODEL", d.get("env", {}).get("CLAUDE_CODE_SUBAGENT_MODEL"), "sonnet"),
    ("env.MAX_THINKING_TOKENS", d.get("env", {}).get("MAX_THINKING_TOKENS"), "10000"),
    ("enabledPlugins[ecc@everything-claude-code]", d.get("enabledPlugins", {}).get("ecc@everything-claude-code"), True),
]
for key, got, want in expect:
    print(f"  {'OK   ' if got == want else 'WARN '} {key} = {got!r} (expected {want!r})")
PY

printf '\nsummary: %s failure(s), %s warning(s)\n' "$FAILS" "$WARNS"
if [ "$FAILS" -ne 0 ]; then
  [ "$MODE" = check ] && echo "run with --apply to fix what can be fixed safely"
  exit 1
fi
exit 0
