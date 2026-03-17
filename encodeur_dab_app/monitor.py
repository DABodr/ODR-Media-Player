from dataclasses import dataclass
import os
import time

from .dls import sanitize_broadcast_metadata


@dataclass
class MonitorSnapshot:
    dls: str
    artist: str
    title: str
    slide_mtime: float | None
    slide_path: str | None


def draw_vu(cr, width, height, left, right):
    outer_pad = 1.5
    gap = 2.0
    inner_pad = 2.5
    body_height = max(8.0, height - outer_pad * 2)
    channel_height = max(4.0, (body_height - inner_pad * 2 - gap) / 2.0)
    radius = min(8.0, body_height / 2.0)

    _rounded_rect(cr, outer_pad, outer_pad, width - outer_pad * 2, body_height, radius)
    cr.set_source_rgb(0.06, 0.08, 0.11)
    cr.fill_preserve()
    cr.set_source_rgb(0.18, 0.22, 0.28)
    cr.set_line_width(1.0)
    cr.stroke()

    top_y = outer_pad + inner_pad
    bottom_y = top_y + channel_height + gap
    track_width = max(1.0, width - outer_pad * 2 - inner_pad * 2)
    track_x = outer_pad + inner_pad
    track_radius = min(5.0, channel_height / 2.0)

    _draw_track(cr, track_x, top_y, track_width, channel_height, track_radius)
    _draw_track(cr, track_x, bottom_y, track_width, channel_height, track_radius)
    _draw_channel(cr, track_x, top_y, track_width, channel_height, left)
    _draw_channel(cr, track_x, bottom_y, track_width, channel_height, right)


def gst_peak_to_vu(peak_values):
    try:
        left_peak = float(peak_values[0]) if peak_values else -60.0
        right_peak = float(peak_values[1]) if len(peak_values) > 1 else left_peak
    except Exception:
        return None

    return _db_to_percent(left_peak), _db_to_percent(right_peak)


def read_monitor_snapshot(
    dls_path,
    playlist,
    current_idx,
    sls_enabled,
    slide_dump,
    previous_slide_mtime,
    slide_paths=None,
    slide_wait=10,
    rotation_started_at=0.0,
    preview_override_path="",
    preview_override_until=0.0,
):
    artist, title = current_track_labels(playlist, current_idx)
    slide_mtime = previous_slide_mtime
    slide_path = None
    valid_paths = [path for path in (slide_paths or ()) if path and os.path.isfile(path)]

    if sls_enabled:
        if (
            preview_override_path
            and os.path.isfile(preview_override_path)
            and time.monotonic() < float(preview_override_until or 0.0)
        ):
            slide_path = preview_override_path
        else:
            slide_path = current_slide_preview_path(valid_paths, slide_wait, rotation_started_at)
        if os.path.isfile(slide_dump):
            try:
                mtime = os.path.getmtime(slide_dump)
                # When only one slide is active, always mirror the real dump emitted by
                # odr-padenc so the UI matches what external receivers display.
                if len(valid_paths) <= 1:
                    slide_mtime = mtime
                    slide_path = slide_dump
                elif mtime != previous_slide_mtime:
                    slide_mtime = mtime
                    slide_path = slide_dump
                elif slide_path is None:
                    slide_path = slide_dump
            except Exception:
                pass

    return MonitorSnapshot(
        dls=read_current_dls(dls_path),
        artist=artist,
        title=title,
        slide_mtime=slide_mtime,
        slide_path=slide_path,
    )


def current_slide_preview_path(slide_paths, slide_wait, rotation_started_at):
    valid_paths = [path for path in slide_paths if path and os.path.isfile(path)]
    if not valid_paths:
        return None
    if len(valid_paths) == 1:
        return valid_paths[0]

    wait_seconds = max(1, int(slide_wait or 1))
    if rotation_started_at <= 0:
        return valid_paths[0]

    elapsed = max(0.0, time.monotonic() - rotation_started_at)
    index = int(elapsed // wait_seconds) % len(valid_paths)
    return valid_paths[index]


def read_current_dls(dls_path):
    if not os.path.isfile(dls_path):
        return "—"

    current = "—"
    try:
        with open(dls_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#####") and not line.startswith("DL_PLUS"):
                    current = line
    except Exception:
        return "—"

    return current


def current_track_labels(playlist, current_idx):
    if 0 <= current_idx < len(playlist):
        track = playlist[current_idx]
        artist, title = sanitize_broadcast_metadata(track.artist, track.title)
        return (artist or "—"), (title or "—")
    return "—", "—"


def _db_to_percent(value):
    return max(0, min(100, int((value + 60.0) * 100.0 / 60.0)))


def _draw_channel(cr, x, y, width, height, level):
    level_width = width * level / 100.0
    if level_width <= 0:
        return

    fill_width = max(height, level_width)
    fill_width = min(fill_width, width)
    radius = min(5.0, height / 2.0)
    r, g, b = _level_color(level)

    _rounded_rect(cr, x, y, fill_width, height, radius)
    cr.set_source_rgb(r, g, b)
    cr.fill()

    highlight_width = max(0.0, fill_width - 4.0)
    if highlight_width > 0:
        _rounded_rect(cr, x + 2.0, y + 1.0, highlight_width, max(1.2, height * 0.28), radius / 2.0)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.18)
        cr.fill()

    peak_x = x + min(width - 2.0, max(0.0, level_width - 1.5))
    cr.set_source_rgba(1.0, 1.0, 1.0, 0.65)
    cr.rectangle(peak_x, y + 1.0, 1.5, max(1.0, height - 2.0))
    cr.fill()


def _draw_track(cr, x, y, width, height, radius):
    _rounded_rect(cr, x, y, width, height, radius)
    cr.set_source_rgb(0.11, 0.14, 0.18)
    cr.fill()

    _rounded_rect(cr, x + 0.5, y + 0.5, max(1.0, width - 1.0), max(1.0, height - 1.0), max(1.0, radius - 0.5))
    cr.set_source_rgba(1.0, 1.0, 1.0, 0.05)
    cr.set_line_width(0.8)
    cr.stroke()


def _level_color(level):
    if level > 85:
        return 0.96, 0.29, 0.24
    if level > 70:
        return 0.94, 0.73, 0.21
    return 0.12, 0.84, 0.48


def _rounded_rect(cr, x, y, width, height, radius):
    radius = max(0.0, min(radius, width / 2.0, height / 2.0))
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -1.5708, 0.0)
    cr.arc(x + width - radius, y + height - radius, radius, 0.0, 1.5708)
    cr.arc(x + radius, y + height - radius, radius, 1.5708, 3.1416)
    cr.arc(x + radius, y + radius, radius, 3.1416, 4.7124)
    cr.close_path()
