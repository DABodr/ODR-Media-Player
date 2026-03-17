# CLAUDE.md - ODR Media Player Python/GTK Development Notes

> Primary directive: this application controls a real DAB+ encoder chain.
> Priorities: stability > elegance, local patch > rewrite, compatibility > novelty.

## 1. Current Project

`ODR Media Player` is a Python 3 + GTK3 / PyGObject desktop application.
The old Gambas project is gone. This repository now targets only the Python codebase.

### Goal

Single-service desktop interface used to control locally:

- audio playback from local files
- audio playback from stream URLs and imported M3U / PLS playlists
- live app-audio capture routed from PulseAudio
- live audio-input capture from PulseAudio sources / external sound cards
- `odr-audioenc`
- `odr-padenc`
- output toward a remote or external `odr-dabmux`

### Runtime Chain

```text
Local files / stream URLs / app audio / audio input
  -> GStreamer
  -> ALSA Loopback
  -> odr-audioenc
  -> ZMQ (tcp) or EDI (udp)
  -> remote / external odr-dabmux

PAD / DLS / DL+ / SLS
  -> odr-padenc
  -> Unix socket /tmp/dab-encodeur.padenc
  -> injected into odr-audioenc
```

## 2. Repository Structure

```text
odr_fileplayer.py
encodeur_dab_app/
  app_config.py
  config_store.py
  constants.py
  dls.py
  encoder.py
  media.py
  monitor.py
  player.py
  playlist_model.py
  playlist_state.py
  pulseaudio.py
  runtime_state.py
  ui.py
  view_builders.py
resources/
  dab_logo.png
```

### Current Split

- `odr_fileplayer.py`: main GTK window, callbacks, orchestration
- `encodeur_dab_app/view_builders.py`: UI construction
- `encodeur_dab_app/player.py`: GStreamer playback pipeline and labels
- `encodeur_dab_app/encoder.py`: `odr-audioenc` / `odr-padenc` command building
- `encodeur_dab_app/media.py`: tags, SLS asset generation, title card rendering, artwork lookup, logo import
- `encodeur_dab_app/monitor.py`: VU drawing, DLS reading, slideshow preview rotation
- `encodeur_dab_app/pulseaudio.py`: PulseAudio app routing and input-source discovery
- `encodeur_dab_app/config_store.py`: config file read/write compatibility

## 3. Current UI Layout

The app currently exposes 3 tabs:

- `Player`
- `PAD / DLS`
- `DAB+ output`

Below the tabs, a persistent `Now Playing` strip stays visible on every page.

### Player

- playlist only
- add files / add playlist / add URL / app audio / audio input / folder / edit / move / remove / clear actions
- multi-selection enabled for remove / move operations
- current track highlight in blue
- empty-state placeholder when no tracks are loaded

### Now Playing Strip

- current title
- countdown to next track
- playlist autostart toggle
- `Prev / Play / Stop / Next`
- `Shuffle`
- `Repeat`
- `Local monitor`

### PAD / DLS

- default DLS text
- force default DLS
- DLS from file
- DL+
- SLS enable
- internal default logo library
- generated title card preview
- optional local cover-art usage toggle
- optional online cover-art lookup
- slideshow rotation delay
- PAD/SLS throughput estimate and warning

### DAB+ output

- process status
- encoder autostart toggle
- `Start / Restart encoder / Stop / Log / Save config`
- DLS / DL+ / Title / Artist / slideshow preview
- codec / bitrate / sample rate / channels / gain / output mode / address / port
- compact `Encoder settings` panel including PAD length
- single DAB+ output VU meter sourced from `odr-audioenc`

## 4. Useful Commands

```bash
python3 -m py_compile odr_fileplayer.py encodeur_dab_app/*.py
DISPLAY=:0 python3 odr_fileplayer.py
```

## 5. Important Runtime Files and Paths

