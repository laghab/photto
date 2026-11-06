#!/usr/bin/env bash
# Removes a user (or PREFIX) install of SIFT. Your files are never touched.
#   curl -fsSL https://raw.githubusercontent.com/laghab/sift/main/uninstall.sh | bash
#   add --purge to also delete saved marks and the thumbnail cache
set -euo pipefail
PREFIX="${PREFIX:-$HOME/.local}"

rm -rf "$PREFIX/share/sift"
rm -f  "$PREFIX/bin/sift" "$PREFIX/share/applications/sift.desktop" \
       "$PREFIX/share/icons/hicolor/scalable/apps/sift.svg"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$PREFIX/share/applications" 2>/dev/null || true

if [ "${1:-}" = "--purge" ]; then
  rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/sift" "${XDG_CACHE_HOME:-$HOME/.cache}/sift"
  echo "✓ SIFT removed (including saved marks and cache)"
else
  echo "✓ SIFT removed (saved marks kept in ~/.local/state/sift; use --purge to delete them)"
fi
