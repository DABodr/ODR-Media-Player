# Third-Party Notices

This project relies on third-party software and online services.

This file is informational and is not legal advice. For the authoritative terms,
always refer to each upstream project's own license files, notices and official
documentation.

Unless explicitly stated otherwise, `ODR Media Player` does not bundle these
third-party projects in its source repository. The Debian package mainly
declares them as system dependencies or recommended external tools.

## Core external tools

### ODR-AudioEnc

- Role: DAB/DAB+ audio encoder used by `ODR Media Player`
- Upstream: https://github.com/Opendigitalradio/ODR-AudioEnc
- Notes: upstream describes `odr-audioenc` as part of the ODR-mmbTools and
  includes its own source tree, notices and license files. Consult the upstream
  repository for the authoritative licensing terms.

### ODR-PadEnc

- Role: PAD / DLS / DL+ / slideshow encoder used by `ODR Media Player`
- Upstream: https://github.com/Opendigitalradio/ODR-PadEnc
- Notes: consult the upstream repository and included license files for the
  authoritative licensing terms.

### GStreamer

- Role: media playback / decoding pipeline
- Upstream: https://gstreamer.freedesktop.org/
- Licensing reference: GStreamer licensing FAQ
  https://gstreamer.freedesktop.org/documentation/frequently-asked-questions/licensing.html

### PyGObject / GTK

- Role: GTK3 desktop UI bindings for Python
- Upstream: https://pygobject.gnome.org/
- Notes: refer to the upstream project and the distribution packages installed
  on the target system for exact licensing details.

### FFmpeg / ffprobe

- Role: audio tag probing and media inspection
- Upstream: https://ffmpeg.org/
- Licensing reference: https://ffmpeg.org/legal.html
- Notes: FFmpeg licensing can vary depending on how it was built. Refer to the
  FFmpeg legal page and to your distribution package metadata.

### ImageMagick

- Role: image conversion / optimization for slideshow generation
- Upstream: https://imagemagick.org/
- License reference: https://imagemagick.org/license/

## Online metadata and cover-art services

### MusicBrainz

- Role: metadata lookup used for optional cover-art discovery
- Official documentation:
  - About: https://musicbrainz.org/doc/About
  - Rate limiting: https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting
- Notes:
  - The application should use a meaningful `User-Agent`.
  - MusicBrainz asks clients to respect its web service usage rules, including
    rate limiting.
  - MusicBrainz states that most of its database is published under `CC0 1.0`,
    with some remaining data under `CC BY-NC-SA 3.0`.

### Cover Art Archive

- Role: retrieval of cover-art images associated with MusicBrainz releases
- Official documentation:
  - API: https://musicbrainz.org/doc/Cover_Art_Archive/API
- Notes:
  - Retrieved images remain subject to their own rights.
  - `ODR Media Player` uses these images as optional runtime lookups and local
    cache entries.
  - This project does not claim ownership of third-party artwork.

## Practical packaging note

If you redistribute `ODR Media Player` together with bundled copies of
third-party binaries, libraries, images or other assets, you may have additional
license compliance obligations beyond what is described here. In that case, you
should review the upstream licenses again and update this file accordingly.
