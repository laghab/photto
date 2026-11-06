#!/usr/bin/env bash
# SIFT installer: puts SIFT in your app menu and on your PATH (no root needed).
#
#   curl -fsSL https://raw.githubusercontent.com/laghab/sift/main/install.sh | bash
#   ./install.sh            (from a git checkout)
#
# Installs into ~/.local by default. Set PREFIX=/usr/local (and run with sudo) for a system install.
set -euo pipefail

REPO="laghab/sift"
REF="${SIFT_REF:-main}"
PREFIX="${PREFIX:-$HOME/.local}"
LIB="$PREFIX/share/sift"
BIN="$PREFIX/bin"
APPS="$PREFIX/share/applications"
ICONS="$PREFIX/share/icons/hicolor/scalable/apps"

say()  { printf '\033[1;33m›\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null || die "python3 is required"
python3 - <<'EOF' || die "python 3.10 or newer is required"
import sys; sys.exit(sys.version_info < (3, 10))
EOF

# Where are the files? A checkout next to this script, or download a tarball.
SRC=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/photto.py" ]; then
  SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  say "downloading SIFT ($REF)…"
  curl -fsSL "https://github.com/$REPO/archive/$REF.tar.gz" | tar -xz -C "$TMP" --strip-components=1
  SRC="$TMP"
fi

say "installing to $PREFIX"
rm -rf "$LIB"
mkdir -p "$LIB" "$BIN" "$APPS" "$ICONS"
cp "$SRC/photto.py" "$LIB/"
cp "$SRC/sift_ocr.py" "$LIB/"
cp -r "$SRC/static" "$LIB/"
cp "$SRC/LICENSE" "$LIB/" 2>/dev/null || true

rm -f "$BIN/sift"   # may be an old symlink; never write through it
cat > "$BIN/sift" <<EOF
#!/bin/sh
exec python3 "$LIB/photto.py" "\$@"
EOF
chmod +x "$BIN/sift"

sed "s|^Exec=sift|Exec=$BIN/sift|" "$SRC/data/sift.desktop" > "$APPS/sift.desktop"
cp "$SRC/data/sift.svg" "$ICONS/sift.svg"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache  >/dev/null && gtk-update-icon-cache -q -t "$PREFIX/share/icons/hicolor" 2>/dev/null || true
ok "SIFT installed: find it in your app menu, or run 'sift' in any folder"

case ":$PATH:" in *":$BIN:"*) ;; *) say "note: $BIN is not on your PATH (only matters for the terminal command)";; esac

# Optional extras
missing=()
python3 -c 'import gi; gi.require_version("WebKit2","4.1")' 2>/dev/null \
  || python3 -c 'import gi; gi.require_version("WebKit2","4.0")' 2>/dev/null || missing+=("native-window")
command -v ffmpeg    >/dev/null || missing+=("ffmpeg")
command -v pdftoppm  >/dev/null || missing+=("poppler")
command -v magick    >/dev/null || command -v vips >/dev/null || missing+=("imagemagick")
command -v gio       >/dev/null || missing+=("gio")

if [ ${#missing[@]} -gt 0 ]; then
  echo
  say "optional extras not found: ${missing[*]}"
  echo "  native-window → own app window instead of a browser window"
  echo "  ffmpeg        → video thumbnails · poppler → PDF pages · imagemagick → HEIC/RAW photos"
  if   command -v pacman >/dev/null; then echo "  sudo pacman -S --needed python-gobject webkit2gtk-4.1 ffmpeg poppler imagemagick"
  elif command -v apt    >/dev/null; then echo "  sudo apt install python3-gi gir1.2-webkit2-4.1 ffmpeg poppler-utils imagemagick"
  elif command -v dnf    >/dev/null; then echo "  sudo dnf install python3-gobject webkit2gtk4.1 ffmpeg poppler-utils ImageMagick"
  elif command -v zypper >/dev/null; then echo "  sudo zypper install python3-gobject typelib-1_0-WebKit2-4_1 ffmpeg poppler-tools ImageMagick"
  fi
fi
