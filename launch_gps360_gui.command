#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_PATH="$ROOT_DIR/GPS360 Viewer.app"

if [ ! -d "$APP_PATH" ]; then
  "$ROOT_DIR/scripts/build_macos_app.sh"
fi

open "$APP_PATH"
