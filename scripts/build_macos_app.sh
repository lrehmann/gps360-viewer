#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="$(command -v python3)"
SWIFT_SRC="$ROOT_DIR/macos/GPS360Viewer.swift"
APP_NAME="GPS360 Viewer"
APP_PATH="$ROOT_DIR/$APP_NAME.app"
APP_CONTENTS="$APP_PATH/Contents"
APP_MACOS="$APP_CONTENTS/MacOS"
APP_RESOURCES="$APP_CONTENTS/Resources"
APP_PYTHON="$APP_RESOURCES/python"
APP_LIB="$APP_RESOURCES/lib"
EXEC_NAME="gps360-viewer"
EXEC_PATH="$APP_MACOS/$EXEC_NAME"
PLIST_PATH="$APP_CONTENTS/Info.plist"
CONFIG_PATH="$APP_RESOURCES/gps360-config.json"
ICON_PATH="$APP_RESOURCES/GPS360Viewer.icns"
TMP_ICON_ROOT="$(mktemp -d /tmp/gps360-iconset-XXXXXX)"
ICONSET_DIR="$TMP_ICON_ROOT/GPS360Viewer.iconset"
CUSTOM_ICON="$ROOT_DIR/aibzy-1qsp1.icns"
LOGO_ICON="$ROOT_DIR/logo.png"
BUNDLED_LIBUSB="/opt/homebrew/lib/libusb-1.0.dylib"

cleanup() {
  rm -rf "$TMP_ICON_ROOT"
}
trap cleanup EXIT

if [ ! -f "$SWIFT_SRC" ]; then
  echo "Missing Swift source at $SWIFT_SRC" >&2
  exit 1
fi

rm -rf "$APP_PATH"
mkdir -p "$APP_MACOS" "$APP_RESOURCES" "$APP_PYTHON" "$APP_LIB" "$ICONSET_DIR"

/usr/bin/swiftc \
  "$SWIFT_SRC" \
  -framework Cocoa \
  -framework WebKit \
  -o "$EXEC_PATH"

cat > "$CONFIG_PATH" <<EOF
{
  "python_bin": "$PYTHON_BIN",
  "host": "127.0.0.1",
  "port": 8765
}
EOF

cp -R "$ROOT_DIR/gps360" "$APP_PYTHON/"
find "$APP_PYTHON" -type d -name "__pycache__" -prune -exec rm -rf {} +

if [ -f "$BUNDLED_LIBUSB" ]; then
  cp -L "$BUNDLED_LIBUSB" "$APP_LIB/libusb-1.0.dylib"
fi

if [ -f "$CUSTOM_ICON" ]; then
  cp "$CUSTOM_ICON" "$ICON_PATH"
  ICON_SOURCE="$CUSTOM_ICON"
else
  if [ ! -f "$LOGO_ICON" ]; then
    echo "Missing icon sources: $CUSTOM_ICON and $LOGO_ICON" >&2
    exit 1
  fi
  for size in 16 32 128 256 512; do
    /usr/bin/sips -z "$size" "$size" "$LOGO_ICON" \
      --out "$ICONSET_DIR/icon_${size}x${size}.png" >/dev/null
    double_size=$((size * 2))
    /usr/bin/sips -z "$double_size" "$double_size" "$LOGO_ICON" \
      --out "$ICONSET_DIR/icon_${size}x${size}@2x.png" >/dev/null
  done
  /usr/bin/iconutil -c icns "$ICONSET_DIR" -o "$ICON_PATH"
  ICON_SOURCE="$LOGO_ICON"
fi

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleExecutable</key>
  <string>$EXEC_NAME</string>
  <key>CFBundleIconFile</key>
  <string>GPS360Viewer</string>
  <key>CFBundleIdentifier</key>
  <string>local.gps360.viewer</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>$APP_NAME</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>0.3.0</string>
  <key>CFBundleVersion</key>
  <string>3</string>
  <key>LSMinimumSystemVersion</key>
  <string>13.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
EOF

echo "Built: $APP_PATH"
echo "Icon source: $ICON_SOURCE"
if [ -f "$APP_LIB/libusb-1.0.dylib" ]; then
  echo "Bundled: $APP_LIB/libusb-1.0.dylib"
else
  echo "Bundled libusb not found at $BUNDLED_LIBUSB (runtime will use system libusb)."
fi
echo "Double-click '$APP_NAME.app' in Finder to launch (self-contained window)."
