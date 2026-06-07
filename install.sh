#!/usr/bin/env bash
#
# install.sh — Install claude-visualizer into a local virtualenv and create a
#              `claude-visualizer` shell alias so you can launch it from anywhere.
#
# Usage:   ./install.sh
#
# Idempotent: safe to run repeatedly. Re-running re-installs the app and
# refreshes the alias in place (it never duplicates the alias block).
#
set -euo pipefail

ALIAS_NAME="claude-visualizer"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
BEGIN_MARK="# >>> claude-visualizer alias (managed by install.sh) >>>"
END_MARK="# <<< claude-visualizer alias (managed by install.sh) <<<"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# --- Seed bundled monitors into ~/.claude-visualizer/monitors/ ---------------
# Copies every non-underscore *.py from the package monitors/ dir into the
# user's monitors directory, always overwriting software-delivered files so
# updates are picked up on re-install.  User files with DIFFERENT names are
# left untouched because the loop only writes basenames it ships.
# The _* pattern covers both __init__.py and single-underscore helpers.
seed_monitors() {
  local src_dir="$PROJECT_DIR/claude_visualizer/monitors"
  local dst_dir="$HOME/.claude-visualizer/monitors"
  mkdir -p "$dst_dir"
  for src in "$src_dir"/*.py; do
    local base
    base="$(basename "$src")"
    # Skip __init__.py and any underscore-prefixed helpers (_* matches both).
    case "$base" in
      _*) continue ;;
    esac
    cp -f "$src" "$dst_dir/$base"
  done
  info "Monitor files seeded into $dst_dir"
}

# Testable seam: invoke just the seed logic without triggering venv/pip work.
# Must be placed AFTER PROJECT_DIR and helpers are defined but BEFORE any
# venv or pip commands so the AC6 test never triggers a pip install.
if [ "${1:-}" = "--seed-monitors-only" ]; then
  seed_monitors
  exit 0
fi

# --- 1. Find a Python 3.11+ interpreter --------------------------------------
# The system `python3` may be older than 3.11 (e.g. RHEL 9 ships 3.9), so prefer
# a versioned interpreter and accept plain `python3` only if it is itself >= 3.11.
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    PYTHON="$candidate"
    break
  fi
done
[ -n "$PYTHON" ] || die "No Python 3.11+ interpreter found (tried python3.13/3.12/3.11/python3). Install Python 3.11+ first."
info "Using $("$PYTHON" --version 2>&1) ($(command -v "$PYTHON"))."

# --- 2. Create / reuse the virtualenv (idempotent) ---------------------------
if [ -x "$VENV_DIR/bin/python" ]; then
  info "Reusing existing virtualenv: $VENV_DIR"
else
  info "Creating virtualenv: $VENV_DIR"
  "$PYTHON" -m venv "$VENV_DIR"
fi

# --- 3. Install the app into the virtualenv (editable) -----------------------
# Editable (-e) so the alias/binary always reflects this working tree; a plain
# install copies into site-packages and can serve a stale build after edits.
info "Installing the app (pip, editable)…"
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python" -m pip install --quiet -e "$PROJECT_DIR"

APP_BIN="$VENV_DIR/bin/$ALIAS_NAME"
[ -x "$APP_BIN" ] || die "Expected console script missing after install: $APP_BIN"
info "App installed: $APP_BIN"

# --- 4. Seed bundled monitor files into ~/.claude-visualizer/monitors/ -------
seed_monitors

# --- 5. Create / refresh the `claude-visualizer` alias (idempotent) ----------
# Writes a marker-delimited block so re-running this script replaces (never
# duplicates) the alias, and an uninstall could remove exactly this block.
write_alias() {
  local rc="$1"
  touch "$rc"
  if grep -qF "$BEGIN_MARK" "$rc"; then
    awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
      $0==b {skip=1; next} $0==e {skip=0; next} !skip {print}
    ' "$rc" > "$rc.cv.tmp" && mv "$rc.cv.tmp" "$rc"
  fi
  # Ensure the file ends with a newline so our block can never concatenate onto
  # an existing last line that lacks one (e.g. an `export ...` with no newline).
  if [ -s "$rc" ] && [ -n "$(tail -c1 "$rc")" ]; then
    printf '\n' >> "$rc"
  fi
  {
    printf '%s\n' "$BEGIN_MARK"
    printf "alias %s='%s'\n" "$ALIAS_NAME" "$APP_BIN"
    printf '%s\n' "$END_MARK"
  } >> "$rc"
  info "alias '$ALIAS_NAME' written to $rc"
}

RC_FILES=()
[ -f "$HOME/.bashrc" ] && RC_FILES+=("$HOME/.bashrc")
[ -f "$HOME/.zshrc" ]  && RC_FILES+=("$HOME/.zshrc")
[ "${#RC_FILES[@]}" -gt 0 ] || RC_FILES+=("$HOME/.bashrc")

for rc in "${RC_FILES[@]}"; do write_alias "$rc"; done

# --- 5. Done -----------------------------------------------------------------
cat <<EOF

✅ Done — claude-visualizer is installed.

Activate the alias in your current shell:
    source ${RC_FILES[0]}
(or just open a new terminal)

Then run it:
    ${ALIAS_NAME}                                   # watches ~/.claude/projects
    ${ALIAS_NAME} --projects-root ~/.claude/projects
    ${ALIAS_NAME} --help                            # all options

Quit the TUI with 'q' (or Ctrl+C).
EOF
