#!/bin/zsh
# Build the self-contained app: the backend scripts and config are copied into the bundle, so the app runs from
# anywhere (Downloads, /Applications) without this source tree. Ad-hoc signed. Needs the Xcode command line tools.
# Usage: scripts/build_app.sh [output folder]     (default: ./dist)
set -euo pipefail
PROJECT="${0:A:h:h}"
OUT="${1:-$PROJECT/dist}"
PY="${LMC_BUILD_PYTHON:-/usr/bin/python3}"
VERSION="$(cat "$PROJECT/VERSION" 2>/dev/null || cat "$PROJECT/../VERSION" 2>/dev/null || echo 0.0.0)"
eval "$("$PY" -I "$PROJECT/scripts/brand.py" --shell)"   # APP_NAME, BUNDLE_ID, EXECUTABLE, PUBLISHER, HOMEPAGE
SHORT="${VERSION%%-*}"   # 1.2.0-preview.1 -> 1.2.0: the bundle's version must be plain numbers; the file name keeps the full one
[[ -n "${APP_NAME:-}" && -n "${BUNDLE_ID:-}" && -n "${EXECUTABLE:-}" ]] || { echo "Could not read scripts/brand.py with $PY" >&2; exit 1 }
SOURCES="$PROJECT/app/ConsoleApp"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
APP="$WORK/$APP_NAME.app"

swiftc -parse-as-library -O -swift-version 5 -target arm64-apple-macos14.0 \
  -framework SwiftUI -framework AppKit -framework WebKit -framework LocalAuthentication -framework ServiceManagement \
  "$SOURCES"/*.swift -o "$WORK/$EXECUTABLE"

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/console/scripts" "$APP/Contents/Resources/console/config/model-families"
cp -R "$SOURCES"/*.lproj "$APP/Contents/Resources/"
cp "$WORK/$EXECUTABLE" "$APP/Contents/MacOS/$EXECUTABLE"
# The backend: exactly the files the console installs on a paired Mac, plus the launcher.
for f in clusterctl.py paths.py brand.py rpc.py serve_omlx.py hub.py chip.py gateway.py device_service.py i18n.py i18n_en.json omlx_adapter.py updates.py signing.py run.py netscan.py gateway_defaults.py; do
  cp "$PROJECT/scripts/$f" "$APP/Contents/Resources/console/scripts/$f"
done
cp "$PROJECT/config/engine-compat.json" "$APP/Contents/Resources/console/config/"
cp "$PROJECT"/config/model-families/*.json "$APP/Contents/Resources/console/config/model-families/"
print -r -- "$VERSION" > "$APP/Contents/Resources/VERSION"
for f in LICENSE NOTICE; do [[ -f "$PROJECT/$f" ]] && cp "$PROJECT/$f" "$APP/Contents/Resources/$f"; done   # the license travels with the app
LICENSE_NAME="$([[ -f "$PROJECT/LICENSE" ]] && head -3 "$PROJECT/LICENSE" | grep -q 'Apache License' && echo 'Apache-2.0' || true)"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>$EXECUTABLE</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleDevelopmentRegion</key><string>en</string>
  <key>CFBundleLocalizations</key><array><string>en</string><string>zh-Hans</string></array>
  <key>LSHasLocalizedDisplayName</key><true/>
  <key>NSLocalNetworkUsageDescription</key><string>Used to connect to other Macs on your local network.</string>
  <key>CFBundleShortVersionString</key><string>$SHORT</string>
  <key>LMCFullVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>LMCEdition</key><string>public</string>
  ${LICENSE_NAME:+<key>LMCLicense</key><string>$LICENSE_NAME</string>}
  <key>LMCPublisher</key><string>$PUBLISHER</string>
  <key>LMCHomepage</key><string>$HOMEPAGE</string>
</dict></plist>
EOF

# Icon: render the SVG with Quick Look, then build every iconset size.
qlmanage -t -s 1024 -o "$WORK" "$PROJECT/assets/console-icon.svg" >/dev/null 2>&1
"$PY" -I "$PROJECT/scripts/icon_alpha.py" "$WORK/console-icon.svg.png" "$WORK/icon.png"
mkdir "$WORK/AppIcon.iconset"
for size in 16 32 128 256 512; do
  sips -z $size $size "$WORK/icon.png" --out "$WORK/AppIcon.iconset/icon_${size}x${size}.png" >/dev/null
  sips -z $((size * 2)) $((size * 2)) "$WORK/icon.png" --out "$WORK/AppIcon.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$WORK/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"
xattr -cr "$APP"
codesign --force --sign - "$APP"
codesign --verify --strict "$APP"

mkdir -p "$OUT"
ZIP="$OUT/$APP_NAME-$VERSION.zip"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
( cd "$OUT" && shasum -a 256 "${ZIP:t}" | tee "${ZIP:t}.sha256" )   # file name only: no build-machine path in the checksum file
echo "Built: $ZIP"