```text
DLS_FILE          /tmp/dab-encodeur.dls
PAD_ID            dab-encodeur
SLIDE_INPUT_DIR   /tmp/dab-encodeur-slides
SLIDE_INPUT_FILE  /tmp/dab-encodeur-slides/slide.jpg   (legacy constant; runtime slide files are now generated as slide-<token>.jpg)
SLIDE_DUMP        /tmp/dab-current-slide.jpg
CONF_FILE         ~/.config/encodeur-dab.conf
APP_DATA_DIR      ~/.local/share/odr-fileplayer
DEFAULT_LOGO_DIR  ~/.local/share/odr-fileplayer/default-logos
COVER_CACHE_DIR   ~/.local/share/odr-fileplayer/cover-cache
DAB_LOGO_FILE     resources/dab_logo.png
```

## 6. Important Behaviors To Preserve

### Player

- The `Now Playing` strip must remain persistent across all tabs.
- `Stop` preserves the current playlist position. `Play` resumes from the stopped track, not from index `0`.
- The current track stays highlighted in blue in the playlist.
- Playlist multi-selection must remain supported for group removal and group move up / down.
- The player must keep supporting these source families in the same playlist:
  - local files
  - stream URLs
  - `pulse-monitor://...` app-audio captures
  - `pulse-source://...` live audio-input captures
- The GStreamer playback pipeline must force `audio/x-raw,rate={sample_rate}` after `audioresample`.
- `Local monitor` duplicates playback to the local desktop audio output without affecting the encoder path.
- If playlist autostart is enabled and the playlist is not empty, playback should start automatically on the current track when the UI launches.
- `Add playlist` must continue to support `.m3u`, `.m3u8`, `.pls`, including server-relative entries requiring a prompted base URL.
- `Edit` is single-selection only and must preserve manual metadata overrides in config.
- For app-audio capture, only one routed app is kept on the dedicated PulseAudio capture sink at a time.
- Stream sources must keep parsing GStreamer `TAG` messages to update titling live when metadata exists.

### DLS / DL+

- If no track is actively playing, DLS must fall back to the default DLS text.
- `DLS from audio file` only applies during active playback.
- `Force default DLS` overrides file metadata titling.
- If DL+ is inactive, `Title` and `Artist` in `DAB+ output` must display `-`.
- If playback stops, `DAB+ output` must return to default DLS and inactive DL+ state.
- The same metadata cleanup used for artwork lookup must also be applied to broadcast-facing `Artist` / `Title` values:
  - strip prefixes such as `001_`
  - avoid noisy filename-style titling in DLS / DL+
- Stream URLs may provide only partial metadata; DLS must tolerate title-only cases gracefully.

### Encoder

- `odr-padenc` must start before `odr-audioenc`.
- Shell launches must use `exec ...` to avoid orphan wrapper processes.
- `Restart encoder` becomes enabled only when the encoder is running and encoder settings have changed.
- `PAD` length is part of the restart signature and must trigger `Restart encoder` when changed.
- The DAB+ output VU meter must come only from `odr-audioenc`.
- If encoder autostart is enabled, `odr-padenc` and `odr-audioenc` should start automatically when the UI launches.
- Output protocol is selected via `cmb_output_proto`.
- The full output URI is built by `_get_output_uri()` and stored in config as `ZmqOut`, even when the mode is EDI/UDP.
- On confirmed application quit, current settings must be saved automatically before stopping playback / encoder processes.

### SLS / Title Card / Logos

- The app no longer relies on one external logo path.
- Default logos are imported into the internal logo library under `DEFAULT_LOGO_DIR`.
- Logos displayed in the UI are thumbnails only; runtime assets sent to `odr-padenc` must remain `320x240 JPEG`.
- `Include default logo` controls whether the internal default-logo library participates in the SLS rotation.
- If `Include default logo` is unchecked, the default logo library frame must be hidden.
- `Generate title card from metadata` builds a local title card from current metadata.
- The `Title card` preview must visually align with the default logo library cards.
- Title card text layout must remain adaptive for long artist/title values:
  - wrap before shrinking
  - keep title readability as priority
  - hide album if space is insufficient
  - reduce font size only as a last resort
- If cover art is available, it replaces the generated title-card graphics entirely: no overlaid title text should remain on the slide.
- Generated SLS JPEGs should stay as small as reasonably possible for transmission while keeping acceptable readability.
- A regenerated slideshow must use a fresh runtime filename, not keep overwriting the same `slide.jpg`, so `odr-padenc` reliably reloads it.
- In mono-slide mode, the `DAB+ output` preview must follow the real `SLIDE_DUMP` emitted by `odr-padenc`, not just the prepared source asset.

