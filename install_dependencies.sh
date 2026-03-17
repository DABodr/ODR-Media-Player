#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="ODR Media Player"

if [[ "${1:-}" == "--help" ]]; then
  cat <<'EOF'
Usage: ./install_dependencies.sh

Installs the runtime dependencies required by ODR Media Player on Debian/Ubuntu:
- Python GTK3 / PyGObject
- GStreamer runtime and codecs
- ALSA / PulseAudio tools
- FFmpeg
- ImageMagick
- ODR-AudioEnc / ODR-PadEnc when available in APT
- persistent snd-aloop loading when the module is available

Run as root or with sudo.
EOF
  exit 0
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "This script must be run as root."
  echo "Example: sudo ./install_dependencies.sh"
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer currently supports Debian/Ubuntu systems using apt-get."
  exit 1
fi

APT_REQUIRED=(
  python3
  python3-gi
  python3-cairo
  python3-gi-cairo
  gir1.2-gtk-3.0
  gir1.2-gstreamer-1.0
  gir1.2-gdkpixbuf-2.0
  gir1.2-pango-1.0
  gstreamer1.0-tools
  gstreamer1.0-alsa
  gstreamer1.0-plugins-base
  gstreamer1.0-plugins-good
  gstreamer1.0-plugins-bad
  gstreamer1.0-plugins-ugly
  gstreamer1.0-libav
  alsa-utils
  pulseaudio-utils
  ffmpeg
  imagemagick
  kmod
)

APT_ODR=(
  odr-audioenc
  odr-padenc
)

package_available() {
  local package="$1"
  apt-cache show "$package" >/dev/null 2>&1
}

collect_available_packages() {
  local package
  for package in "$@"; do
    if package_available "$package"; then
      printf '%s\n' "$package"
    fi
  done
}

mapfile -t REQUIRED_PACKAGES < <(collect_available_packages "${APT_REQUIRED[@]}")
mapfile -t ODR_PACKAGES < <(collect_available_packages "${APT_ODR[@]}")

echo "==> Installing ${PROJECT_NAME} dependencies"
echo "==> apt-get update"
apt-get update

if ((${#REQUIRED_PACKAGES[@]})); then
  echo "==> Installing core packages"
  apt-get install -y "${REQUIRED_PACKAGES[@]}"
fi

if ((${#ODR_PACKAGES[@]})); then
  echo "==> Installing ODR encoder packages"
  apt-get install -y "${ODR_PACKAGES[@]}"
else
  echo
  echo "WARNING: odr-audioenc / odr-padenc were not found in current APT repositories."
  echo "You must install them manually before using the encoder chain."
fi

echo
echo "==> Verifying critical commands"
CRITICAL_CMDS=(
  python3
  ffprobe
  ffmpeg
  pactl
  arecord
  gst-launch-1.0
  odr-audioenc
  odr-padenc
)

missing_cmds=()
for cmd in "${CRITICAL_CMDS[@]}"; do
  if command -v "$cmd" >/dev/null 2>&1; then
    printf '  [OK] %s -> %s\n' "$cmd" "$(command -v "$cmd")"
  else
    printf '  [MISSING] %s\n' "$cmd"
    missing_cmds+=("$cmd")
  fi
done

echo
echo "==> Checking ALSA loopback module"
if modprobe -n snd-aloop >/dev/null 2>&1; then
  echo "  snd-aloop is available."
  echo "==> Enabling persistent snd-aloop loading"
  install -d /etc/modules-load.d
  cat >/etc/modules-load.d/odr-fileplayer-snd-aloop.conf <<'EOF'
snd-aloop
EOF
  if modprobe snd-aloop >/dev/null 2>&1; then
    echo "  snd-aloop loaded."
  else
    echo "  snd-aloop is available but could not be loaded automatically."
    echo "  Load it manually with: sudo modprobe snd-aloop"
  fi
  echo "  Persistent config written to /etc/modules-load.d/odr-fileplayer-snd-aloop.conf"
else
  echo "  snd-aloop is not available on this system."
fi

echo
echo "==> Summary"
if ((${#missing_cmds[@]} == 0)); then
  echo "All critical commands are available."
else
  echo "Some critical commands are still missing:"
  printf '  - %s\n' "${missing_cmds[@]}"
fi

echo
echo "You can now start the app with:"
echo "  DISPLAY=:0 python3 odr_fileplayer.py"
