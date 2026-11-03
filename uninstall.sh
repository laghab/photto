#!/usr/bin/env bash
# Removes a user (or PREFIX) install of photto. Your files are never touched.
#   curl -fsSL https://raw.githubusercontent.com/laghab/photto/main/uninstall.sh | bash
#   add --purge to also delete saved marks and the thumbnail cache
set -euo pipefail
PREFIX="${PREFIX:-$HOME/.local}"

rm -rf "$PREFIX/share/photto"
rm -f  "$PREFIX/bin/photto" "$PREFIX/share/applications/photto.desktop" \
       "$PREFIX/share/icons/hicolor/scalable/apps/photto.svg"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$PREFIX/share/applications" 2>/dev/null || true

if [ "${1:-}" = "--purge" ]; then
  rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/photto" "${XDG_CACHE_HOME:-$HOME/.cache}/photto"
  echo "✓ photto removed (including saved marks and cache)"
else
  echo "✓ photto removed (saved marks kept in ~/.local/state/photto; use --purge to delete them)"
fi