### Cover Art Lookup

- `Use local cover art` is optional and only relevant when title card generation is enabled.
- `Fetch cover art online` is optional and only relevant when title card generation is enabled.
- Cover lookup priority is:
  1. embedded cover art from the audio file, if local cover usage is enabled
  2. local folder cover (`cover`, `folder`, `front`, etc.), if local cover usage is enabled
  3. online lookup, if online cover usage is enabled
  4. pure generated title card fallback
- Online lookup uses `MusicBrainz` + `Cover Art Archive`.
- If album metadata exists, prefer `Artist + Album`.
- If album metadata is absent, fallback to `Artist + Title`.
- Online title lookup should try cleaned query variants for noisy radio / remix filenames:
  - strip numeric prefixes like `001_`
  - remove remix / mix / edit / version suffixes
  - retry with a simplified lead artist when needed
- Online results must be cached in `COVER_CACHE_DIR`.
- Failed cover lookups create `.miss` markers to avoid repeated useless requests.

### Slideshow Rotation

- Rotation delay is configured by `Rotation (s)`.
- The configured value must be passed to `odr-padenc` using `-s`.
- The `DAB+ output` slideshow preview must follow the same rotation logic as the assets prepared for `odr-padenc`.
- The app must expose a practical `PAD/SLS` estimate based on:
  - active slide sizes
  - current PAD length
  - current rotation
- The estimate should warn when the selected rotation is likely too fast for the current slide size and PAD throughput.

## 7. Current Functional State

- local audio playlist with `ffprobe` metadata
- stream URL playback with live GStreamer tag updates
- M3U / M3U8 / PLS import
- app-audio capture from running PulseAudio applications
- live audio-input capture from PulseAudio input sources
- playback through ALSA Loopback
- track highlight and persistent now-playing strip
- playlist multi-selection for grouped operations
- per-entry edit dialog with persisted manual metadata overrides
- stop/resume on current track
- playlist and encoder autostart
- DLS, file titling, DL+
- internal default-logo library with add / remove
- generated title card preview
- optional local cover-art toggle
- optional online cover-art lookup with cache
- adaptive title-card rendering for long text
- cover-only slideshow rendering when real artwork is found
- SLS slideshow rotation sent to `odr-padenc`
- SLS preview aligned with the real `odr-padenc` dump in mono-slide mode
- SLS runtime assets regenerated with unique filenames
- optimized slideshow JPEG generation
- PAD/SLS timing estimate and warning
- `odr-audioenc` / `odr-padenc` supervision
- process and socket cleanup on stop
- ZMQ tcp or EDI udp output selection
- DAB+ output panel with process state, output details, slideshow preview and encoder controls
- separate log window opened from the `Log` button

## 8. Known Limits

- `dablin` did not decode the EDI generated directly by `odr-audioenc` in our tests.
- `dablin` is the console player; use `dablin_gtk` when slideshow display is required.
- The best real multiplex audio monitoring still comes from a preview stream emitted by the remote `dabmux`.
- Online cover lookup is best-effort and may still miss or misidentify some tracks, especially metadata-poor singles or compilations.
- Online cover fetch depends on external connectivity and third-party metadata quality.
- Small PAD values can make slideshow delivery extremely slow; for image-heavy SLS, the PAD/SLS estimate in the UI is the reference.

## 9. Config and Compatibility Rules

- Keep local, minimal patches.
- Preserve the flat config format in `~/.config/encodeur-dab.conf`.
- Preserve existing keys unless there is a strong migration reason.
- Current flat config also stores `PlaylistAutostart`, `EncoderAutostart`, `SLSCoverLocal`, `SLSCoverOnline`, `SLSDefaultLogo`, `LastLogoDir`, and `PlaylistOverrides`.
- `ZmqOut` must continue to store the full output URI including scheme.
- Temporary paths and runtime identifiers must stay stable unless explicitly migrated.
- Do not introduce a hard dependency on a local `dabmux` unless explicitly requested.

## 10. Minimal Post-Change Verification

```bash
python3 -m py_compile odr_fileplayer.py encodeur_dab_app/*.py
DISPLAY=:0 python3 odr_fileplayer.py
```
