import os
from urllib.parse import urlparse


def build_playlist_entry(index, track):
    text = f"{index + 1}.  "
    text += playlist_label(track)
    if track.duration and track.duration != "?":
        text += f"  [{track.duration}]"
    return text


def playlist_label(track):
    source_label = str(getattr(track, "source_label", "") or "").strip()
    if source_label and is_stream_url(track.path):
        return source_label
    return now_playing_label(track)


def now_playing_label(track):
    if track.artist and track.title:
        return f"{track.artist} — {track.title}"
    if track.title:
        return track.title
    if is_pulse_monitor_source(track.path):
        return pulse_monitor_title(track.path)
    if is_pulse_source(track.path):
        return pulse_source_title(track.path)
    if is_stream_url(track.path):
        return default_stream_title(track.path)
    return os.path.basename(track.path)


def is_pulse_monitor_source(path):
    parsed = urlparse((path or "").strip())
    return parsed.scheme.lower() == "pulse-monitor"


def pulse_monitor_source_name(path):
    parsed = urlparse((path or "").strip())
    source = (parsed.netloc or "") + (parsed.path or "")
    return source.strip().strip("/")


def pulse_monitor_title(path):
    source = pulse_monitor_source_name(path)
    return source or "Desktop audio capture"


def is_pulse_source(path):
    parsed = urlparse((path or "").strip())
    return parsed.scheme.lower() == "pulse-source"


def pulse_source_name(path):
    parsed = urlparse((path or "").strip())
    source = (parsed.netloc or "") + (parsed.path or "")
    return source.strip().strip("/")


def pulse_source_title(path):
    source = pulse_source_name(path)
    return source or "Audio input"


def is_stream_url(path):
    parsed = urlparse((path or "").strip())
    return bool(parsed.scheme and parsed.scheme.lower() != "file")


def default_stream_title(url):
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or parsed.netloc or "").strip()
    path = (parsed.path or "").rstrip("/")
    name = os.path.basename(path) if path else ""
    if host and name:
        return f"{host} / {name}"
    if host:
        return host
    if name:
        return name
    return (url or "").strip() or "Online stream"


def build_pipeline(path, volume, loop_card, sample_rate=48000, local_monitor=False):
    safe_path = path.replace("\\", "\\\\").replace('"', '\\"')
    if is_pulse_monitor_source(path):
        source_name = pulse_monitor_source_name(path).replace("\\", "\\\\").replace('"', '\\"')
        source = f'pulsesrc device="{source_name}"'
    elif is_pulse_source(path):
        source_name = pulse_source_name(path).replace("\\", "\\\\").replace('"', '\\"')
        source = f'pulsesrc device="{source_name}"'
    elif is_stream_url(path):
        source = f'uridecodebin uri="{safe_path}"'
    else:
        source = f'filesrc location="{safe_path}" ! decodebin'

    pipeline = (
        f"{source} ! audioconvert ! audioresample ! "
        f'audio/x-raw,rate={sample_rate} ! '
        f'volume volume={volume:.4f} name=vol ! tee name=t '
        f't. ! queue ! alsasink device=plughw:{loop_card},0,0 '
        f't. ! queue ! level name=lvl interval=300000000 post-messages=true ! fakesink'
    )
    if local_monitor:
        pipeline += ' t. ! queue ! autoaudiosink'
    return pipeline
