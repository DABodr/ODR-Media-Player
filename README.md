# ODR Media Player

`ODR Media Player` is a GTK3 desktop frontend for a local DAB+ playout and encoding chain based on:

- `GStreamer`
- `ALSA Loopback`
- `odr-audioenc`
- `odr-padenc`

It can play:

- local audio files
- web radio / stream URLs
- imported `M3U`, `M3U8` and `PLS` playlists
- live desktop app audio routed from PulseAudio / PipeWire
- live audio inputs exposed by PulseAudio / PipeWire

It can also generate and manage:

- `DLS`
- `DL+`
- `SLS / slideshow`
- generated title cards
- local and online cover-art lookup

## Features

- Persistent `Now Playing` strip visible from every tab
- Playlist management with multi-selection
- Stream metadata parsing from GStreamer tags
- App-audio and audio-input capture
- Encoder control for `odr-audioenc` and `odr-padenc`
- `ZMQ (tcp)` or `EDI (udp)` output modes
- Internal logo library for SLS
- PAD / SLS throughput estimate
- Debian menu integration when installed from the `.deb`

## Runtime chain

```text
Files / URLs / App audio / Audio input
  -> GStreamer
  -> ALSA Loopback
  -> odr-audioenc
  -> ZMQ (tcp) or EDI (udp)
  -> external / remote odr-dabmux

DLS / DL+ / SLS
  -> odr-padenc
  -> Unix socket
  -> injected into odr-audioenc
```

## Installation

### Option 1: Install the Debian package

If you already built the package:

```bash
sudo apt install ./dist/odr-media-player_1.0.0_all.deb
```

This installs:

- the application launcher in `/usr/bin/odr-media-player`
- the desktop entry in `/usr/share/applications/odr-media-player.desktop`
- the icon in `/usr/share/icons/hicolor/256x256/apps/odr-media-player.png`

On Debian / Raspberry Pi OS Desktop, the app will then appear in the applications menu.

### Option 2: Install dependencies manually

On Debian / Ubuntu:

```bash
sudo ./install_dependencies.sh
```

This installs the core runtime dependencies and enables persistent `snd-aloop` loading when available.

### Option 3: Manual installation without helper script

Install at least:

```bash
sudo apt install \
  python3 \
  python3-gi \
  python3-cairo \
  python3-gi-cairo \
  gir1.2-gtk-3.0 \
  gir1.2-gstreamer-1.0 \
  gir1.2-gdkpixbuf-2.0 \
  gir1.2-pango-1.0 \
  gstreamer1.0-tools \
  gstreamer1.0-alsa \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  alsa-utils \
  pulseaudio-utils \
  ffmpeg \
  imagemagick \
  kmod
```

Then install:

- `odr-audioenc`
- `odr-padenc`

If these packages are not available in your current repository, install them manually from your distribution backports or from your own build chain.

## Required components

### Mandatory for the GUI

- `python3`
- `PyGObject / GTK3`
- `GStreamer`
- `ALSA`
- `ffmpeg`

### Mandatory for the full encoder chain

- `odr-audioenc`
- `odr-padenc`
- `snd-aloop`

### Recommended

- `imagemagick`
  - better SLS conversion and optimization
- `pulseaudio-utils` or compatible `PipeWire` tools
  - needed for `App audio` and `Audio input`

## Enable ALSA loopback

Load it immediately:

```bash
sudo modprobe snd-aloop
```

Make it persistent across reboots:

```bash
echo snd-aloop | sudo tee /etc/modules-load.d/odr-fileplayer-snd-aloop.conf
```

## Launch from source

From the project directory:

```bash
python3 -m py_compile odr_fileplayer.py encodeur_dab_app/*.py
python3 odr_fileplayer.py
```

If needed on a desktop session:

```bash
DISPLAY=:0 python3 odr_fileplayer.py
```

## Build the Debian package

```bash
./build_deb.sh
```

Output:

```text
dist/odr-media-player_1.0.0_all.deb
```

## Configuration and runtime files

Main config file:

```text
~/.config/encodeur-dab.conf
```

Runtime files:

```text
/tmp/dab-encodeur.dls
/tmp/dab-encodeur-slides/
/tmp/dab-current-slide.jpg
```

App data:

```text
~/.local/share/odr-fileplayer/default-logos
~/.local/share/odr-fileplayer/cover-cache
```

## Notes

- The app is designed for a real encoder chain, so `Start`, `Restart encoder` and `Stop` act on real processes.
- `Local monitor` only duplicates audio locally and does not replace the encoder path.
- Online cover-art lookup depends on metadata quality and may be imperfect for remixes or noisy stream titles.
- For slideshow reception tests, graphical receivers such as `dablin_gtk` are more useful than console-only players.

## License

This project is distributed under the MIT license.
See [LICENSE](LICENSE).
