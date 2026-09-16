#!/bin/sh
# Preflight for every sift hook. Exits 0 silently on any problem so a
# missing interpreter -- or a broken PATH -- can never break a session. Never
# triggers the macOS CLT install dialog: a bare /usr/bin/python3 without CLT is
# a stub that pops it.
HOOK="$1"; [ -n "$HOOK" ] || exit 0
DIR=$(cd "$(dirname "$0" 2>/dev/null)" 2>/dev/null && pwd) || exit 0
[ -n "$DIR" ] || exit 0
if [ "$(uname 2>/dev/null)" = "Darwin" ] && ! xcode-select -p >/dev/null 2>&1; then
  command -v python3 2>/dev/null | grep -qv '^/usr/bin/python3$' || exit 0
fi
[ -f "$DIR/hooks/$HOOK.py" ] || exit 0
PY=$(command -v python3 2>/dev/null) || exit 0
[ -n "$PY" ] || exit 0
"$PY" -I -S -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null || exit 0
exec "$PY" -I -S "$DIR/hooks/$HOOK.py"
