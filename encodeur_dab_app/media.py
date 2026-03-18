import os
import re
import shlex
import shutil
import subprocess
import tempfile
import hashlib
import colorsys
import uuid
import json
import time
import math
import configparser
import urllib.error
import urllib.parse
import urllib.request
import cairo
import gi
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GdkPixbuf, Pango, PangoCairo

from .constants import (
    COVER_CACHE_DIR,
    DAB_LOGO_FILE,
    DEFAULT_LOGO_DIR,
    PAD_ID,
    SLIDE_INPUT_DIR,
    SLIDE_INPUT_FILE,
)
from .dls import sanitize_broadcast_metadata


MUSICBRAINZ_USER_AGENT = "ODR-MediaPlayer/1.0 (local-radio-workstation)"
MUSICBRAINZ_MIN_INTERVAL = 1.1
_MUSICBRAINZ_LAST_REQUEST = 0.0
SLS_AUS_PER_SECOND = 1000.0 / 24.0
SLS_USEFUL_PAD_EFFICIENCY = 0.55
SLS_TARGET_MAX_BYTES = 20 * 1024


def try_load_loopback_module():
    modprobe = shutil.which("modprobe")
    if not modprobe:
        return False

    try:
        subprocess.run(
            [modprobe, "snd-aloop"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return False

    return True


def detect_loop_card():
    try:
        out = subprocess.check_output(
            ["arecord", "-l"],
            stderr=subprocess.DEVNULL,
            env={"LC_ALL": "C", **os.environ},
            timeout=5,
        ).decode()
    except Exception:
        return -1

    for line in out.splitlines():
        if "loopback" not in line.lower():
            continue
        match = re.search(r"card (\d+)", line)
        if match:
            return int(match.group(1))
    return -1


def should_ignore_audio_file(path):
    filename = os.path.basename((path or "").strip())
    return bool(filename) and filename.startswith("._")


def probe_audio_tags(path):
    artist = ""
    title = os.path.splitext(os.path.basename(path))[0]
    album = ""
    duration = "?"

    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "flat",
                "-show_entries",
                "format_tags:format=duration",
                path,
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:
        return artist, title, album, duration

    for line in out.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"')
        key = key.lower()
        if key in ("format.tags.artist", "format.tags.album_artist"):
            if not artist:
                artist = value
        elif key == "format.tags.title":
            title = value
        elif key == "format.tags.album":
            album = value
        elif key == "format.duration":
            try:
                seconds = float(value)
                duration = f"{int(seconds // 60)}:{int(seconds % 60):02d}"
            except ValueError:
                pass

    if not artist:
        fallback_artist, fallback_title = split_artist_title(title)
        if fallback_artist and fallback_title:
            artist = fallback_artist
            title = fallback_title

    return artist, title, album, duration


def list_audio_files(folder):
    out = subprocess.check_output(
        f"find {shlex.quote(folder)} -type f "
        r"\( -iname '*.mp3' -o -iname '*.wav' -o -iname '*.flac' "
        r"-o -iname '*.ogg' -o -iname '*.aac' -o -iname '*.m4a' "
        r"-o -iname '*.opus' \) | sort",
        shell=True,
        text=True,
        timeout=30,
    )
    return [
        line.strip()
        for line in out.splitlines()
        if line.strip() and not should_ignore_audio_file(line.strip())
    ]


def load_playlist_entries(path):
    playlist_path = (path or "").strip()
    if not playlist_path:
        raise ValueError("No playlist file selected.")
    if not os.path.isfile(playlist_path):
        raise ValueError("The playlist file was not found.")

    ext = os.path.splitext(playlist_path)[1].lower()
    if ext in (".pls",):
        entries = _parse_pls_file(playlist_path)
    else:
        entries = _parse_m3u_file(playlist_path)

    return [entry for entry in entries if entry.get("path")]


def estimate_sls_delivery(slide_paths, pad_len, rotation_seconds):
    sizes = []
    for path in slide_paths or ():
        if path and os.path.isfile(path):
            try:
                sizes.append(os.path.getsize(path))
            except OSError:
                pass

    if not sizes:
        return {
            "count": 0,
            "max_size": 0,
            "avg_size": 0,
            "gross_bytes_per_sec": 0.0,
            "useful_bytes_per_sec": 0.0,
            "seconds_per_slide": 0.0,
            "recommended_rotation": 0,
            "fits_rotation": True,
        }

    pad_len = max(1, int(pad_len or 1))
    rotation_seconds = max(1, int(rotation_seconds or 1))
    gross_bytes_per_sec = pad_len * SLS_AUS_PER_SECOND
    useful_bytes_per_sec = gross_bytes_per_sec * SLS_USEFUL_PAD_EFFICIENCY
    max_size = max(sizes)
    avg_size = int(sum(sizes) / len(sizes))
    seconds_per_slide = max_size / useful_bytes_per_sec if useful_bytes_per_sec > 0 else 0.0
    recommended_rotation = max(rotation_seconds, int(math.ceil(seconds_per_slide * 1.25)))

    return {
        "count": len(sizes),
        "max_size": max_size,
        "avg_size": avg_size,
        "gross_bytes_per_sec": gross_bytes_per_sec,
        "useful_bytes_per_sec": useful_bytes_per_sec,
        "seconds_per_slide": seconds_per_slide,
        "recommended_rotation": recommended_rotation,
        "fits_rotation": rotation_seconds >= seconds_per_slide,
    }


def _parse_m3u_file(path):
    text = _read_playlist_text(path)
    base_dir = os.path.dirname(os.path.abspath(path))
    entries = []
    pending_title = ""

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            pending_title = _parse_extinf_title(line)
            continue
        if line.startswith("#"):
            continue

        resolved = _resolve_playlist_entry(base_dir, line)
        entries.append({
            "path": resolved,
            "title": pending_title.strip(),
        })
        pending_title = ""

    return entries


def _parse_pls_file(path):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    text = _read_playlist_text(path)
    parser.read_string(text)
    if not parser.has_section("playlist"):
        return []

    section = parser["playlist"]
    base_dir = os.path.dirname(os.path.abspath(path))
    entries = []
    for key, value in section.items():
        match = re.fullmatch(r"file(\d+)", key, flags=re.IGNORECASE)
        if not match:
            continue
        index = match.group(1)
        resolved = _resolve_playlist_entry(base_dir, value)
        title = (section.get(f"title{index}", "") or "").strip()
        entries.append({
            "path": resolved,
            "title": title,
        })
    return entries


def _read_playlist_text(path):
    with open(path, "rb") as handle:
        payload = handle.read()

    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _parse_extinf_title(line):
    _prefix, _sep, title = line.partition(",")
    return (title or "").strip()


def _resolve_playlist_entry(base_dir, value):
    value = (value or "").strip()
    if not value:
        return ""

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme.lower() == "file":
        local_path = urllib.request.url2pathname(parsed.path or "")
        if parsed.netloc:
            local_path = f"//{parsed.netloc}{local_path}"
        return os.path.abspath(os.path.expanduser(local_path))

    if parsed.scheme and parsed.scheme.lower() != "file":
        return value

    local_path = os.path.expanduser(value)
    if not os.path.isabs(local_path):
        local_path = os.path.join(base_dir, local_path)
    return os.path.abspath(local_path)


def cleanup_pad_artifacts():
    for path in (
        f"/tmp/{PAD_ID}.padenc",
        f"/tmp/{PAD_ID}.audioenc",
        f"/tmp/{PAD_ID}.stats",
    ):
        try:
            if os.path.lexists(path):
                os.unlink(path)
        except OSError:
            pass


def prepare_slide_image(source_path, output_path=None, reset_dir=True):
    source_path = (source_path or "").strip()
    if not source_path:
        raise ValueError("No image file selected for SLS.")
    if not os.path.isfile(source_path):
        raise ValueError("The SLS image file was not found.")

    src = GdkPixbuf.Pixbuf.new_from_file(source_path)
    width = src.get_width()
    height = src.get_height()
    if width <= 0 or height <= 0:
        raise ValueError("Invalid SLS image.")

    target_path = output_path or SLIDE_INPUT_FILE
    if reset_dir:
        _reset_slide_input_dir()
    else:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)

    if _prepare_slide_with_imagemagick(source_path, target_path):
        _optimize_generated_slide(target_path)
        return target_path

    target_path = _prepare_slide_with_gdkpixbuf(source_path, target_path)
    _optimize_generated_slide(target_path)
    return target_path


def generate_title_card_image(
    artist,
    title,
    album="",
    footer_text="Generated from metadata",
    artwork_path="",
    output_path=None,
    reset_dir=True,
):
    display_artist, display_title = sanitize_broadcast_metadata(artist, title)
    display_title = display_title or "Unknown Title"
    display_artist = display_artist or "Unknown Artist"
    display_album = (album or "").strip()

    handle = tempfile.NamedTemporaryFile(
        prefix=f"{PAD_ID}-title-card-",
        suffix=".png",
        delete=False,
    )
    temp_png = handle.name
    handle.close()

    try:
        _render_title_card_png(
            temp_png,
            display_artist,
            display_title,
            display_album,
            footer_text,
            artwork_path=artwork_path,
        )
        return prepare_slide_image(temp_png, output_path=output_path, reset_dir=reset_dir)
    finally:
        try:
            os.unlink(temp_png)
        except OSError:
            pass


def build_sls_slide_set(
    default_logo_paths=None,
    include_title_card=False,
    use_local_cover=True,
    fetch_cover_online=False,
    online_cache_only=False,
    include_default_logo=False,
    track=None,
    default_text="",
    allow_placeholder=False,
):
    _reset_slide_input_dir()
    slides = []
    preview_source = ""
    generation_token = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"

    if include_title_card:
        if track is not None:
            title = track.title or os.path.splitext(os.path.basename(track.path))[0]
            artwork_path, preview_source = resolve_track_artwork(
                track,
                use_local=use_local_cover,
                fetch_online=fetch_cover_online,
                online_cache_only=online_cache_only,
            )
            slides.append(
                (
                    generate_title_card_image(
                        track.artist,
                        title,
                        track.album,
                        footer_text="",
                        artwork_path=artwork_path,
                        output_path=_slide_output_path(len(slides), generation_token),
                        reset_dir=False,
                    ),
                    True,
                )
            )
        elif allow_placeholder:
            slides.append(
                (
                    generate_title_card_image(
                        "ODR Media Player",
                        (default_text or "").strip() or "Standby",
                        "",
                        footer_text="Waiting for playback",
                        artwork_path="",
                        output_path=_slide_output_path(len(slides), generation_token),
                        reset_dir=False,
                    ),
                    True,
                )
            )

    if include_default_logo:
        for default_logo_path in normalize_default_logo_paths(default_logo_paths or ()):
            slides.append(
                (
                    prepare_slide_image(
                        default_logo_path,
                        output_path=_slide_output_path(len(slides), generation_token),
                        reset_dir=False,
                    ),
                    False,
                )
            )

    slides = _deduplicate_slide_set(slides)

    if not slides:
        raise ValueError("No SLS image source is available.")

    return {
        "preview_path": slides[0][0],
        "preview_generated": slides[0][1],
        "preview_source": preview_source,
        "count": len(slides),
        "paths": [path for path, _generated in slides],
    }


def resolve_track_artwork(track, use_local=True, fetch_online=False, online_cache_only=False):
    if track is None or not getattr(track, "path", "").strip():
        return "", ""

    if use_local:
        embedded_art = _extract_embedded_cover_art(track.path)
        if embedded_art:
            return embedded_art, "embedded cover art"

        folder_art = _find_directory_cover_art(track.path)
        if folder_art:
            return folder_art, "local cover art"

    if fetch_online:
        if online_cache_only:
            online_art, online_source = _find_cached_cover_art_online(track.artist, track.album, track.title)
        else:
            online_art, online_source = _fetch_cover_art_online(track.artist, track.album, track.title)
        if online_art:
            return online_art, online_source or "online cover art"

    return "", ""


def _find_cached_cover_art_online(artist, album="", title=""):
    artist = (artist or "").strip()
    album = (album or "").strip()
    title = (title or "").strip()
    if not artist or (not album and not title):
        return "", ""

    os.makedirs(COVER_CACHE_DIR, exist_ok=True)
    for artist_variant, album_variant in _iter_album_artwork_search_variants(artist, album):
        album_art = _cached_cover_art_for_key(
            f"album|{artist_variant.lower()}|{album_variant.lower()}"
        )
        if album_art:
            return album_art, "online cover art (artist/album)"

    for artist_variant, title_variant in _iter_title_artwork_search_variants(artist, title):
        title_art = _cached_cover_art_for_key(
            f"title|{artist_variant.lower()}|{title_variant.lower()}"
        )
        if title_art:
            return title_art, "online cover art (artist/title)"

    return "", ""


def _extract_embedded_cover_art(source_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not os.path.isfile(source_path):
        return ""

    os.makedirs(COVER_CACHE_DIR, exist_ok=True)
    try:
        stat = os.stat(source_path)
    except OSError:
        return ""

    key = hashlib.sha1(
        f"{os.path.abspath(source_path)}|{stat.st_mtime_ns}|{stat.st_size}".encode("utf-8", errors="replace")
    ).hexdigest()
    target_path = os.path.join(COVER_CACHE_DIR, f"embedded-{key}.jpg")
    miss_path = os.path.join(COVER_CACHE_DIR, f"embedded-{key}.miss")
    if os.path.isfile(target_path):
        return target_path
    if os.path.isfile(miss_path):
        return ""

    cmd = [
        ffmpeg,
        "-y",
        "-i",
        source_path,
        "-an",
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        target_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
    except Exception:
        result = None

    if result is not None and result.returncode == 0 and os.path.isfile(target_path):
        return target_path

    _touch_file(miss_path)
    try:
        os.unlink(target_path)
    except OSError:
        pass
    return ""


def _find_directory_cover_art(source_path):
    folder = os.path.dirname(os.path.abspath(source_path))
    if not os.path.isdir(folder):
        return ""

    priorities = [
        "cover",
        "folder",
        "front",
        "album",
        "albumart",
        "artwork",
        "thumb",
    ]
    candidates = []
    for entry in os.listdir(folder):
        entry_path = os.path.join(folder, entry)
        if not os.path.isfile(entry_path):
            continue
        base, ext = os.path.splitext(entry)
        if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"):
            continue
        normalized_base = re.sub(r"[^a-z0-9]+", "", base.lower())
        score = len(priorities) + 10
        for index, prefix in enumerate(priorities):
            if normalized_base == prefix:
                score = index
                break
            if normalized_base.startswith(prefix):
                score = index + 2
                break
        candidates.append((score, entry.lower(), entry_path))

    if not candidates:
        return ""

    candidates.sort()
    return candidates[0][2]


def _fetch_cover_art_online(artist, album="", title=""):
    artist = (artist or "").strip()
    album = (album or "").strip()
    title = (title or "").strip()
    if not artist or (not album and not title):
        return "", ""

    os.makedirs(COVER_CACHE_DIR, exist_ok=True)
    for artist_variant, album_variant in _iter_album_artwork_search_variants(artist, album):
        album_art = _fetch_cover_art_for_key(
            f"album|{artist_variant.lower()}|{album_variant.lower()}",
            _musicbrainz_release_candidates(artist_variant, album_variant),
        )
        if album_art:
            return album_art, "online cover art (artist/album)"

    for artist_variant, title_variant in _iter_title_artwork_search_variants(artist, title):
        title_art = _fetch_cover_art_for_key(
            f"title|{artist_variant.lower()}|{title_variant.lower()}",
            _musicbrainz_recording_release_candidates(artist_variant, title_variant),
        )
        if title_art:
            return title_art, "online cover art (artist/title)"

    return "", ""


def _iter_album_artwork_search_variants(artist, album):
    variants = []
    _append_query_variant(variants, artist, album)
    _append_query_variant(variants, _clean_artwork_artist_query(artist), _clean_artwork_album_query(album))
    _append_query_variant(variants, _lead_artwork_artist_query(artist), _clean_artwork_album_query(album))
    return variants


def _iter_title_artwork_search_variants(artist, title):
    variants = []
    _append_query_variant(variants, artist, title)
    clean_artist = _clean_artwork_artist_query(artist)
    clean_title = _clean_artwork_title_query(title)
    _append_query_variant(variants, clean_artist, clean_title)
    _append_query_variant(variants, _lead_artwork_artist_query(clean_artist), clean_title)
    return variants


def _append_query_variant(variants, artist, value):
    artist = (artist or "").strip()
    value = (value or "").strip()
    if not artist or not value:
        return
    candidate = (artist, value)
    if candidate not in variants:
        variants.append(candidate)


def _clean_artwork_artist_query(value):
    value = re.sub(r"^\s*\d+[\W_]+", "", (value or "").strip(), flags=re.IGNORECASE)
    value = re.sub(r"\b(?:feat|featuring|ft)\.?\b.*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -_")


def _lead_artwork_artist_query(value):
    value = _clean_artwork_artist_query(value)
    if not value:
        return ""
    return re.split(r"\s*(?:&|,|/| x | vs\.?)\s*", value, maxsplit=1, flags=re.IGNORECASE)[0].strip()


def _clean_artwork_title_query(value):
    value = re.sub(r"^\s*\d+[\W_]+", "", (value or "").strip(), flags=re.IGNORECASE)
    value = re.sub(r"\s*[\(\[]\s*(?:radio|club|extended|mix|remix|edit|version)[^)\]]*[\)\]]\s*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*-\s*(?:radio|club|extended|mix|remix|edit|version).*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -_")


def _clean_artwork_album_query(value):
    value = re.sub(r"^\s*\d+[\W_]+", "", (value or "").strip(), flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -_")


def _fetch_cover_art_for_key(cache_key_base, release_candidates):
    release_candidates = [release_id for release_id in release_candidates if release_id]
    if not release_candidates:
        return ""

    cached = _cached_cover_art_for_key(cache_key_base)
    if cached:
        return cached

    cache_key = hashlib.sha1(cache_key_base.encode("utf-8", errors="replace")).hexdigest()
    target_path = os.path.join(COVER_CACHE_DIR, f"online-{cache_key}.jpg")
    miss_path = os.path.join(COVER_CACHE_DIR, f"online-{cache_key}.miss")
    if os.path.isfile(miss_path):
        return ""

    for release_id in release_candidates[:8]:
        cover_url = f"https://coverartarchive.org/release/{release_id}/front-500"
        try:
            _download_binary_file(cover_url, target_path, accept="image/*")
        except Exception:
            try:
                os.unlink(target_path)
            except OSError:
                pass
            continue
        if os.path.isfile(target_path):
            return target_path

    _touch_file(miss_path)
    return ""


def _cached_cover_art_for_key(cache_key_base):
    cache_key = hashlib.sha1(cache_key_base.encode("utf-8", errors="replace")).hexdigest()
    target_path = os.path.join(COVER_CACHE_DIR, f"online-{cache_key}.jpg")
    return target_path if os.path.isfile(target_path) else ""


def _musicbrainz_release_id(artist, album):
    query = f'artist:"{artist}" AND release:"{album}"'
    url = (
        "https://musicbrainz.org/ws/2/release/?"
        + urllib.parse.urlencode({"query": query, "fmt": "json", "limit": "5"})
    )
    try:
        payload = _download_json(url)
    except Exception:
        return ""

    releases = payload.get("releases") or []
    if not releases:
        return ""

    artist_norm = _normalize_search_value(artist)
    album_norm = _normalize_search_value(album)
    best_match = None
    best_rank = None
    for release in releases:
        release_id = (release.get("id") or "").strip()
        if not release_id:
            continue
        release_title = _normalize_search_value(release.get("title"))
        artist_credit = _normalize_search_value(" ".join(_iter_artist_credit_names(release)))
        score = int(release.get("score") or 0)
        rank = (
            1 if release_title == album_norm else 0,
            1 if artist_norm and artist_norm in artist_credit else 0,
            score,
        )
        if best_rank is None or rank > best_rank:
            best_rank = rank
            best_match = release_id

    return best_match or ""


def _musicbrainz_release_candidates(artist, album):
    release_id = _musicbrainz_release_id(artist, album)
    return [release_id] if release_id else []


def _musicbrainz_recording_release_candidates(artist, title):
    query = f'artist:"{artist}" AND recording:"{title}"'
    url = (
        "https://musicbrainz.org/ws/2/recording/?"
        + urllib.parse.urlencode({"query": query, "fmt": "json", "limit": "8"})
    )
    try:
        payload = _download_json(url)
    except Exception:
        return []

    recordings = payload.get("recordings") or []
    artist_norm = _normalize_search_value(artist)
    title_norm = _normalize_search_value(title)
    candidates = []
    for recording in recordings:
        rec_title = _normalize_search_value(recording.get("title"))
        rec_artists = _normalize_search_value(" ".join(_iter_artist_credit_names(recording)))
        score = int(recording.get("score") or 0)
        base_rank = (
            1 if rec_title == title_norm else 0,
            1 if title_norm and title_norm in rec_title else 0,
            1 if artist_norm and artist_norm in rec_artists else 0,
            score,
        )
        for release in recording.get("releases") or []:
            release_id = (release.get("id") or "").strip()
            if not release_id:
                continue
            release_title = _normalize_search_value(release.get("title"))
            release_rank = (
                1 if release_title == title_norm else 0,
                0 if "compilation" in release_title else 1,
            )
            candidates.append((base_rank + release_rank, release_id))

    candidates.sort(reverse=True)
    unique = []
    seen = set()
    for _rank, release_id in candidates:
        if release_id in seen:
            continue
        seen.add(release_id)
        unique.append(release_id)
    return unique


def _iter_artist_credit_names(release):
    for item in release.get("artist-credit") or []:
        if isinstance(item, dict):
            name = item.get("name") or ""
            if name:
                yield name


def _download_json(url):
    _respect_musicbrainz_rate_limit()
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": MUSICBRAINZ_USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset, errors="replace")
    return json.loads(payload)


def _download_binary_file(url, target_path, accept="*/*"):
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": MUSICBRAINZ_USER_AGENT,
            "Accept": accept,
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        with open(target_path, "wb") as handle:
            handle.write(response.read())


def _respect_musicbrainz_rate_limit():
    global _MUSICBRAINZ_LAST_REQUEST
    delay = MUSICBRAINZ_MIN_INTERVAL - (time.monotonic() - _MUSICBRAINZ_LAST_REQUEST)
    if delay > 0:
        time.sleep(delay)
    _MUSICBRAINZ_LAST_REQUEST = time.monotonic()


def _normalize_search_value(value):
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _touch_file(path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            os.utime(path, None)
    except OSError:
        pass


def _prepare_slide_with_imagemagick(source_path, target_path):
    convert = shutil.which("magick") or shutil.which("convert")
    if not convert:
        return False

    if os.path.basename(convert) == "magick":
        cmd = [convert, source_path]
    else:
        cmd = [convert, source_path]

    cmd.extend(
        [
            "-auto-orient",
            "-resize",
            "320x240",
            "-background",
            "white",
            "-gravity",
            "center",
            "-extent",
            "320x240",
            "-alpha",
            "remove",
            "-alpha",
            "off",
            "-strip",
            "-sampling-factor",
            "4:2:0",
            "-quality",
            "82",
            target_path,
        ]
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:
        return False

    return result.returncode == 0 and os.path.isfile(target_path)


def _render_title_card_png(path, artist, title, album, footer_text, artwork_path=""):
    width = 320
    height = 240
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surface)

    has_artwork = _draw_title_card_background(
        cr,
        width,
        height,
        artwork_path,
        artist,
        title,
        album,
        cover_only=bool(artwork_path),
    )
    if has_artwork:
        surface.write_to_png(path)
        return

    logo_bottom = 26
    if os.path.isfile(DAB_LOGO_FILE):
        try:
            logo = cairo.ImageSurface.create_from_png(DAB_LOGO_FILE)
            logo_height = 26.0
            scale = logo_height / logo.get_height()
            cr.save()
            cr.translate(22, 20)
            cr.scale(scale, scale)
            cr.set_source_surface(logo, 0, 0)
            cr.paint()
            cr.restore()
            logo_bottom = int(20 + logo.get_height() * scale)
        except Exception:
            logo_bottom = 26

    body_top = logo_bottom + 12
    footer_layout = None
    footer_height = 0
    if (footer_text or "").strip():
        footer_layout = _build_text_layout(
            cr,
            footer_text,
            276,
            "Sans 10",
            max_lines=1,
            letter_spacing=300,
        )
        footer_height = _layout_pixel_height(footer_layout)
    footer_y = height - 20 - footer_height
    available_height = max(60, footer_y - body_top - 8)
    artist_layout, title_layout, album_layout = _fit_title_card_text_stack(
        cr,
        artist.upper(),
        title,
        album,
        276,
        available_height,
    )

    current_y = body_top
    if artist_layout is not None and (artist or "").strip():
        _draw_layout(cr, artist_layout, 22, current_y, (0.88, 0.93, 1.0, 0.92))
        current_y += _layout_pixel_height(artist_layout) + 6
    if title_layout is not None:
        title_rgba = (1.0, 1.0, 1.0, 0.99 if has_artwork else 0.98)
        _draw_layout(cr, title_layout, 22, current_y, title_rgba)
        current_y += _layout_pixel_height(title_layout)
    if album_layout is not None:
        current_y += 8
        _draw_layout(cr, album_layout, 22, current_y, (0.92, 0.95, 1.0, 0.78))
    if footer_layout is not None:
        _draw_layout(cr, footer_layout, 22, footer_y, (0.92, 0.95, 1.0, 0.60))

    surface.write_to_png(path)


def _title_card_palette(seed_text):
    digest = hashlib.sha1(seed_text.encode("utf-8", errors="replace")).digest()
    hue = digest[0] / 255.0
    primary = colorsys.hsv_to_rgb(hue, 0.55, 0.32)
    secondary = colorsys.hsv_to_rgb((hue + 0.12) % 1.0, 0.68, 0.78)
    return primary, secondary


def _draw_title_card_background(cr, width, height, artwork_path, artist, title, album, cover_only=False):
    if artwork_path and os.path.isfile(artwork_path):
        try:
            src = GdkPixbuf.Pixbuf.new_from_file(artwork_path)
            scale = max(width / src.get_width(), height / src.get_height())
            scaled_width = max(1, int(round(src.get_width() * scale)))
            scaled_height = max(1, int(round(src.get_height() * scale)))
            scaled = src.scale_simple(scaled_width, scaled_height, GdkPixbuf.InterpType.BILINEAR)
            offset_x = (width - scaled_width) // 2
            offset_y = (height - scaled_height) // 2
            Gdk.cairo_set_source_pixbuf(cr, scaled, offset_x, offset_y)
            cr.paint()

            if cover_only:
                return True

            overlay = cairo.LinearGradient(0, 0, width, 0)
            overlay.add_color_stop_rgba(0.0, 0.04, 0.06, 0.09, 0.82)
            overlay.add_color_stop_rgba(0.55, 0.04, 0.06, 0.09, 0.54)
            overlay.add_color_stop_rgba(1.0, 0.04, 0.06, 0.09, 0.34)
            cr.rectangle(0, 0, width, height)
            cr.set_source(overlay)
            cr.fill()

            bottom_overlay = cairo.LinearGradient(0, 0, 0, height)
            bottom_overlay.add_color_stop_rgba(0.0, 0.04, 0.06, 0.09, 0.10)
            bottom_overlay.add_color_stop_rgba(1.0, 0.04, 0.06, 0.09, 0.42)
            cr.rectangle(0, 0, width, height)
            cr.set_source(bottom_overlay)
            cr.fill()

            cr.set_source_rgba(1, 1, 1, 0.08)
            cr.rectangle(18, 18, width - 36, height - 36)
            cr.set_line_width(1.2)
            cr.stroke()
            return True
        except Exception:
            pass

    primary, secondary = _title_card_palette(f"{artist}|{title}|{album}")
    gradient = cairo.LinearGradient(0, 0, width, height)
    gradient.add_color_stop_rgb(0.0, *primary)
    gradient.add_color_stop_rgb(1.0, *secondary)
    cr.rectangle(0, 0, width, height)
    cr.set_source(gradient)
    cr.fill()

    cr.set_source_rgba(1, 1, 1, 0.08)
    cr.arc(width - 48, 46, 70, 0, 6.28318)
    cr.fill()
    cr.arc(44, height - 24, 58, 0, 6.28318)
    cr.fill()

    cr.set_source_rgba(1, 1, 1, 0.10)
    cr.rectangle(18, 18, width - 36, height - 36)
    cr.set_line_width(1.2)
    cr.stroke()
    return False


def _fit_title_card_text_stack(cr, artist, title, album, width, available_height):
    profiles = [
        {
            "artist_font": "Sans Semi-Bold 14",
            "artist_lines": 2,
            "artist_spacing": 300,
            "title_font": "Sans Bold 24",
            "title_lines": 4,
            "album_font": "Sans 13",
            "album_lines": 2,
        },
        {
            "artist_font": "Sans Semi-Bold 13",
            "artist_lines": 2,
            "artist_spacing": 220,
            "title_font": "Sans Bold 22",
            "title_lines": 4,
            "album_font": "Sans 12",
            "album_lines": 2,
        },
        {
            "artist_font": "Sans Semi-Bold 12",
            "artist_lines": 2,
            "artist_spacing": 160,
            "title_font": "Sans Bold 20",
            "title_lines": 5,
            "album_font": "Sans 11",
            "album_lines": 2,
        },
        {
            "artist_font": "Sans Condensed Semi-Bold 12",
            "artist_lines": 2,
            "artist_spacing": 120,
            "title_font": "Sans Condensed Bold 19",
            "title_lines": 5,
            "album_font": "Sans 10",
            "album_lines": 2,
        },
    ]

    best_fallback = None
    for profile in profiles:
        for show_album in ((True, False) if (album or "").strip() else (False,)):
            artist_layout = None
            artist_height = 0
            artist_ellipsized = False
            if (artist or "").strip():
                artist_layout = _build_text_layout(
                    cr,
                    artist,
                    width,
                    profile["artist_font"],
                    max_lines=profile["artist_lines"],
                    letter_spacing=profile["artist_spacing"],
                )
                artist_height = _layout_pixel_height(artist_layout)
                artist_ellipsized = _layout_is_ellipsized(artist_layout)

            title_layout = _build_text_layout(
                cr,
                title,
                width,
                profile["title_font"],
                max_lines=profile["title_lines"],
            )
            title_height = _layout_pixel_height(title_layout)
            title_ellipsized = _layout_is_ellipsized(title_layout)

            album_layout = None
            album_height = 0
            album_ellipsized = False
            if show_album and (album or "").strip():
                album_layout = _build_text_layout(
                    cr,
                    album,
                    width,
                    profile["album_font"],
                    max_lines=profile["album_lines"],
                )
                album_height = _layout_pixel_height(album_layout)
                album_ellipsized = _layout_is_ellipsized(album_layout)

            total_height = title_height
            if artist_height:
                total_height += artist_height + 6
            if album_height:
                total_height += album_height + 8

            candidate = (
                artist_layout,
                title_layout,
                album_layout,
                total_height,
                artist_ellipsized or title_ellipsized or album_ellipsized,
            )
            best_fallback = candidate
            if total_height <= available_height and not candidate[4]:
                return candidate[:3]

    if best_fallback is not None:
        return best_fallback[:3]
    return None, None, None


def _build_text_layout(cr, text, width, font_desc, max_lines=1, letter_spacing=0):
    layout = PangoCairo.create_layout(cr)
    layout.set_width(width * Pango.SCALE)
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_ellipsize(Pango.EllipsizeMode.END)
    if max_lines:
        layout.set_height(-max_lines)
    layout.set_font_description(Pango.FontDescription(font_desc))
    attrs = Pango.AttrList()
    if letter_spacing:
        attrs.insert(Pango.attr_letter_spacing_new(letter_spacing))
    layout.set_attributes(attrs)
    layout.set_text((text or "").strip(), -1)
    return layout


def _draw_layout(cr, layout, x, y, rgba):
    cr.set_source_rgba(*rgba)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)


def _layout_pixel_height(layout):
    if layout is None:
        return 0
    _width, height = layout.get_pixel_size()
    return height


def _layout_is_ellipsized(layout):
    if layout is None:
        return False
    checker = getattr(layout, "is_ellipsized", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return False


def _prepare_slide_with_gdkpixbuf(source_path, target_path):
    src = GdkPixbuf.Pixbuf.new_from_file(source_path)
    width = src.get_width()
    height = src.get_height()
    if width <= 0 or height <= 0:
        raise ValueError("Invalid SLS image.")

    scale = min(320.0 / width, 240.0 / height)
    scaled_width = max(1, int(round(width * scale)))
    scaled_height = max(1, int(round(height * scale)))
    scaled = src.scale_simple(scaled_width, scaled_height, GdkPixbuf.InterpType.BILINEAR)
    if scaled is None:
        raise ValueError("Unable to resize the SLS image.")

    canvas = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 320, 240)
    canvas.fill(0xFFFFFFFF)
    offset_x = (320 - scaled_width) // 2
    offset_y = (240 - scaled_height) // 2
    scaled.copy_area(0, 0, scaled_width, scaled_height, canvas, offset_x, offset_y)
    canvas.savev(target_path, "jpeg", ["quality"], ["82"])
    return target_path


def _optimize_generated_slide(target_path):
    if not os.path.isfile(target_path):
        return target_path
    try:
        if os.path.getsize(target_path) <= SLS_TARGET_MAX_BYTES:
            return target_path
    except OSError:
        return target_path

    if _optimize_slide_with_imagemagick(target_path):
        return target_path
    _optimize_slide_with_gdkpixbuf(target_path)
    return target_path


def _optimize_slide_with_imagemagick(target_path):
    convert = shutil.which("magick") or shutil.which("convert")
    if not convert or not os.path.isfile(target_path):
        return False

    qualities = (78, 72, 66, 60, 54)
    for quality in qualities:
        handle = tempfile.NamedTemporaryFile(
            prefix=f"{PAD_ID}-opt-slide-",
            suffix=".jpg",
            delete=False,
        )
        temp_path = handle.name
        handle.close()
        try:
            cmd = [convert, target_path]
            cmd.extend(
                [
                    "-strip",
                    "-sampling-factor",
                    "4:2:0",
                    "-quality",
                    str(quality),
                    temp_path,
                ]
            )
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode != 0 or not os.path.isfile(temp_path):
                continue
            if os.path.getsize(temp_path) < os.path.getsize(target_path):
                os.replace(temp_path, target_path)
            else:
                os.unlink(temp_path)
            if os.path.getsize(target_path) <= SLS_TARGET_MAX_BYTES:
                return True
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    return os.path.getsize(target_path) <= SLS_TARGET_MAX_BYTES


def _optimize_slide_with_gdkpixbuf(target_path):
    if not os.path.isfile(target_path):
        return False
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(target_path)
    except Exception:
        return False

    qualities = (78, 72, 66, 60, 54)
    for quality in qualities:
        handle = tempfile.NamedTemporaryFile(
            prefix=f"{PAD_ID}-opt-slide-",
            suffix=".jpg",
            delete=False,
        )
        temp_path = handle.name
        handle.close()
        try:
            pixbuf.savev(temp_path, "jpeg", ["quality"], [str(quality)])
            if os.path.getsize(temp_path) < os.path.getsize(target_path):
                os.replace(temp_path, target_path)
            else:
                os.unlink(temp_path)
            if os.path.getsize(target_path) <= SLS_TARGET_MAX_BYTES:
                return True
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    return os.path.getsize(target_path) <= SLS_TARGET_MAX_BYTES


def _reset_slide_input_dir():
    os.makedirs(SLIDE_INPUT_DIR, exist_ok=True)
    for entry in os.listdir(SLIDE_INPUT_DIR):
        entry_path = os.path.join(SLIDE_INPUT_DIR, entry)
        try:
            if os.path.isfile(entry_path) or os.path.islink(entry_path):
                os.unlink(entry_path)
        except OSError:
            pass


def _slide_output_path(index, generation_token=""):
    token = (generation_token or "").strip()
    if not token:
        token = "active"
    if index <= 0:
        return os.path.join(SLIDE_INPUT_DIR, f"slide-{token}.jpg")
    return os.path.join(SLIDE_INPUT_DIR, f"slide-{token}-{index + 1}.jpg")


def import_default_logo(source_path):
    os.makedirs(DEFAULT_LOGO_DIR, exist_ok=True)
    target_path = os.path.join(DEFAULT_LOGO_DIR, f"logo-{uuid.uuid4().hex}.jpg")
    return prepare_slide_image(source_path, output_path=target_path, reset_dir=False)


def normalize_default_logo_paths(paths):
    normalized = []
    seen = set()
    for path in paths:
        candidate = os.path.abspath((path or "").strip())
        if not candidate or not os.path.isfile(candidate):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized


def remove_default_logo(path):
    candidate = os.path.abspath((path or "").strip())
    logo_root = os.path.abspath(DEFAULT_LOGO_DIR)
    if not candidate.startswith(f"{logo_root}{os.sep}"):
        return
    try:
        os.unlink(candidate)
    except OSError:
        pass


def _deduplicate_slide_set(slides):
    unique_slides = []
    seen_hashes = set()

    for path, generated in slides:
        fingerprint = _file_sha1(path)
        if fingerprint and fingerprint in seen_hashes:
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        if fingerprint:
            seen_hashes.add(fingerprint)
        unique_slides.append((path, generated))

    return unique_slides


def _file_sha1(path):
    try:
        digest = hashlib.sha1()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def split_artist_title(value):
    value = (value or "").strip()
    if not value:
        return "", ""

    for separator in (" - ", " — ", " -", " —", "- ", "— "):
        if separator in value:
            artist, title = value.split(separator, 1)
            artist = artist.strip(" -—\t")
            title = title.strip(" -—\t")
            if artist and title:
                return artist, title

    return "", ""


def split_app_audio_title(value):
    value = (value or "").strip()
    if not value:
        return "", ""

    # Browser and desktop app titles often expose media metadata as
    # "Title • Artist" or "Title · Artist".
    for separator in (" • ", " · ", " | "):
        if separator in value:
            parts = [part.strip() for part in value.split(separator) if part.strip()]
            if len(parts) >= 2:
                title = parts[0]
                artist = parts[1]
                if artist and title:
                    return artist, title

    return split_artist_title(value)
