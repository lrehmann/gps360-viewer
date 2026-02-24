#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="GPS360 Viewer.app"
VERSION="${1:-0.3.0}"
DIST_DIR="$ROOT_DIR/dist"
APP_ZIP="$DIST_DIR/gps360-viewer-macos-apple-silicon-v${VERSION}.zip"
SRC_ZIP="$DIST_DIR/gps360-source-v${VERSION}.zip"

mkdir -p "$DIST_DIR"
rm -f "$APP_ZIP" "$APP_ZIP.sha256" "$SRC_ZIP" "$SRC_ZIP.sha256"

"$ROOT_DIR/scripts/build_macos_app.sh"

/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$ROOT_DIR/$APP_NAME" "$APP_ZIP"

(
  cd "$ROOT_DIR"
  /usr/bin/zip -rq "$SRC_ZIP" \
    README.md \
    LICENSE \
    pyproject.toml \
    launch_gps360_gui.command \
    logo.png \
    aibzy-1qsp1.icns \
    gps360 \
    macos \
    scripts \
    .gitignore \
    -x "gps360/__pycache__/*" "gps360/*.pyc" "GPS360 Viewer.app/*" "captures/*" "dist/*" "*.log" "*.tmp" ".DS_Store" "*/.DS_Store"
)

/usr/bin/shasum -a 256 "$APP_ZIP" > "$APP_ZIP.sha256"
/usr/bin/shasum -a 256 "$SRC_ZIP" > "$SRC_ZIP.sha256"

printf "Release artifacts:\n"
printf "  %s\n" "$APP_ZIP" "$APP_ZIP.sha256" "$SRC_ZIP" "$SRC_ZIP.sha256"
