#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_NAME="odr-media-player"
PKG_VERSION="${1:-1.1.2}"
PKG_ARCH="all"
DIST_DIR="$ROOT_DIR/dist"
BUILD_DIR="$DIST_DIR/build"
PKG_DIR="$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${PKG_ARCH}"
APP_DIR="$PKG_DIR/usr/share/odr-media-player"
ICON_DIR="$PKG_DIR/usr/share/icons/hicolor/256x256/apps"
DESKTOP_DIR="$PKG_DIR/usr/share/applications"
DOC_DIR="$PKG_DIR/usr/share/doc/$PKG_NAME"
DEBIAN_DIR="$PKG_DIR/DEBIAN"
OUTPUT_DEB="$DIST_DIR/${PKG_NAME}_${PKG_VERSION}_${PKG_ARCH}.deb"

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Missing required command: $1" >&2
        exit 1
    fi
}

require_cmd dpkg-deb
require_cmd python3
require_cmd install

rm -rf "$PKG_DIR"
mkdir -p "$APP_DIR" "$ICON_DIR" "$DESKTOP_DIR" "$DOC_DIR" "$DEBIAN_DIR"
mkdir -p "$DIST_DIR"

cat >"$DEBIAN_DIR/control" <<EOF
Package: $PKG_NAME
Version: $PKG_VERSION
Section: sound
Priority: optional
Architecture: $PKG_ARCH
Maintainer: ODR Media Player contributors
Depends: python3, python3-gi, python3-cairo, gir1.2-gtk-3.0, gir1.2-gstreamer-1.0, gir1.2-gdkpixbuf-2.0, gir1.2-pango-1.0, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad, gstreamer1.0-plugins-ugly, gstreamer1.0-libav, alsa-utils, ffmpeg, imagemagick, pulseaudio-utils | pipewire-bin
Recommends: odr-audioenc, odr-padenc
Description: ODR Media Player
 Desktop audio playout frontend for local files, online streams and live inputs,
 with DLS/DL+/SLS generation for ODR AudioEnc and ODR PADEnc.
EOF

cat >"$DEBIAN_DIR/postinst" <<'EOF'
#!/usr/bin/env bash
set -e

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q /usr/share/icons/hicolor >/dev/null 2>&1 || true
fi

exit 0
EOF
chmod 755 "$DEBIAN_DIR/postinst"

install -Dm755 "$ROOT_DIR/packaging/odr-media-player" "$PKG_DIR/usr/bin/odr-media-player"
install -Dm644 "$ROOT_DIR/packaging/odr-media-player.desktop" "$DESKTOP_DIR/odr-media-player.desktop"
install -Dm644 "$ROOT_DIR/odr_fileplayer.py" "$APP_DIR/odr_fileplayer.py"
install -Dm644 "$ROOT_DIR/LICENSE" "$DOC_DIR/LICENSE"
install -Dm644 "$ROOT_DIR/install_dependencies.sh" "$DOC_DIR/install_dependencies.sh"

while IFS= read -r -d '' file; do
    target="$APP_DIR/${file#"$ROOT_DIR/"}"
    install -Dm644 "$file" "$target"
done < <(find "$ROOT_DIR/encodeur_dab_app" -type f -name '*.py' -print0)

install -Dm644 "$ROOT_DIR/resources/dab_logo.png" "$APP_DIR/resources/dab_logo.png"

if command -v magick >/dev/null 2>&1; then
    magick "$ROOT_DIR/resources/dab_logo.png" -background none -gravity center -resize 256x256 -extent 256x256 "$ICON_DIR/odr-media-player.png"
elif command -v convert >/dev/null 2>&1; then
    convert "$ROOT_DIR/resources/dab_logo.png" -background none -gravity center -resize 256x256 -extent 256x256 "$ICON_DIR/odr-media-player.png"
else
    install -Dm644 "$ROOT_DIR/resources/dab_logo.png" "$ICON_DIR/odr-media-player.png"
fi

find "$PKG_DIR" -type d -exec chmod 755 {} +
find "$PKG_DIR/usr/share/odr-media-player" -type f -name '*.py' -exec chmod 644 {} +

dpkg-deb --build "$PKG_DIR" "$OUTPUT_DEB"
echo "Built package: $OUTPUT_DEB"
