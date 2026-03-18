#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ODR Media Player V1.1 — Python 3 + GTK3 + GStreamer
Architecture : GStreamer pipeline → ALSA Loopback → odr-audioenc → ZMQ → odr-dabmux
"""

import gi
gi.require_version('Gdk', '3.0')
gi.require_version('Gtk', '3.0')
gi.require_version('Gst', '1.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gdk, Gtk, GLib, Gst, GdkPixbuf
from gi.repository import Gio

import os, random, shutil, subprocess, threading, time
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from encodeur_dab_app.constants import (
    BITRATES,
    CONF_FILE,
    DAB_LOGO_FILE,
    DLS_FILE,
    PAD_LENGTHS,
    SLIDE_DUMP,
    SLIDE_INPUT_FILE,
)
from encodeur_dab_app.app_config import (
    AppConfig,
    REPEAT_MODE_ALL,
    REPEAT_MODE_FOLDER,
    REPEAT_MODE_OFF,
    normalize_repeat_mode,
)
from encodeur_dab_app.config_store import read_config_file, write_flat_config
from encodeur_dab_app.dls import build_dls_content
from encodeur_dab_app.encoder import (
    EncoderOptions,
    build_audio_cmd,
    build_pad_cmd,
    channels_label,
    codec_label,
    decode_exit_status,
    is_running,
    output_endpoint_parts,
    parse_audioenc_chunk,
    set_nonblocking,
    use_pad,
)
from encodeur_dab_app.media import (
    build_sls_slide_set,
    cleanup_pad_artifacts,
    detect_loop_card,
    estimate_sls_delivery,
    import_default_logo,
    load_playlist_entries,
    list_audio_files,
    normalize_default_logo_paths,
    prepare_slide_image,
    probe_audio_tags,
    resolve_track_artwork,
    remove_default_logo,
    should_ignore_audio_file,
    split_app_audio_title,
    split_artist_title,
    try_load_loopback_module,
)
from encodeur_dab_app.monitor import draw_vu, gst_peak_to_vu, read_monitor_snapshot
from encodeur_dab_app.player import (
    build_pipeline,
    build_playlist_entry,
    is_pulse_monitor_source,
    is_pulse_source,
    is_stream_url,
    now_playing_label,
    pulse_monitor_title,
    pulse_source_title,
)
from encodeur_dab_app.playlist_model import PlaylistModel, Track
from encodeur_dab_app.pulseaudio import (
    capture_monitor_source_name,
    current_captured_app_info,
    list_audio_applications,
    list_audio_inputs,
    route_app_to_capture,
)
from encodeur_dab_app.runtime_state import RuntimeState
from encodeur_dab_app.ui import set_status_label_markup, show_message
from encodeur_dab_app.view_builders import build_ui


class ODRFilePlayer(Gtk.Window):
    PLAYER_UNITY_VOLUME = 100
    LOG_MAX_LINES = 5000
    LOG_TRIM_TO_LINES = 4000
    PLAYLIST_GROUP_PREFIX_FOLDER = "folder:"
    PLAYLIST_GROUP_STREAMS = "__streams__"
    PLAYLIST_GROUP_APP_AUDIO = "__app_audio__"
    PLAYLIST_GROUP_AUDIO_INPUTS = "__audio_inputs__"
    PLAYLIST_GROUP_MISC = "__misc__"
    AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".aac", ".m4a", ".opus"}
    REPEAT_MODE_OPTIONS = (
        (REPEAT_MODE_OFF, "Repeat off"),
        (REPEAT_MODE_ALL, "Repeat all"),
        (REPEAT_MODE_FOLDER, "Repeat folder"),
    )

    def __init__(self):
        super().__init__(title="ODR Media Player V1.1")
        self._apply_window_icon()
        self._set_initial_window_size()
        self.connect("delete-event", self.on_close)

        # ---- État lecteur ----
        self.playlist = PlaylistModel()

        # ---- État encodeur ----
        self.runtime = RuntimeState()
        self.default_logo_paths = []
        self.last_logo_dir = ""
        self.preview_window = None
        self.preview_image = None
        self.preview_window_path = ""
        self.preview_window_live_output = False
        self.current_output_slide_path = ""
        self._last_app_audio_title_refresh = 0.0
        self._pending_cover_fetch_keys = set()
        self._playlist_import_running = False
        self.repeat_mode = REPEAT_MODE_OFF
        self.playlist_group_enabled = {}
        self.playlist_group_expanded = {}
        self.playlist_folder_roots = []
        self._playlist_model_refreshing = False
        self._playlist_reorder_sync_pending = False
        self._applying_config = False
        self._folder_monitors = {}
        self._folder_rescan_sources = {}
        self._folder_scan_in_progress = set()
        self._player_seek_updating = False
        self._player_seek_dragging = False

        # ---- Init GStreamer ----
        Gst.init(None)

        # ---- Construction UI ----
        self._build_ui()
        self._set_repeat_mode(self.repeat_mode)
        self._refresh_dls_controls()
        self.show_all()
        GLib.idle_add(self._fit_window_to_workarea)

        # ---- Initialisation app ----
        GLib.idle_add(self._init_app)

    def _apply_window_icon(self):
        if not DAB_LOGO_FILE or not os.path.isfile(DAB_LOGO_FILE):
            return
        try:
            Gtk.Window.set_default_icon_from_file(DAB_LOGO_FILE)
        except Exception:
            pass
        try:
            self.set_icon_from_file(DAB_LOGO_FILE)
        except Exception:
            pass

    def _set_initial_window_size(self):
        workarea = self._get_monitor_workarea()
        if workarea is None:
            self.set_default_size(1430, 900)
            return

        width = min(1430, max(980, workarea.width - 60))
        height = min(860, max(680, workarea.height - 50))
        self.set_default_size(width, height)

    def _get_monitor_workarea(self):
        display = Gdk.Display.get_default()
        if display is None:
            return None

        monitor = display.get_primary_monitor()
        if monitor is None:
            screen = Gdk.Screen.get_default()
            if screen is None:
                return None
            rect = Gdk.Rectangle()
            rect.x = 0
            rect.y = 0
            rect.width = screen.get_width()
            rect.height = screen.get_height()
            return rect
        return monitor.get_workarea()

    def _fit_window_to_workarea(self):
        workarea = self._get_monitor_workarea()
        if workarea is None:
            return False

        width, height = self.get_size()
        target_width = min(width, max(980, workarea.width - 40))
        target_height = min(height, max(680, workarea.height - 40))
        self.resize(target_width, target_height)
        self.move(max(workarea.x, 0), max(workarea.y, 0))
        return False

    # ============================================================
    # INITIALISATION
    # ============================================================
    def _init_app(self):
        try_load_loopback_module()
        self.runtime.loop_card = detect_loop_card()

        if self.runtime.loop_card < 0:
            self.lbl_src_info.set_text("Source: Loopback NOT DETECTED  (snd-aloop missing)")
            self.log("WARNING: ALSA Loopback card not found.")
            self.log("  The snd-aloop module is not loaded.")
            self.log("  Load it manually: sudo modprobe snd-aloop")
        else:
            self.lbl_src_info.set_text(
                f"Source: Native ALSA Loopback  (hw:{self.runtime.loop_card},1)")

        self.load_config()
        self.write_dls_file()
        GLib.timeout_add(1000, self._on_status_timer)
        GLib.timeout_add_seconds(1, self._schedule_initial_folder_watch_scan)
        GLib.idle_add(self._run_autostart_actions)

        if self.runtime.loop_card >= 0:
            self.log(f"ODR Media Player started.  Loopback card: hw:{self.runtime.loop_card}")
        else:
            self.log("ODR Media Player started.  Loopback card: not detected.")
        return False

    def _schedule_initial_folder_watch_scan(self):
        if self._playlist_import_running:
            return True
        for folder in self._watched_folder_paths():
            self._schedule_folder_rescan(folder)
        return False

    def _run_autostart_actions(self):
        if getattr(self, "chk_encoder_autostart", None) is not None and self.chk_encoder_autostart.get_active():
            self.log("Autostart enabled for encoder.")
            self._start_all()

        if getattr(self, "chk_playlist_autostart", None) is not None and self.chk_playlist_autostart.get_active():
            GLib.timeout_add(250, self._autostart_playlist)
        return False

    def _autostart_playlist(self):
        if self._playlist_import_running:
            return True
        if self.runtime.proc_player is not None:
            return False
        if not self.playlist:
            self.log("Autostart playlist enabled, but the playlist is empty.")
            return False
        if self.runtime.loop_card < 0:
            self.log("Autostart playlist skipped: ALSA Loopback not detected.")
            return False

        idx = self._ensure_playable_current()
        if idx is None or idx < 0:
            self.log("Autostart playlist skipped: no enabled track found.")
            return False

        self.log("Autostart enabled for playlist.")
        self._play_track(idx)
        return False

    # ============================================================
    # CONSTRUCTION UI
    # ============================================================
    def _get_output_uri(self):
        host = self.txt_output_host.get_text().strip() or "localhost"
        port = int(self.spn_output_port.get_value()) or 9000
        addr = f"{host}:{port}"
        if self.cmb_output_proto.get_active() == 1:
            return f"udp://{addr}"
        return f"tcp://{addr}"

    def _build_ui(self):
        build_ui(self)
        self.notebook.connect("switch-page", self.on_notebook_switch_page)
        self._bind_encoder_settings_watchers()

    def _bind_encoder_settings_watchers(self):
        watched = [
            (self.cmb_bitrate, "changed"),
            (self.cmb_channels, "changed"),
            (self.cmb_samplerate, "changed"),
            (self.cmb_codec, "changed"),
            (self.cmb_pad_len, "changed"),
            (self.spn_gain, "value-changed"),
            (self.cmb_output_proto, "changed"),
            (self.txt_output_host, "changed"),
            (self.spn_output_port, "value-changed"),
        ]
        for widget, signal in watched:
            widget.connect(signal, self.on_encoder_settings_changed)
        self.spn_silence.connect("value-changed", self._on_silence_warning_changed)

    def _on_silence_warning_changed(self, widget):
        self._update_silence_warning_state(force_recompute=True)
        self._update_status()

    def on_notebook_switch_page(self, notebook, page, page_num):
        GLib.idle_add(self._clear_default_dls_selection)

    def _clear_default_dls_selection(self):
        if not hasattr(self, "txt_dls"):
            return False
        try:
            cursor_pos = self.txt_dls.get_position()
        except Exception:
            cursor_pos = 0
        if cursor_pos is None or cursor_pos < 0:
            cursor_pos = len(self.txt_dls.get_text() or "")
        try:
            self.txt_dls.select_region(cursor_pos, cursor_pos)
        except Exception:
            pass
        return False

    # ============================================================
    # VU MÈTRE — dessin Cairo direct
    # ============================================================
    def _draw_player_vu(self, widget, cr):
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        draw_vu(cr, w, h, self.runtime.player_vu_left, self.runtime.player_vu_right)

    def _draw_monitor_vu(self, widget, cr):
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        draw_vu(cr, w, h, self.runtime.monitor_vu_left, self.runtime.monitor_vu_right)

    def _refresh_player_vu(self):
        if hasattr(self, "dwa_player_vu"):
            self.dwa_player_vu.queue_draw()

    def _refresh_monitor_vu(self):
        self.dwa_vu.queue_draw()

    # ============================================================
    # DÉTECTION LOOPBACK
    # ============================================================
    # ============================================================
    # PLAYLIST
    # ============================================================
    def _playlist_group_key(self, track):
        if track is None:
            return self.PLAYLIST_GROUP_MISC
        if is_pulse_monitor_source(track.path):
            return self.PLAYLIST_GROUP_APP_AUDIO
        if is_pulse_source(track.path):
            return self.PLAYLIST_GROUP_AUDIO_INPUTS
        if is_stream_url(track.path):
            return self.PLAYLIST_GROUP_STREAMS
        folder = os.path.dirname(track.path or "").strip()
        if folder:
            return f"{self.PLAYLIST_GROUP_PREFIX_FOLDER}{folder}"
        return self.PLAYLIST_GROUP_MISC

    def _playlist_group_label(self, group_key, count):
        if group_key.startswith(self.PLAYLIST_GROUP_PREFIX_FOLDER):
            folder_path = group_key[len(self.PLAYLIST_GROUP_PREFIX_FOLDER):]
            name = os.path.basename(folder_path.rstrip(os.sep)) or folder_path or "Music folder"
        elif group_key == self.PLAYLIST_GROUP_STREAMS:
            name = "Stream URLs"
        elif group_key == self.PLAYLIST_GROUP_APP_AUDIO:
            name = "App audio"
        elif group_key == self.PLAYLIST_GROUP_AUDIO_INPUTS:
            name = "Audio inputs"
        else:
            name = "Other sources"
        suffix = "track" if count == 1 else "tracks"
        return f"{name}  ({count} {suffix})"

    def _playlist_group_default_expanded(self, group_key):
        return not group_key.startswith(self.PLAYLIST_GROUP_PREFIX_FOLDER)

    def _playlist_group_enabled_state(self, group_key):
        return bool(self.playlist_group_enabled.get(group_key, True))

    def _is_track_enabled(self, track):
        return self._playlist_group_enabled_state(self._playlist_group_key(track))

    def _is_local_audio_track(self, track):
        return track is not None and not is_stream_url(track.path) and os.path.isfile(track.path)

    def _watched_folder_paths(self):
        folders = []
        seen = set()
        for folder in self.playlist_folder_roots:
            folder = (folder or "").strip()
            if not folder or not os.path.isdir(folder) or folder in seen:
                continue
            seen.add(folder)
            folders.append(folder)
        for track in self.playlist:
            if not self._is_local_audio_track(track):
                continue
            folder = os.path.dirname(track.path or "").strip()
            if not folder or not os.path.isdir(folder) or folder in seen:
                continue
            seen.add(folder)
            folders.append(folder)
        return folders

    def _is_relevant_audio_path(self, path):
        if not path:
            return False
        if should_ignore_audio_file(path):
            return False
        return os.path.splitext(path)[1].lower() in self.AUDIO_EXTENSIONS

    def _add_playlist_folder_root(self, folder):
        folder = (folder or "").strip()
        if not folder or not os.path.isdir(folder):
            return
        if folder not in self.playlist_folder_roots:
            self.playlist_folder_roots.append(folder)

    def _ensure_folder_monitor(self, folder):
        folder = (folder or "").strip()
        if not folder or not os.path.isdir(folder):
            return False
        if folder in self._folder_monitors:
            return False
        try:
            monitor = Gio.File.new_for_path(folder).monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES,
                None,
            )
        except Exception as exc:
            self.log(f"Folder watch unavailable for {folder}: {exc}")
            return False
        monitor.connect("changed", self._on_folder_monitor_changed, folder)
        self._folder_monitors[folder] = monitor
        return True

    def _playable_indices(self):
        return [i for i, track in enumerate(self.playlist) if self._is_track_enabled(track)]

    def _playable_indices_for_group(self, group_key):
        if not group_key:
            return []
        return [
            i
            for i, track in enumerate(self.playlist)
            if self._is_track_enabled(track) and self._playlist_group_key(track) == group_key
        ]

    def _first_playable_index(self):
        playable = self._playable_indices()
        return playable[0] if playable else None

    def _ensure_playable_current(self):
        if 0 <= self.playlist.current_idx < len(self.playlist):
            current = self.playlist.current_track()
            if current is not None and self._is_track_enabled(current):
                return self.playlist.current_idx
        first = self._first_playable_index()
        self.playlist.current_idx = -1 if first is None else first
        return first

    def _repeat_mode_label(self, mode):
        mode = normalize_repeat_mode(mode)
        for option_mode, label in self.REPEAT_MODE_OPTIONS:
            if option_mode == mode:
                return label
        return "Repeat off"

    def _set_repeat_mode(self, mode):
        self.repeat_mode = normalize_repeat_mode(mode)
        if hasattr(self, "lbl_repeat_mode") and self.lbl_repeat_mode is not None:
            self.lbl_repeat_mode.set_text(self._repeat_mode_label(self.repeat_mode))

    def on_repeat_mode_selected(self, _btn, mode):
        self._set_repeat_mode(mode)
        if hasattr(self, "repeat_mode_popover") and self.repeat_mode_popover is not None:
            self.repeat_mode_popover.popdown()

    def _repeat_mode(self):
        return normalize_repeat_mode(getattr(self, "repeat_mode", REPEAT_MODE_OFF))

    def _next_playable_index(self, shuffle_enabled=False, repeat_mode=REPEAT_MODE_OFF):
        playable = self._playable_indices()
        if not playable:
            return None
        current_idx = self.playlist.current_idx
        current_track = self.playlist.current_track()
        if repeat_mode == REPEAT_MODE_FOLDER and current_track is not None:
            group_key = self._playlist_group_key(current_track)
            scoped_playable = self._playable_indices_for_group(group_key)
            if scoped_playable:
                if shuffle_enabled:
                    return random.choice(scoped_playable)
                for idx in scoped_playable:
                    if idx > current_idx:
                        return idx
                return scoped_playable[0]
        if shuffle_enabled:
            return random.choice(playable)
        for idx in playable:
            if idx > current_idx:
                return idx
        if repeat_mode == REPEAT_MODE_ALL:
            return playable[0]
        return None

    def _previous_playable_index(self):
        playable = self._playable_indices()
        if not playable:
            return None
        current_idx = self.playlist.current_idx
        previous = [idx for idx in playable if idx < current_idx]
        if previous:
            return previous[-1]
        return playable[0]

    def _now_playing_markup(self, track=None):
        if track is None:
            return "—"

        base_label = GLib.markup_escape_text(now_playing_label(track))
        if is_pulse_monitor_source(track.path):
            return f"{base_label} <b>- App audio</b>"
        if is_pulse_source(track.path):
            return f"{base_label} <b>- Audio input</b>"
        if is_stream_url(track.path):
            return f"{base_label} <b>- Stream URL</b>"

        folder = os.path.basename(os.path.dirname(track.path.rstrip(os.sep)))
        folder = (folder or "").strip()
        if not folder:
            return base_label
        folder_markup = GLib.markup_escape_text(folder)
        return f"{base_label} <b>- {folder_markup}</b>"

    def _set_now_playing_label(self, track=None):
        self.lbl_now.set_markup(self._now_playing_markup(track))

    def _reset_playback_display(self):
        self._highlight_current(-1)
        self._set_now_playing_label()
        self._reset_player_seek_scale()
        self.btn_play.set_label("▶  Play")
        self.write_dls_file()
        self._update_sls_source_preview()
        self._update_monitor()

    def _apply_disabled_playlist_state(self):
        was_running = self.runtime.proc_player is not None
        old_index = self.playlist.current_idx
        current_track = self.playlist.current_track()
        current_disabled = current_track is not None and not self._is_track_enabled(current_track)
        replacement = self._ensure_playable_current()
        if current_disabled:
            self.playlist.current_idx = old_index
            replacement = self._next_playable_index(
                self.chk_shuffle.get_active(),
                self._repeat_mode(),
            )
            if replacement is None:
                replacement = self._first_playable_index()
            self.playlist.current_idx = -1 if replacement is None else replacement
        if not current_disabled:
            self._refresh_pl()
            return

        if self.runtime.proc_player is not None:
            self.playlist.manual_skip = True
            self._stop_player()
            self.runtime.reset_player_recovery()

        if replacement is None:
            self.playlist.stop()
            self._reset_playback_display()
            self._refresh_pl()
            return

        self._refresh_pl()
        if was_running and self.runtime.loop_card >= 0 and not self.playlist.paused:
            self._play_track(replacement)

    def on_add_files(self, btn):
        if self._playlist_import_running:
            self._msg_info("A playlist import is already in progress.")
            return
        dlg = Gtk.FileChooserDialog(
            title="Add audio files",
            parent=self, action=Gtk.FileChooserAction.OPEN)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        dlg.set_select_multiple(True)
        flt = Gtk.FileFilter()
        flt.set_name("Audio files")
        for ext in ["*.mp3","*.wav","*.flac","*.ogg","*.aac","*.m4a","*.opus"]:
            flt.add_pattern(ext)
        dlg.add_filter(flt)
        if dlg.run() == Gtk.ResponseType.OK:
            selected_files = sorted(dlg.get_filenames())
            if selected_files:
                self._start_track_import(
                    selected_files,
                    source_label="File import",
                    loading_hint="Reading file metadata in background.",
                )
        dlg.destroy()

    def on_add_folder(self, btn):
        if self._playlist_import_running:
            self._msg_info("A playlist import is already in progress.")
            return
        dlg = Gtk.FileChooserDialog(
            title="Choose a music folder",
            parent=self, action=Gtk.FileChooserAction.SELECT_FOLDER)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        if dlg.run() == Gtk.ResponseType.OK:
            folder = dlg.get_filename()
            if folder:
                self._start_track_import(
                    folder=folder,
                    watch_root=folder,
                    source_label="Folder import",
                    loading_hint="Scanning the selected folder in background.",
                )
        dlg.destroy()

    def on_add_playlist(self, btn):
        dlg = Gtk.FileChooserDialog(
            title="Add playlist file",
            parent=self, action=Gtk.FileChooserAction.OPEN)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        dlg.set_select_multiple(True)
        flt = Gtk.FileFilter()
        flt.set_name("Playlist files")
        for ext in ["*.m3u", "*.m3u8", "*.pls"]:
            flt.add_pattern(ext)
            flt.add_pattern(ext.upper())
        dlg.add_filter(flt)
        all_filter = Gtk.FileFilter()
        all_filter.set_name("All files")
        all_filter.add_pattern("*")
        dlg.add_filter(all_filter)

        if dlg.run() == Gtk.ResponseType.OK:
            imported = 0
            skipped = 0
            for playlist_path in dlg.get_filenames():
                try:
                    entries = load_playlist_entries(playlist_path)
                except Exception as exc:
                    self.log(f"Playlist import error ({os.path.basename(playlist_path)}): {exc}")
                    continue

                base_url = ""
                sample_relative = next(
                    (
                        (entry.get("path") or "").strip()
                        for entry in entries
                        if self._looks_like_server_relative_playlist_entry((entry.get("path") or "").strip())
                    ),
                    "",
                )
                if sample_relative:
                    base_url = self._prompt_playlist_base_url(
                        os.path.basename(playlist_path),
                        sample_relative,
                    )
                    if not base_url:
                        self.log(
                            f"Playlist import cancelled ({os.path.basename(playlist_path)}): "
                            "base URL required for server-relative entries."
                        )
                        continue

                for entry in entries:
                    path = (entry.get("path") or "").strip()
                    title_hint = (entry.get("title") or "").strip()
                    if not path:
                        continue
                    if base_url and self._looks_like_server_relative_playlist_entry(path):
                        path = urllib.parse.urljoin(base_url.rstrip("/") + "/", path)
                    if is_stream_url(path):
                        self._add_stream_url(path, title_hint=title_hint)
                        imported += 1
                    elif os.path.isfile(path):
                        if self._add_file(path) is not None:
                            imported += 1
                        else:
                            skipped += 1
                    else:
                        skipped += 1

            self._refresh_pl()
            if imported:
                self.log(f"Playlist import: {imported} entries added.")
            if skipped:
                self.log(f"Playlist import: {skipped} entries skipped (missing or unsupported).")
        dlg.destroy()

    def _looks_like_server_relative_playlist_entry(self, path):
        path = (path or "").strip()
        if not path:
            return False
        if is_stream_url(path):
            return False
        if os.path.isfile(path):
            return False
        if re.match(r"^[A-Za-z]:[\\\\/]", path):
            return False
        return path.startswith("/")

    def _prompt_playlist_base_url(self, playlist_name, sample_path):
        dlg = Gtk.Dialog(
            title="Playlist base URL",
            parent=self,
            modal=True,
        )
        dlg.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK,
        )
        dlg.set_default_response(Gtk.ResponseType.OK)

        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)

        title = Gtk.Label(xalign=0)
        title.set_use_markup(True)
        title.set_markup(
            "<b>This playlist contains server-relative entries.</b>"
        )
        box.pack_start(title, False, False, 0)

        hint = Gtk.Label(xalign=0)
        hint.set_use_markup(True)
        hint.set_line_wrap(True)
        hint.set_max_width_chars(54)
        hint.set_markup(
            "Enter the base URL to complete paths like "
            f"<tt>{GLib.markup_escape_text(sample_path)}</tt>\n"
            f"Playlist: <tt>{GLib.markup_escape_text(playlist_name)}</tt>"
        )
        box.pack_start(hint, False, False, 0)

        entry = Gtk.Entry()
        entry.set_placeholder_text("http://host:port")
        entry.set_activates_default(True)
        box.pack_start(entry, False, False, 0)

        dlg.show_all()
        base_url = ""
        if dlg.run() == Gtk.ResponseType.OK:
            candidate = entry.get_text().strip()
            parsed = urllib.parse.urlparse(candidate)
            if parsed.scheme and parsed.netloc:
                base_url = candidate
            else:
                self._msg_warn("Invalid base URL.\nPlease enter a full URL such as http://host:port")
        dlg.destroy()
        return base_url

    def _start_track_import(self, paths=None, folder="", watch_root="", source_label="Track import", loading_hint=""):
        if self._playlist_import_running:
            return
        path_count = len(paths or ())
        if folder:
            self.log(f"{source_label}: started for {folder}")
        elif path_count:
            self.log(f"{source_label}: started for {path_count} selected file(s)")
        self._set_playlist_import_state(True, loading_hint or "Loading tracks in background.")
        worker = threading.Thread(
            target=self._run_track_import_worker,
            args=(tuple(paths or ()), folder or "", watch_root or "", source_label),
            daemon=True,
        )
        worker.start()

    def _run_track_import_worker(self, paths, folder, watch_root, source_label):
        imported_tracks = []
        skipped = 0
        error_message = ""

        try:
            candidate_paths = list_audio_files(folder) if folder else list(paths)
            for path in candidate_paths:
                track = self._probe_track_file(path)
                if track is None:
                    skipped += 1
                    continue
                imported_tracks.append(track)
        except Exception as exc:
            error_message = str(exc)

        GLib.idle_add(
            self._finish_track_import,
            imported_tracks,
            skipped,
            watch_root,
            source_label,
            error_message,
        )

    def _finish_track_import(self, imported_tracks, skipped, watch_root, source_label, error_message):
        try:
            if watch_root:
                self._add_playlist_folder_root(watch_root)
            if imported_tracks:
                for track in imported_tracks:
                    self.playlist.append(track)
                self._refresh_pl()
                self.log(f"{source_label}: {len(imported_tracks)} track(s) added.")

            if skipped:
                self.log(f"{source_label}: {skipped} item(s) skipped.")

            if error_message:
                self.log(f"{source_label} error: {error_message}")
        finally:
            self._set_playlist_import_state(False)
        return False

    def _start_config_playlist_load(self, playlist_paths, playlist_overrides):
        if self._playlist_import_running:
            return
        entry_count = len(playlist_paths or ())
        self.log(f"Saved playlist: loading {entry_count} entry(s) in background.")
        self._set_playlist_import_state(True, "Loading saved playlist in background.")
        worker = threading.Thread(
            target=self._run_config_playlist_load_worker,
            args=(tuple(playlist_paths or ()), dict(playlist_overrides or {})),
            daemon=True,
        )
        worker.start()

    def _run_config_playlist_load_worker(self, playlist_paths, playlist_overrides):
        skipped = 0
        error_message = ""

        try:
            invalid_indices = set()
            resolved = {}
            next_emit_index = 0
            emitted_count = 0
            emit_batch = []
            emit_skipped = 0
            batch_size = 24
            max_workers = min(8, max(2, os.cpu_count() or 4))
            future_map = {}

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                for index, path in enumerate(playlist_paths):
                    if os.path.isfile(path):
                        future = executor.submit(self._probe_track_file, path)
                        future_map[future] = index
                    elif is_stream_url(path):
                        resolved[index] = self._build_track_for_saved_source(path)
                    else:
                        invalid_indices.add(index)

                for future in as_completed(future_map):
                    index = future_map[future]
                    try:
                        resolved[index] = future.result()
                    except Exception:
                        resolved[index] = None

                    while next_emit_index < len(playlist_paths):
                        if next_emit_index in invalid_indices:
                            emit_skipped += 1
                            skipped += 1
                            next_emit_index += 1
                            continue
                        if next_emit_index not in resolved:
                            break

                        track = resolved.pop(next_emit_index)
                        if track is None:
                            emit_skipped += 1
                            skipped += 1
                        else:
                            override = playlist_overrides.get(str(next_emit_index))
                            if isinstance(override, dict):
                                track.artist = str(override.get("artist", "") or "")
                                track.title = str(override.get("title", "") or "")
                                track.album = str(override.get("album", "") or "")
                                track.manual_metadata = True
                            emit_batch.append(track)
                            emitted_count += 1
                        next_emit_index += 1

                        if len(emit_batch) >= batch_size:
                            GLib.idle_add(
                                self._append_config_playlist_batch,
                                list(emit_batch),
                                emitted_count,
                                len(playlist_paths),
                            )
                            emit_batch.clear()
                        elif emit_skipped >= batch_size:
                            GLib.idle_add(
                                self._append_config_playlist_batch,
                                [],
                                emitted_count,
                                len(playlist_paths),
                            )
                            emit_skipped = 0

            while next_emit_index < len(playlist_paths):
                if next_emit_index in invalid_indices:
                    emit_skipped += 1
                    skipped += 1
                    next_emit_index += 1
                    continue
                if next_emit_index not in resolved:
                    break
                track = resolved.pop(next_emit_index)
                if track is None:
                    emit_skipped += 1
                    skipped += 1
                else:
                    override = playlist_overrides.get(str(next_emit_index))
                    if isinstance(override, dict):
                        track.artist = str(override.get("artist", "") or "")
                        track.title = str(override.get("title", "") or "")
                        track.album = str(override.get("album", "") or "")
                        track.manual_metadata = True
                    emit_batch.append(track)
                    emitted_count += 1
                next_emit_index += 1

            if emit_batch or emit_skipped:
                GLib.idle_add(
                    self._append_config_playlist_batch,
                    list(emit_batch),
                    emitted_count,
                    len(playlist_paths),
                )
        except Exception as exc:
            error_message = str(exc)

        GLib.idle_add(
            self._finish_config_playlist_load,
            emitted_count,
            skipped,
            error_message,
        )

    def _build_track_for_saved_source(self, path):
        title_hint = ""
        duration = "?"
        if is_pulse_monitor_source(path):
            title_hint = pulse_monitor_title(path)
            duration = "LIVE"
        elif is_pulse_source(path):
            title_hint = pulse_source_title(path)
            duration = "LIVE"
        return Track(
            path=path,
            artist="",
            title=title_hint,
            album="",
            duration=duration,
        )

    def _append_config_playlist_batch(self, tracks, loaded_count, total_count):
        if tracks:
            for track in tracks:
                self.playlist.append(track)
            self._refresh_pl()
        if self._playlist_import_running and hasattr(self, "player_empty_hint"):
            self.player_empty_hint.set_markup(
                f"<i>Loading saved playlist in background. {loaded_count}/{total_count} ready.</i>"
            )
        return False

    def _finish_config_playlist_load(self, loaded_count, skipped, error_message):
        try:
            if self.playlist:
                self._ensure_playable_current()
                self._refresh_pl()
            if loaded_count:
                self.log(f"Saved playlist: {loaded_count} track(s) loaded.")
            if skipped:
                self.log(f"Saved playlist: {skipped} item(s) skipped.")
            if error_message:
                self.log(f"Saved playlist error: {error_message}")
        finally:
            self._set_playlist_import_state(False)
        return False

    def _set_playlist_import_state(self, active, loading_hint=""):
        self._playlist_import_running = bool(active)

        if hasattr(self, "player_actions_bar"):
            self.player_actions_bar.set_sensitive(not active)

        gdk_window = self.get_window()
        if gdk_window is not None:
            if active:
                display = Gdk.Display.get_default()
                cursor = None
                if display is not None:
                    cursor = Gdk.Cursor.new_for_display(display, Gdk.CursorType.WATCH)
                gdk_window.set_cursor(cursor)
            else:
                gdk_window.set_cursor(None)

        if hasattr(self, "player_empty_title"):
            title_markup = "<b>Loading tracks…</b>" if active else "<b>No tracks loaded</b>"
            self.player_empty_title.set_markup(title_markup)
        if hasattr(self, "player_empty_hint"):
            hint_markup = (
                f"<i>{GLib.markup_escape_text(loading_hint or 'Reading audio metadata in background.')}</i>"
                if active else
                "<i>Use Add or Folder to build the playlist.</i>"
            )
            self.player_empty_hint.set_markup(hint_markup)

        self._refresh_player_empty_state()

    def on_add_url(self, btn):
        dlg = Gtk.Dialog(
            title="Add stream URL",
            parent=self,
            modal=True,
        )
        dlg.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_ADD, Gtk.ResponseType.OK,
        )
        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)

        box.pack_start(Gtk.Label(label="Stream URL:", xalign=0), False, False, 0)
        entry = Gtk.Entry()
        entry.set_placeholder_text("https://example.com/stream")
        entry.set_activates_default(True)
        box.pack_start(entry, False, False, 0)

        hint = Gtk.Label(xalign=0)
        hint.set_use_markup(True)
        hint.set_markup("<i>HTTP(S), ICY and other URI-based GStreamer sources are accepted.</i>")
        box.pack_start(hint, False, False, 0)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.show_all()

        if dlg.run() == Gtk.ResponseType.OK:
            url = entry.get_text().strip()
            if not is_stream_url(url):
                self._msg_warn("Invalid stream URL.\nPlease enter a full URI such as https://example.com/stream")
            else:
                self._add_stream_url(url)
                self._refresh_pl()
                self.log(f"Stream URL added: {url}")
        dlg.destroy()

    def on_add_app_audio(self, btn):
        try:
            apps = list_audio_applications()
        except Exception as exc:
            self._msg_err(f"Unable to query PulseAudio applications.\n\n{exc}")
            return

        if not apps:
            self._msg_info("No running audio application was found in PulseAudio.")
            return

        dlg = Gtk.Dialog(
            title="Capture application audio",
            parent=self,
            modal=True,
        )
        dlg.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_ADD, Gtk.ResponseType.OK,
        )
        dlg.set_default_size(720, 320)
        dlg.set_default_response(Gtk.ResponseType.OK)

        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)

        hint = Gtk.Label(xalign=0)
        hint.set_use_markup(True)
        hint.set_line_wrap(True)
        hint.set_markup(
            "<i>Select a running audio application to route it into ODR Media Player. "
            "Only one captured app is kept on the dedicated sink at a time.</i>"
        )
        box.pack_start(hint, False, False, 0)

        store = Gtk.ListStore(int, str, str, str)
        for app in apps:
            store.append(
                [
                    int(app["index"]),
                    app["app_name"],
                    app["media_name"] or "—",
                    app["process_id"] or "—",
                ]
            )

        tree = Gtk.TreeView(model=store)
        tree.set_headers_visible(True)
        for column_id, title in enumerate(("App", "Media", "PID"), start=1):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=column_id)
            column.set_resizable(True)
            tree.append_column(column)

        selection = tree.get_selection()
        selection.set_mode(Gtk.SelectionMode.SINGLE)
        if store.get_iter_first() is not None:
            selection.select_iter(store.get_iter_first())

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.add(tree)
        box.pack_start(scroll, True, True, 0)

        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            model, tree_iter = selection.get_selected()
            if tree_iter is None:
                dlg.destroy()
                self._msg_info("Select one audio application first.")
                return

            sink_input_index = int(model[tree_iter][0])
            app_name = str(model[tree_iter][1])
            media_name = str(model[tree_iter][2])
            try:
                monitor_source = route_app_to_capture(sink_input_index)
            except Exception as exc:
                dlg.destroy()
                self._msg_err(f"Unable to route the application audio.\n\n{exc}")
                return

            self._add_app_audio_track(
                monitor_source,
                f"{app_name} — {media_name}" if media_name and media_name != "—" else app_name,
                source_pid=int(model[tree_iter][3]) if str(model[tree_iter][3]).isdigit() else 0,
                source_app_name=app_name,
            )
            self._refresh_pl()
            self.save_config()
            self.log(f"App audio routed: {app_name} (sink input #{sink_input_index})")
        dlg.destroy()

    def on_add_audio_input(self, btn):
        try:
            inputs = list_audio_inputs()
        except Exception as exc:
            self._msg_err(f"Unable to query PulseAudio inputs.\n\n{exc}")
            return

        if not inputs:
            self._msg_info("No audio input source was found in PulseAudio.")
            return

        dlg = Gtk.Dialog(
            title="Add audio input",
            parent=self,
            modal=True,
        )
        dlg.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_ADD, Gtk.ResponseType.OK,
        )
        dlg.set_default_size(780, 320)
        dlg.set_default_response(Gtk.ResponseType.OK)

        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)

        hint = Gtk.Label(xalign=0)
        hint.set_use_markup(True)
        hint.set_line_wrap(True)
        hint.set_markup(
            "<i>Select a live audio input source exposed by PulseAudio. "
            "This is typically a microphone, line input, USB interface or external sound card input.</i>"
        )
        box.pack_start(hint, False, False, 0)

        store = Gtk.ListStore(str, str, str, str)
        for item in inputs:
            store.append(
                [
                    item["name"],
                    item["description"],
                    item["sample_specification"] or "—",
                    item["state"] or "—",
                ]
            )

        tree = Gtk.TreeView(model=store)
        tree.set_headers_visible(True)
        for column_id, title in enumerate(("Input", "Description", "Format", "State")):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=column_id)
            column.set_resizable(True)
            tree.append_column(column)

        selection = tree.get_selection()
        selection.set_mode(Gtk.SelectionMode.SINGLE)
        if store.get_iter_first() is not None:
            selection.select_iter(store.get_iter_first())

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.add(tree)
        box.pack_start(scroll, True, True, 0)

        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            model, tree_iter = selection.get_selected()
            if tree_iter is None:
                dlg.destroy()
                self._msg_info("Select one audio input first.")
                return

            source_name = str(model[tree_iter][0] or "").strip()
            description = str(model[tree_iter][1] or "").strip()
            if not source_name:
                dlg.destroy()
                return

            self._add_audio_input_track(source_name, description)
            self._refresh_pl()
            self.save_config()
            self.log(f"Audio input added: {description or source_name}")
        dlg.destroy()

    def _playlist_toggle_cell_data(self, column, cell, model, tree_iter, data=None):
        is_group = bool(model[tree_iter][4])
        cell.set_property("visible", is_group)
        cell.set_property("activatable", is_group)
        if is_group:
            cell.set_property("active", bool(model[tree_iter][5]))

    def _playlist_text_cell_data(self, column, cell, model, tree_iter, data=None):
        text = str(model[tree_iter][2] or "")
        is_current = bool(model[tree_iter][3])
        is_group = bool(model[tree_iter][4])
        enabled = bool(model[tree_iter][5])
        selection = self.tv_pl.get_selection() if hasattr(self, "tv_pl") else None
        is_selected = bool(selection.iter_is_selected(tree_iter)) if selection is not None else False

        cell.set_property("text", text)
        cell.set_property("weight", 700 if is_group else 400)

        if is_selected:
            cell.set_property("foreground-set", False)
            cell.set_property("background-set", False)
            return

        cell.set_property("foreground-set", True)
        cell.set_property("foreground", "#6c757d" if not enabled else ("#1f2937" if is_group else "#000000"))
        cell.set_property("background-set", bool(is_current and not is_group))
        if is_current and not is_group:
            cell.set_property("background", "#2ec27e")
            cell.set_property("foreground", "white")

    def on_playlist_group_toggled(self, cell, path):
        try:
            tree_iter = self.store_pl.get_iter(path)
        except Exception:
            return
        if tree_iter is None or not bool(self.store_pl[tree_iter][4]):
            return

        group_key = str(self.store_pl[tree_iter][1] or "")
        new_state = not bool(self.store_pl[tree_iter][5])
        self.playlist_group_enabled[group_key] = new_state
        self._refresh_pl()
        self.save_config()

        current = self.playlist.current_track()
        if current is not None and self._playlist_group_key(current) == group_key and not new_state:
            self._apply_disabled_playlist_state()

    def on_playlist_row_expanded(self, treeview, tree_iter, tree_path):
        if tree_iter is None or not bool(self.store_pl[tree_iter][4]):
            return
        group_key = str(self.store_pl[tree_iter][1] or "")
        self.playlist_group_expanded[group_key] = True

    def on_playlist_row_collapsed(self, treeview, tree_iter, tree_path):
        if tree_iter is None or not bool(self.store_pl[tree_iter][4]):
            return
        group_key = str(self.store_pl[tree_iter][1] or "")
        self.playlist_group_expanded[group_key] = False

    def on_playlist_rows_reordered(self, model, tree_path, tree_iter, new_order):
        if self._playlist_model_refreshing or self._playlist_reorder_sync_pending:
            return
        self._playlist_reorder_sync_pending = True
        GLib.idle_add(self._sync_playlist_from_tree_model)

    def _selected_tracks_from_view(self):
        tracks = []
        for idx in self._selected_indices():
            if 0 <= idx < len(self.playlist):
                tracks.append(self.playlist[idx])
        return tracks

    def _sync_playlist_from_tree_model(self):
        self._playlist_reorder_sync_pending = False
        if self._playlist_model_refreshing:
            return False

        previous_tracks = list(self.playlist.tracks)
        current_track = self.playlist.current_track()
        selected_tracks = self._selected_tracks_from_view()
        reordered_tracks = []
        next_group_enabled = {}
        next_group_expanded = {}

        root_iter = self.store_pl.get_iter_first()
        while root_iter:
            if bool(self.store_pl[root_iter][4]):
                group_key = str(self.store_pl[root_iter][1] or "")
                next_group_enabled[group_key] = bool(self.store_pl[root_iter][5])
                path = self.store_pl.get_path(root_iter)
                next_group_expanded[group_key] = bool(path is not None and self.tv_pl.row_expanded(path))

                child = self.store_pl.iter_children(root_iter)
                while child:
                    idx = int(self.store_pl[child][0])
                    if 0 <= idx < len(previous_tracks):
                        reordered_tracks.append(previous_tracks[idx])
                    child = self.store_pl.iter_next(child)
            root_iter = self.store_pl.iter_next(root_iter)

        if len(reordered_tracks) != len(previous_tracks):
            return False

        self.playlist.tracks = reordered_tracks
        self.playlist_group_enabled = next_group_enabled
        self.playlist_group_expanded = next_group_expanded
        self._restore_current_track_index(current_track, fallback_idx=0)
        self._ensure_playable_current()
        self._refresh_pl()
        self._select_tracks(selected_tracks)
        self.save_config()
        return False

    def _refresh_folder_monitors(self):
        target_folders = set(self._watched_folder_paths())

        for folder, source_id in list(self._folder_rescan_sources.items()):
            if folder in target_folders:
                continue
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass
            self._folder_rescan_sources.pop(folder, None)

        for folder, monitor in list(self._folder_monitors.items()):
            if folder in target_folders:
                continue
            try:
                monitor.cancel()
            except Exception:
                pass
            self._folder_monitors.pop(folder, None)
            self._folder_scan_in_progress.discard(folder)

        for folder in sorted(target_folders):
            created = self._ensure_folder_monitor(folder)
            if created:
                self._schedule_folder_rescan(folder)

    def _on_folder_monitor_changed(self, monitor, file_obj, other_file_obj, event_type, folder):
        relevant_events = {
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.MOVED_IN,
            Gio.FileMonitorEvent.RENAMED,
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
        }
        if event_type not in relevant_events:
            return

        candidate_paths = []
        for item in (file_obj, other_file_obj):
            if item is None:
                continue
            try:
                path = item.get_path()
            except Exception:
                path = None
            if path:
                candidate_paths.append(path)

        has_directory = any(os.path.isdir(path) for path in candidate_paths)
        if has_directory:
            for path in candidate_paths:
                if os.path.isdir(path):
                    self._ensure_folder_monitor(path)

        if candidate_paths and not has_directory and not any(self._is_relevant_audio_path(path) for path in candidate_paths):
            return
        self._schedule_folder_rescan(folder)

    def _schedule_folder_rescan(self, folder):
        existing = self._folder_rescan_sources.pop(folder, None)
        if existing:
            try:
                GLib.source_remove(existing)
            except Exception:
                pass
        self._folder_rescan_sources[folder] = GLib.timeout_add_seconds(2, self._run_folder_rescan, folder)

    def _run_folder_rescan(self, folder):
        self._folder_rescan_sources.pop(folder, None)
        if folder in self._folder_scan_in_progress:
            return False
        if not os.path.isdir(folder):
            self._refresh_folder_monitors()
            return False
        self._folder_scan_in_progress.add(folder)
        worker = threading.Thread(
            target=self._scan_folder_for_new_tracks_worker,
            args=(folder,),
            daemon=True,
        )
        worker.start()
        return False

    def _scan_folder_for_new_tracks_worker(self, folder):
        new_tracks = []
        error_message = ""
        try:
            existing_paths = {
                track.path
                for track in self.playlist
                if self._is_local_audio_track(track) and os.path.dirname(track.path or "").strip() == folder
            }
            for path in list_audio_files(folder):
                if path in existing_paths:
                    continue
                track = self._probe_track_file(path)
                if track is not None:
                    new_tracks.append(track)
        except Exception as exc:
            error_message = str(exc)
        GLib.idle_add(self._finish_folder_watch_scan, folder, new_tracks, error_message)

    def _finish_folder_watch_scan(self, folder, new_tracks, error_message):
        self._folder_scan_in_progress.discard(folder)

        added = 0
        existing_paths = {track.path for track in self.playlist}
        for track in new_tracks:
            if track.path in existing_paths:
                continue
            self.playlist.append(track)
            existing_paths.add(track.path)
            added += 1

        if added:
            self._refresh_pl()
            self.save_config()
            self.log(
                f"Folder watch: {added} new track(s) added from "
                f"{os.path.basename(folder.rstrip(os.sep)) or folder}."
            )

        if error_message:
            self.log(f"Folder watch error ({folder}): {error_message}")

        self._refresh_folder_monitors()
        return False

    def _stop_folder_monitors(self):
        for source_id in list(self._folder_rescan_sources.values()):
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass
        self._folder_rescan_sources.clear()

        for monitor in list(self._folder_monitors.values()):
            try:
                monitor.cancel()
            except Exception:
                pass
        self._folder_monitors.clear()
        self._folder_scan_in_progress.clear()

    def on_edit_entry(self, btn):
        selection = self.tv_pl.get_selection()
        _, paths = selection.get_selected_rows()
        if not paths:
            self._msg_info("Select one playlist entry to edit.")
            return
        if len(paths) != 1 or self._selection_contains_group_row():
            self._msg_warn("Edit works with a single track entry.")
            return
        idx = self._selected_single_track_index()
        if idx is None:
            self._msg_info("Select one playlist entry to edit.")
            return
        if idx < 0 or idx >= len(self.playlist):
            return
        track = self.playlist[idx]
        is_stream = is_stream_url(track.path)
        previous_path = track.path

        dlg = Gtk.Dialog(
            title="Edit playlist entry",
            parent=self,
            modal=True,
        )
        dlg.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_SAVE, Gtk.ResponseType.OK,
        )
        dlg.set_default_response(Gtk.ResponseType.OK)

        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)

        grid = Gtk.Grid(row_spacing=8, column_spacing=8)
        box.pack_start(grid, False, False, 0)

        row = 0
        kind_label = Gtk.Label(label="Type:", xalign=1)
        kind_value = Gtk.Label(label="Stream URL" if is_stream else "Local file", xalign=0)
        kind_value.set_selectable(True)
        grid.attach(kind_label, 0, row, 1, 1)
        grid.attach(kind_value, 1, row, 1, 1)

        row += 1
        path_label = Gtk.Label(label="Location:", xalign=1)
        path_entry = Gtk.Entry()
        path_entry.set_text(track.path)
        path_entry.set_editable(is_stream)
        path_entry.set_width_chars(56)
        path_entry.set_hexpand(True)
        path_entry.set_tooltip_text(track.path)
        grid.attach(path_label, 0, row, 1, 1)
        grid.attach(path_entry, 1, row, 1, 1)

        if not is_stream:
            row += 1
            hint = Gtk.Label(xalign=0)
            hint.set_use_markup(True)
            hint.set_markup("<i>Local file paths are read-only. Edit the display metadata below.</i>")
            grid.attach(Gtk.Label(label="", xalign=1), 0, row, 1, 1)
            grid.attach(hint, 1, row, 1, 1)

        row += 1
        artist_entry = Gtk.Entry()
        artist_entry.set_text(track.artist)
        artist_entry.set_hexpand(True)
        grid.attach(Gtk.Label(label="Artist:", xalign=1), 0, row, 1, 1)
        grid.attach(artist_entry, 1, row, 1, 1)

        row += 1
        title_entry = Gtk.Entry()
        title_entry.set_text(track.title)
        title_entry.set_hexpand(True)
        grid.attach(Gtk.Label(label="Title:", xalign=1), 0, row, 1, 1)
        grid.attach(title_entry, 1, row, 1, 1)

        row += 1
        album_entry = Gtk.Entry()
        album_entry.set_text(track.album)
        album_entry.set_hexpand(True)
        grid.attach(Gtk.Label(label="Album:", xalign=1), 0, row, 1, 1)
        grid.attach(album_entry, 1, row, 1, 1)

        row += 1
        duration_value = Gtk.Label(label=track.duration or "?", xalign=0)
        duration_value.set_selectable(True)
        grid.attach(Gtk.Label(label="Duration:", xalign=1), 0, row, 1, 1)
        grid.attach(duration_value, 1, row, 1, 1)

        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            new_path = path_entry.get_text().strip()
            if is_stream and not is_stream_url(new_path):
                self._msg_warn("Invalid stream URL.\nPlease enter a full URI such as https://example.com/stream")
                dlg.destroy()
                return

            artist_changed = artist_entry.get_text() != track.artist
            title_changed = title_entry.get_text() != track.title
            album_changed = album_entry.get_text() != track.album
            metadata_changed = artist_changed or title_changed or album_changed

            if is_stream:
                track.path = new_path
            track.artist = artist_entry.get_text()
            track.title = title_entry.get_text()
            track.album = album_entry.get_text()
            if metadata_changed:
                track.manual_metadata = True

            self._refresh_pl()
            self._highlight_current(self.playlist.current_idx)

            if idx == self.playlist.current_idx:
                if self.runtime.proc_player is not None and is_stream and previous_path != track.path:
                    self._play_track(idx, start_paused=self.playlist.paused)
                    self.save_config()
                    self.log("Playlist entry updated.")
                    dlg.destroy()
                    return
                self._set_now_playing_label(track if self.runtime.proc_player is not None else None)
                self.write_dls_file(track if self.runtime.proc_player is not None else None)
                self._update_sls_source_preview(track_override=track if self.runtime.proc_player is not None else None)
                self._update_monitor()

            self.save_config()
            self.log("Playlist entry updated.")
        dlg.destroy()

    def on_move_up(self, btn):
        group_keys = self._selected_group_rows_in_order()
        if group_keys:
            if self._move_selected_groups(-1):
                self._refresh_pl()
                self._select_group_rows(group_keys)
            return

        indices = self._selected_indices()
        if not indices or indices[0] <= 0:
            return
        if len(self._selected_group_keys()) != 1:
            self._msg_info("Move works on tracks from a single group.")
            return

        tracks = list(self.playlist.tracks)
        selected_tracks = [tracks[i] for i in indices]
        current_track = self.playlist.current_track()
        selected_set = set(indices)
        for idx in indices:
            if idx - 1 in selected_set:
                continue
            tracks[idx - 1], tracks[idx] = tracks[idx], tracks[idx - 1]
        self.playlist.tracks = tracks
        self._restore_current_track_index(current_track, fallback_idx=max(0, indices[0] - 1))
        self._refresh_pl()
        self._select_tracks(selected_tracks)

    def on_move_down(self, btn):
        group_keys = self._selected_group_rows_in_order()
        if group_keys:
            if self._move_selected_groups(1):
                self._refresh_pl()
                self._select_group_rows(group_keys)
            return

        indices = self._selected_indices()
        if not indices or indices[-1] >= len(self.playlist) - 1:
            return
        if len(self._selected_group_keys()) != 1:
            self._msg_info("Move works on tracks from a single group.")
            return

        tracks = list(self.playlist.tracks)
        selected_tracks = [tracks[i] for i in indices]
        selected_set = set(indices)
        current_track = self.playlist.current_track()
        for idx in reversed(indices):
            if idx + 1 in selected_set:
                continue
            tracks[idx], tracks[idx + 1] = tracks[idx + 1], tracks[idx]
        self.playlist.tracks = tracks
        self._restore_current_track_index(current_track, fallback_idx=min(len(tracks) - 1, indices[-1] + 1))
        self._refresh_pl()
        self._select_tracks(selected_tracks)

    def on_remove(self, btn):
        indices = self._selected_indices()
        if not indices:
            return
        group_count = self._selected_group_row_count()
        track_count = len(indices)

        message = "Remove selected playlist entries?"
        details = f"{track_count} track(s) will be removed."
        if group_count:
            details += f"\nSelection includes {group_count} folder/group row(s)."

        dlg = Gtk.MessageDialog(
            parent=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=message,
        )
        dlg.format_secondary_text(details)
        response = dlg.run()
        dlg.destroy()
        if response != Gtk.ResponseType.YES:
            return

        current_track = self.playlist.current_track()
        removed_current = current_track is not None and any(
            self.playlist.tracks[idx] is current_track for idx in indices
        )

        for idx in reversed(indices):
            del self.playlist.tracks[idx]

        self._restore_current_track_index(current_track, fallback_idx=min(indices[0], len(self.playlist) - 1))
        self._ensure_playable_current()

        if removed_current:
            self._stop_player()
            self._set_now_playing_label()
            self.write_dls_file()
            self._update_sls_source_preview()
            self._update_monitor()

        self._prune_playlist_group_state()
        self._refresh_pl()

    def on_clear(self, btn):
        dlg = Gtk.MessageDialog(parent=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO, text="Clear the playlist?")
        if dlg.run() == Gtk.ResponseType.YES:
            self._stop_player()
            self.playlist.clear()
            self.playlist_folder_roots.clear()
            self.playlist_group_enabled.clear()
            self.playlist_group_expanded.clear()
            self._stop_folder_monitors()
            self.store_pl.clear()
            self._set_now_playing_label()
            self.write_dls_file()
            self._update_monitor()
        dlg.destroy()

    def _selected_indices(self):
        sel = self.tv_pl.get_selection()
        model, paths = sel.get_selected_rows()
        indices = []
        for path in paths:
            try:
                it = model.get_iter(path)
            except Exception:
                continue
            indices.extend(self._collect_track_indices_from_iter(model, it))
        return sorted(set(indices))

    def _selected_group_keys(self):
        sel = self.tv_pl.get_selection()
        model, paths = sel.get_selected_rows()
        keys = set()
        for path in paths:
            try:
                tree_iter = model.get_iter(path)
            except Exception:
                continue
            if tree_iter is None:
                continue
            group_key = str(model[tree_iter][1] or "")
            if group_key:
                keys.add(group_key)
        return keys

    def _selected_group_row_count(self):
        sel = self.tv_pl.get_selection()
        model, paths = sel.get_selected_rows()
        count = 0
        for path in paths:
            try:
                tree_iter = model.get_iter(path)
            except Exception:
                continue
            if tree_iter is not None and bool(model[tree_iter][4]):
                count += 1
        return count

    def _selection_contains_group_row(self):
        sel = self.tv_pl.get_selection()
        model, paths = sel.get_selected_rows()
        for path in paths:
            try:
                tree_iter = model.get_iter(path)
            except Exception:
                continue
            if tree_iter is not None and bool(model[tree_iter][4]):
                return True
        return False

    def _selected_group_rows_in_order(self):
        sel = self.tv_pl.get_selection()
        model, paths = sel.get_selected_rows()
        keys = []
        for path in paths:
            try:
                tree_iter = model.get_iter(path)
            except Exception:
                continue
            if tree_iter is None or not bool(model[tree_iter][4]):
                continue
            group_key = str(model[tree_iter][1] or "")
            if group_key and group_key not in keys:
                keys.append(group_key)
        return keys

    def _selected_single_track_index(self):
        sel = self.tv_pl.get_selection()
        model, paths = sel.get_selected_rows()
        if len(paths) != 1:
            return None
        try:
            tree_iter = model.get_iter(paths[0])
        except Exception:
            return None
        if tree_iter is None or bool(model[tree_iter][4]):
            return None
        idx = int(model[tree_iter][0])
        return idx if 0 <= idx < len(self.playlist) else None

    def _collect_track_indices_from_iter(self, model, tree_iter):
        if tree_iter is None:
            return []
        if not bool(model[tree_iter][4]):
            idx = int(model[tree_iter][0])
            return [idx] if 0 <= idx < len(self.playlist) else []

        indices = []
        child = model.iter_children(tree_iter)
        while child:
            indices.extend(self._collect_track_indices_from_iter(model, child))
            child = model.iter_next(child)
        return indices

    def _walk_playlist_rows(self, callback):
        def walk(tree_iter):
            while tree_iter:
                callback(tree_iter)
                child = self.store_pl.iter_children(tree_iter)
                if child:
                    walk(child)
                tree_iter = self.store_pl.iter_next(tree_iter)
        root = self.store_pl.get_iter_first()
        if root:
            walk(root)

    def _prune_playlist_group_state(self):
        current_keys = {self._playlist_group_key(track) for track in self.playlist}
        self.playlist_group_enabled = {
            key: bool(value)
            for key, value in self.playlist_group_enabled.items()
            if key in current_keys
        }
        self.playlist_group_expanded = {
            key: bool(value)
            for key, value in self.playlist_group_expanded.items()
            if key in current_keys
        }

    def _restore_current_track_index(self, current_track, fallback_idx=-1):
        if not self.playlist.tracks:
            self.playlist.current_idx = -1
            return
        if current_track in self.playlist.tracks:
            self.playlist.current_idx = self.playlist.tracks.index(current_track)
            return
        self.playlist.current_idx = max(0, min(fallback_idx, len(self.playlist.tracks) - 1))

    def _select_tracks(self, tracks):
        selection = self.tv_pl.get_selection()
        selection.unselect_all()
        selected_ids = {id(track) for track in tracks}
        def maybe_select(tree_iter):
            if bool(self.store_pl[tree_iter][4]):
                return
            idx = int(self.store_pl[tree_iter][0])
            if 0 <= idx < len(self.playlist) and id(self.playlist[idx]) in selected_ids:
                selection.select_iter(tree_iter)
        self._walk_playlist_rows(maybe_select)

    def _select_group_rows(self, group_keys):
        selection = self.tv_pl.get_selection()
        selection.unselect_all()
        target_keys = set(group_keys)

        def maybe_select(tree_iter):
            if not bool(self.store_pl[tree_iter][4]):
                return
            group_key = str(self.store_pl[tree_iter][1] or "")
            if group_key in target_keys:
                selection.select_iter(tree_iter)

        self._walk_playlist_rows(maybe_select)

    def _move_selected_groups(self, direction):
        if direction not in (-1, 1):
            return False

        selected_keys = self._selected_group_rows_in_order()
        if not selected_keys:
            return False

        grouped_tracks = {}
        ordered_groups = []
        for track in self.playlist:
            group_key = self._playlist_group_key(track)
            if group_key not in grouped_tracks:
                grouped_tracks[group_key] = []
                ordered_groups.append(group_key)
            grouped_tracks[group_key].append(track)

        if not ordered_groups:
            return False

        selected_set = set(selected_keys)
        positions = [idx for idx, key in enumerate(ordered_groups) if key in selected_set]
        if not positions:
            return False
        if direction < 0 and positions[0] == 0:
            return False
        if direction > 0 and positions[-1] == len(ordered_groups) - 1:
            return False

        if direction < 0:
            for idx in positions:
                if ordered_groups[idx - 1] in selected_set:
                    continue
                ordered_groups[idx - 1], ordered_groups[idx] = ordered_groups[idx], ordered_groups[idx - 1]
        else:
            for idx in reversed(positions):
                if ordered_groups[idx + 1] in selected_set:
                    continue
                ordered_groups[idx], ordered_groups[idx + 1] = ordered_groups[idx + 1], ordered_groups[idx]

        current_track = self.playlist.current_track()
        reordered_tracks = []
        for group_key in ordered_groups:
            reordered_tracks.extend(grouped_tracks.get(group_key, []))

        if len(reordered_tracks) != len(self.playlist.tracks):
            return False

        self.playlist.tracks = reordered_tracks
        self._restore_current_track_index(current_track, fallback_idx=0)
        self.save_config()
        return True

    def _probe_track_file(self, path):
        if not os.path.isfile(path) or should_ignore_audio_file(path):
            return None
        artist, title, album, dur = probe_audio_tags(path)
        return Track(path=path, artist=artist, title=title, album=album, duration=dur)

    def _add_file(self, path):
        track = self._probe_track_file(path)
        if track is None:
            return None
        self.playlist.append(track)
        return track

    def _add_stream_url(self, url, title_hint=""):
        url = (url or "").strip()
        if not is_stream_url(url):
            return
        title_hint = (title_hint or "").strip()
        duration = "?"
        if is_pulse_monitor_source(url):
            title_hint = title_hint or pulse_monitor_title(url)
            duration = "LIVE"
        elif is_pulse_source(url):
            title_hint = title_hint or pulse_source_title(url)
            duration = "LIVE"
        track = Track(
            path=url,
            artist="",
            title=title_hint,
            album="",
            duration=duration,
        )
        self.playlist.append(track)
        return track

    def _add_app_audio_track(self, monitor_source, title_hint="", source_pid=0, source_app_name=""):
        monitor_source = (monitor_source or capture_monitor_source_name()).strip()
        if not monitor_source:
            return None
        path = f"pulse-monitor://{monitor_source}"
        title_hint = (title_hint or "").strip() or "Desktop audio capture"

        for existing in self.playlist:
            if existing.path == path:
                existing.title = title_hint
                existing.source_pid = int(source_pid or 0)
                existing.source_app_name = (source_app_name or "").strip()
                return existing

        track = Track(
            path=path,
            artist="",
            title=title_hint,
            album="",
            duration="LIVE",
            source_pid=int(source_pid or 0),
            source_app_name=(source_app_name or "").strip(),
        )
        self.playlist.append(track)
        return track

    def _add_audio_input_track(self, source_name, title_hint=""):
        source_name = (source_name or "").strip()
        if not source_name:
            return None
        path = f"pulse-source://{source_name}"
        title_hint = (title_hint or "").strip() or pulse_source_title(path)

        for existing in self.playlist:
            if existing.path == path:
                existing.title = title_hint
                existing.duration = "LIVE"
                return existing

        track = Track(
            path=path,
            artist="",
            title=title_hint,
            album="",
            duration="LIVE",
        )
        self.playlist.append(track)
        return track

    def _refresh_pl(self):
        current = self.playlist.current_idx
        groups = []
        grouped_tracks = {}
        for i, track in enumerate(self.playlist):
            group_key = self._playlist_group_key(track)
            if group_key not in grouped_tracks:
                grouped_tracks[group_key] = []
                groups.append(group_key)
            grouped_tracks[group_key].append((i, track))

        self.playlist_group_enabled = {
            key: bool(self.playlist_group_enabled.get(key, True))
            for key in groups
        }
        self.playlist_group_expanded = {
            key: bool(self.playlist_group_expanded.get(key, self._playlist_group_default_expanded(key)))
            for key in groups
        }

        self._playlist_model_refreshing = True
        try:
            self.store_pl.clear()
            group_iters = {}
            for group_key in groups:
                tracks = grouped_tracks[group_key]
                enabled = self._playlist_group_enabled_state(group_key)
                group_iter = self.store_pl.append(
                    None,
                    [-1, group_key, self._playlist_group_label(group_key, len(tracks)), False, True, enabled],
                )
                group_iters[group_key] = group_iter
                for i, track in tracks:
                    self.store_pl.append(
                        group_iter,
                        [i, group_key, build_playlist_entry(i, track), i == current, False, enabled],
                    )
        finally:
            self._playlist_model_refreshing = False

        for group_key, group_iter in group_iters.items():
            tree_path = self.store_pl.get_path(group_iter)
            if tree_path is None:
                continue
            should_expand = self.playlist_group_expanded.get(
                group_key,
                self._playlist_group_default_expanded(group_key),
            )
            if should_expand:
                self.tv_pl.expand_row(tree_path, False)
            else:
                self.tv_pl.collapse_row(tree_path)
        self._refresh_player_empty_state()
        self._refresh_folder_monitors()

    def _refresh_player_empty_state(self):
        if not hasattr(self, "player_stack"):
            return
        child_name = "playlist" if len(self.playlist) else "empty"
        self.player_stack.set_visible_child_name(child_name)

    def _highlight_current(self, idx):
        def update_row(tree_iter):
            is_group = bool(self.store_pl[tree_iter][4])
            row_idx = int(self.store_pl[tree_iter][0])
            self.store_pl.set_value(tree_iter, 3, (not is_group and row_idx == idx))
        self._walk_playlist_rows(update_row)

    def _get_tags(self, path):
        return probe_audio_tags(path)

    # ============================================================
    # LECTEUR (GStreamer natif)
    # ============================================================
    def _on_pl_dblclick(self, tv, path, col):
        it = self.store_pl.get_iter(path)
        if it is None:
            return
        if bool(self.store_pl[it][4]):
            if self.tv_pl.row_expanded(path):
                self.tv_pl.collapse_row(path)
            else:
                self.tv_pl.expand_row(path, False)
            return
        idx = int(self.store_pl[it][0])
        if idx < 0 or idx >= len(self.playlist):
            return
        if not self._is_track_enabled(self.playlist[idx]):
            self._msg_info("Enable this group before playing one of its tracks.")
            return
        self._play_track(idx)

    def on_play_pause(self, btn):
        if self.runtime.loop_card < 0:
            self.log("Attempting to load snd-aloop...")
            try_load_loopback_module()
            self.runtime.loop_card = detect_loop_card()
            if self.runtime.loop_card >= 0:
                self.lbl_src_info.set_text(
                    f"Source: Native ALSA Loopback  (hw:{self.runtime.loop_card},1)")
                self.log(f"Loopback detected: hw:{self.runtime.loop_card} — playback available.")
            else:
                self._msg_warn("ALSA Loopback card not found.\n"
                               "The snd-aloop module must be loaded as root:\n"
                               "  sudo modprobe snd-aloop\n\n"
                               "Playback cannot start without Loopback.")
                return

        if self.runtime.proc_player is None:
            if not self.playlist:
                self._msg_info("The playlist is empty. Add files first.")
                return
            idx = self._ensure_playable_current()
            if idx is None or idx < 0:
                self._msg_info("No enabled track is available. Enable at least one group first.")
                return
            self._play_track(idx)
            return

        if self.playlist.paused:
            self.runtime.proc_player.set_state(Gst.State.PLAYING)
            self.playlist.paused = False
            self.btn_play.set_label("⏸  Pause")
            self.log("Playback resumed.")
        else:
            self.runtime.proc_player.set_state(Gst.State.PAUSED)
            self.playlist.paused = True
            self.btn_play.set_label("▶  Play")
            self.log("Playback paused.")

    def on_stop_play(self, btn):
        self.playlist.manual_skip = True
        self._stop_player()
        self.runtime.reset_player_recovery()
        self.playlist.paused = False
        self._highlight_current(-1)
        self._set_now_playing_label()
        self.btn_play.set_label("▶  Play")
        self.write_dls_file()
        self._update_sls_source_preview()
        self._update_monitor()

    def on_next(self, btn):
        self.playlist.manual_skip = True
        self._advance_next()

    def on_prev(self, btn):
        prev_idx = self._previous_playable_index()
        if prev_idx is not None:
            self.playlist.manual_skip = True
            self._play_track(prev_idx)

    def on_local_monitor_toggled(self, button):
        self._restart_player_with_current_position()

    def _stop_player(self):
        if self.runtime.player_bus is not None:
            for handler_id in self.runtime.player_bus_handlers:
                try:
                    self.runtime.player_bus.disconnect(handler_id)
                except Exception:
                    pass
            try:
                self.runtime.player_bus.remove_signal_watch()
            except Exception:
                pass
            self.runtime.player_bus = None
            self.runtime.player_bus_handlers = ()
        if self.runtime.proc_player:
            self.runtime.proc_player.set_state(Gst.State.NULL)
            self.runtime.proc_player = None
        self.runtime.reset_player_vu()
        self.runtime.reset_stream_metadata()
        self._refresh_player_vu()
        self._set_player_countdown_label("—")
        self._reset_player_seek_scale()
        self.playlist.paused = False

    def _play_track(self, idx, start_position_ns=None, start_paused=False):
        if idx < 0 or idx >= len(self.playlist): return
        if not self._is_track_enabled(self.playlist[idx]):
            self._msg_info("Enable this group before playing one of its tracks.")
            return
        previous_track = self.playlist.current_track()
        self.playlist.manual_skip = True
        self._stop_player()
        track = self.playlist.set_current(idx)
        if track is None:
            return
        if previous_track is None or previous_track.path != track.path:
            self.runtime.reset_player_recovery()

        info = now_playing_label(track)
        self._set_now_playing_label(track)

        if self.chk_dls_from_file.get_active() or self.chk_force_default_dls.get_active():
            self.write_dls_file(track)
            self._update_monitor()
            if self._effective_dls_from_file():
                self.log(f"DLS updated: {info}")
        self._update_sls_source_preview(track_override=track)

        vol = self.PLAYER_UNITY_VOLUME / 100.0
        card = self.runtime.loop_card

        sr_text = self.cmb_samplerate.get_active_text() or "48000 Hz"
        sample_rate = int(sr_text[:5])
        pipeline_str = build_pipeline(
            track.path,
            vol,
            card,
            sample_rate=sample_rate,
            local_monitor=self.chk_local_monitor.get_active(),
        )
        try:
            self.runtime.proc_player = Gst.parse_launch(pipeline_str)
        except Exception as e:
            self.log(f"GStreamer pipeline error: {e}")
            return

        player = self.runtime.proc_player
        bus = player.get_bus()
        bus.add_signal_watch()
        self.runtime.player_bus = bus
        self.runtime.player_bus_handlers = (
            bus.connect("message::element", self._on_gst_level, player),
            bus.connect("message::tag", self._on_gst_tag, player),
            bus.connect("message::eos", self._on_gst_eos, player),
            bus.connect("message::error", self._on_gst_error, player),
        )

        target_state = Gst.State.PAUSED if start_paused else Gst.State.PLAYING
        self.runtime.proc_player.set_state(target_state)
        if start_position_ns is not None and start_position_ns > 0:
            self.runtime.proc_player.get_state(2 * Gst.SECOND)
            self.runtime.proc_player.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                start_position_ns,
            )
        self.playlist.paused = start_paused
        self.btn_play.set_label("▶  Play" if start_paused else "⏸  Pause")
        self._highlight_current(idx)
        self._update_player_countdown()
        self._update_player_seek_scale()
        self.playlist.manual_skip = False
        self.log(f"Playback: {info}")

    def _on_gst_level(self, bus, msg, player):
        if player is not self.runtime.proc_player:
            return
        s = msg.get_structure()
        if s and s.get_name() == "level":
            vu = gst_peak_to_vu(s.get_value("peak"))
            if vu:
                self.runtime.player_vu_left, self.runtime.player_vu_right = vu
                self._refresh_player_vu()

    def _on_gst_tag(self, bus, msg, player):
        if player is not self.runtime.proc_player:
            return

        track = self.playlist.current_track()
        if track is None or not is_stream_url(track.path):
            return

        try:
            tags = msg.parse_tag()
        except Exception:
            return

        station_name = self._gst_tag_string(tags, "organization")
        codec_name = self._gst_tag_string(tags, "audio-codec")
        bitrate_value = (
            self._gst_tag_int(tags, "nominal-bitrate")
            or self._gst_tag_int(tags, "bitrate")
        )
        bitrate_label = ""
        if bitrate_value:
            bitrate_label = f"{max(1, int(round(bitrate_value / 1000.0)))} kbps"

        if station_name and station_name != self.runtime.stream_station_name:
            self.runtime.stream_station_name = station_name
            self.log(f"Stream station: {station_name}")

        if codec_name and codec_name != self.runtime.stream_codec:
            self.runtime.stream_codec = codec_name
            self.log(f"Stream codec: {codec_name}")

        if bitrate_label and bitrate_label != self.runtime.stream_bitrate:
            self.runtime.stream_bitrate = bitrate_label
            self.log(f"Stream bitrate: {bitrate_label}")

        title_tag = self._gst_tag_string(tags, "title")
        artist_tag = self._gst_tag_string(tags, "artist")
        album_tag = self._gst_tag_string(tags, "album")
        metadata_changed = False

        if track.manual_metadata:
            return

        if title_tag:
            split_artist, split_title = split_artist_title(title_tag)
            next_artist = artist_tag or split_artist or ""
            next_title = title_tag
            if split_title and (not artist_tag or split_artist.casefold() == artist_tag.casefold()):
                next_title = split_title

            if track.artist != next_artist:
                track.artist = next_artist
                metadata_changed = True
            if track.title != next_title:
                track.title = next_title
                metadata_changed = True
        elif artist_tag and track.artist != artist_tag:
            track.artist = artist_tag
            metadata_changed = True

        if album_tag and track.album != album_tag:
            track.album = album_tag
            metadata_changed = True

        if not metadata_changed:
            return

        info = now_playing_label(track)
        self._set_now_playing_label(track)
        self._refresh_pl()
        self._highlight_current(self.playlist.current_idx)
        self.write_dls_file(track)
        self._update_sls_source_preview(track_override=track)
        self._update_monitor()
        self.log(f"Stream metadata: {info}")

    def _gst_tag_string(self, tags, key):
        try:
            value = tags.get_value_index(key, 0)
        except Exception:
            return ""
        if value is None:
            return ""
        text = str(value).strip()
        if not text or text in {"-", "—"}:
            return ""
        return text

    def _gst_tag_int(self, tags, key):
        try:
            value = tags.get_value_index(key, 0)
        except Exception:
            return None
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _on_gst_eos(self, bus, msg, player):
        if player is not self.runtime.proc_player:
            return
        GLib.idle_add(self._player_finished, player)

    def _on_gst_error(self, bus, msg, player):
        if player is not self.runtime.proc_player:
            return
        err, _ = msg.parse_error()
        self.log(f"[gst] Error: {err.message}")
        GLib.idle_add(self._player_finished, player)

    def _player_finished(self, player):
        if player is not self.runtime.proc_player:
            return False
        if self.playlist.manual_skip:
            return False
        self._stop_player()
        self._advance_next()
        return False

    def _advance_next(self):
        if not self.playlist: return
        nxt = self._next_playable_index(
            self.chk_shuffle.get_active(),
            self._repeat_mode(),
        )
        if nxt is None:
            self.log("End of playlist.")
            self.playlist.stop()
            self._reset_playback_display()
            return
        self._play_track(nxt)

    # ============================================================
    # ENCODEUR
    # ============================================================
    def on_start(self, btn):
        self._start_all()

    def on_restart_enc(self, btn):
        self._restart_all()

    def on_stop_enc(self, btn):
        self._stop_all()

    def on_save_config(self, btn):
        self.save_config()
        self.log("Configuration saved.")

    def on_show_log(self, btn):
        if self.log_window is None:
            self.log_window = Gtk.Window(title="ODR Media Player log")
            self.log_window.set_transient_for(self)
            self.log_window.set_destroy_with_parent(True)
            self.log_window.set_default_size(900, 420)
            self.log_window.connect("delete-event", self._on_log_window_delete)

            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            self.log_window.add(scroll)

            self.tv_log = Gtk.TextView(buffer=self.buf_log)
            self.tv_log.set_editable(False)
            self.tv_log.set_cursor_visible(False)
            self.tv_log.set_monospace(True)
            self.tv_log.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            scroll.add(self.tv_log)

        self.log_window.show_all()
        self.log_window.present()
        self._scroll_log_to_end()

    def on_show_output_versions(self, btn):
        dlg = Gtk.Dialog(
            title="Component versions",
            parent=self,
            modal=True,
        )
        dlg.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        dlg.set_default_size(480, -1)

        box = dlg.get_content_area()
        box.set_spacing(10)
        box.set_border_width(12)

        intro = Gtk.Label(xalign=0)
        intro.set_use_markup(True)
        intro.set_markup("<b>DAB+ output component versions</b>")
        box.pack_start(intro, False, False, 0)

        grid = Gtk.Grid(row_spacing=8, column_spacing=10)
        box.pack_start(grid, False, False, 0)

        rows = [
            ("odr-audioenc:", self._tool_version_label("odr-audioenc")),
            ("odr-padenc:", self._tool_version_label("odr-padenc")),
            ("ImageMagick:", self._imagemagick_version_label()),
        ]
        for row, (label, value) in enumerate(rows):
            key = Gtk.Label(label=label, xalign=1)
            val = Gtk.Label(label=value, xalign=0)
            val.set_selectable(True)
            val.set_ellipsize(3)
            grid.attach(key, 0, row, 1, 1)
            grid.attach(val, 1, row, 1, 1)

        dlg.show_all()
        dlg.run()
        dlg.destroy()

    def _tool_version_label(self, command):
        path = shutil.which(command)
        if not path:
            return "Not found"
        try:
            output = subprocess.check_output(
                [path, "--version"],
                text=True,
                errors="replace",
                timeout=3,
            )
        except Exception:
            return "Version unavailable"

        for line in output.splitlines():
            line = line.strip()
            if line:
                return line
        return "Version unavailable"

    def _imagemagick_version_label(self):
        for command in ("magick", "convert"):
            path = shutil.which(command)
            if not path:
                continue
            try:
                output = subprocess.check_output(
                    [path, "--version"],
                    text=True,
                    errors="replace",
                    timeout=3,
                )
            except Exception:
                continue

            for line in output.splitlines():
                line = line.strip()
                if line:
                    return line
        return "Not found"

    def _on_log_window_delete(self, window, event):
        window.hide()
        return True

    def on_add_default_logo(self, btn):
        dlg = Gtk.FileChooserDialog(
            title="Add default logo",
            parent=self, action=Gtk.FileChooserAction.OPEN)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        dlg.set_select_multiple(True)
        initial_dir = self.last_logo_dir if os.path.isdir(self.last_logo_dir) else os.path.expanduser("~")
        if os.path.isdir(initial_dir):
            dlg.set_current_folder(initial_dir)
        image_filter = Gtk.FileFilter()
        image_filter.set_name("Images")
        for pattern in ("*.jpg", "*.jpeg", "*.png", "*.bmp", "*.gif", "*.webp"):
            image_filter.add_pattern(pattern)
            image_filter.add_pattern(pattern.upper())
        dlg.add_filter(image_filter)
        all_files_filter = Gtk.FileFilter()
        all_files_filter.set_name("All files")
        all_files_filter.add_pattern("*")
        dlg.add_filter(all_files_filter)

        selected_paths = dlg.get_filenames() if dlg.run() == Gtk.ResponseType.OK else []
        dlg.destroy()
        if not selected_paths:
            return

        selected_dir = os.path.dirname(selected_paths[0])
        if os.path.isdir(selected_dir):
            self.last_logo_dir = selected_dir

        imported = 0
        errors = []
        for selected in selected_paths:
            try:
                managed_path = import_default_logo(selected)
            except Exception as exc:
                errors.append(f"{os.path.basename(selected)}: {exc}")
                continue

            self.default_logo_paths.append(managed_path)
            imported += 1

        self.default_logo_paths = normalize_default_logo_paths(self.default_logo_paths)
        self._refresh_default_logo_library()
        self._update_sls_source_preview()
        self.save_config()
        if imported == 1:
            self.log("1 default logo imported.")
        elif imported > 1:
            self.log(f"{imported} default logos imported.")

        if errors:
            self._msg_err("Some logos could not be imported.\n\n" + "\n".join(errors[:10]))

    def on_slide_source_changed(self, widget):
        self._update_sls_source_preview()

    def on_output_slide_clicked(self, widget, event):
        if getattr(event, "button", 0) != 1:
            return False
        self._open_preview_image(self.current_output_slide_path, live_output=True)
        return True

    def on_remove_default_logo(self, btn, logo_path):
        self.default_logo_paths = [path for path in self.default_logo_paths if path != logo_path]
        remove_default_logo(logo_path)
        self._refresh_default_logo_library()
        self._update_sls_source_preview()
        self.save_config()
        self.log("Default logo removed.")

    def on_send_dls(self, btn):
        self.write_dls_file()
        self._update_sls_source_preview()
        self.log(f"DLS updated: {self.txt_dls.get_text()}")

    def on_dls_settings_changed(self, widget):
        self._refresh_dls_controls()
        self.write_dls_file()
        self._update_sls_source_preview()
        self._update_monitor()

    def on_encoder_settings_changed(self, widget):
        self._refresh_restart_button()
        self._update_sls_pad_estimate()

    def on_codec_change(self, combo):
        if self.cmb_channels.get_active() == 0 and self.cmb_codec.get_active() == 2:
            self._msg_warn("Invalid combination according to ETSI EN 300 401:\n"
                           "HE-AAC v2 (SBR + PS) requires a stereo signal.\n"
                           "Please choose Stereo or another codec profile.")

    def _start_all(self):
        if self.runtime.loop_card < 0:
            self._msg_err("ALSA Loopback card not found.\n"
                          "The encoder cannot start without Loopback.\n"
                          "Load the module: sudo modprobe snd-aloop")
            self.btn_start.set_sensitive(True)
            self.btn_stop_enc.set_sensitive(False)
            self.btn_restart_enc.set_sensitive(False)
            return
        if not shutil.which("odr-audioenc"):
            self._msg_err("odr-audioenc was not found in PATH.\n"
                          "Check that ODR mmbTools are installed.")
            self.btn_start.set_sensitive(True)
            self.btn_stop_enc.set_sensitive(False)
            self.btn_restart_enc.set_sensitive(False)
            return

        self.runtime.audio_crash = self.runtime.pad_crash = False
        self.runtime.stopping_audio = self.runtime.stopping_pad = False
        self.runtime.reset_silence_state()
        self.runtime.last_audioenc_data_at = time.monotonic()
        options = self._encoder_options()
        if options.sls_enabled:
            try:
                self._prepare_sls_runtime_asset(allow_placeholder=True)
            except Exception as e:
                self._msg_err(f"Unable to prepare the SLS image.\n\n{e}")
                self.btn_start.set_sensitive(True)
                self.btn_stop_enc.set_sensitive(False)
                self.btn_restart_enc.set_sensitive(False)
                return

        # padenc en premier (crée le socket)
        if use_pad(options) and shutil.which("odr-padenc"):
            self.write_dls_file()
            pad_cmd = build_pad_cmd(options)
            self.log("Starting odr-padenc:")
            self.log(f"  {pad_cmd}")
            self.runtime.proc_padenc = subprocess.Popen(
                f"exec {pad_cmd}", shell=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            set_nonblocking(self.runtime.proc_padenc.stdout)
            GLib.io_add_watch(self.runtime.proc_padenc.stdout, GLib.IO_IN | GLib.IO_HUP,
                              self._on_padenc_data)
            GLib.child_watch_add(GLib.PRIORITY_DEFAULT, self.runtime.proc_padenc.pid,
                                 self._on_padenc_exit)
            time.sleep(0.5)
        elif use_pad(options):
            self.log("WARNING: odr-padenc not found — PAD disabled.")

        # audioenc ensuite
        audio_cmd = build_audio_cmd(options)
        self.log("Starting odr-audioenc:")
        self.log(f"  {audio_cmd}")
        self.runtime.proc_audioenc = subprocess.Popen(
            f"exec {audio_cmd}", shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        set_nonblocking(self.runtime.proc_audioenc.stdout)
        GLib.io_add_watch(self.runtime.proc_audioenc.stdout, GLib.IO_IN | GLib.IO_HUP,
                          self._on_audioenc_data)
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, self.runtime.proc_audioenc.pid,
                             self._on_audioenc_exit)

        self.runtime.applied_encoder_signature = self._current_encoder_signature()
        self.btn_start.set_sensitive(False)
        self.btn_stop_enc.set_sensitive(True)
        self._refresh_restart_button()
        self.notebook.set_current_page(3)
        self._update_status()

    def _restart_all(self):
        if self.runtime.restart_pending:
            return
        if not self._encoder_running():
            return
        self.runtime.restart_pending = True
        self.log("Encoder restart requested.")
        self._stop_all(restart=True)

    def _stop_all(self, restart=False):
        if not restart:
            self.runtime.restart_pending = False

        audio_running = is_running(self.runtime.proc_audioenc)
        pad_running = is_running(self.runtime.proc_padenc)
        if audio_running:
            self.runtime.stopping_audio = True
            self.runtime.proc_audioenc.terminate()
            self.log(f"odr-audioenc stopped (SIGTERM pid={self.runtime.proc_audioenc.pid})")
        if pad_running:
            self.runtime.stopping_pad = True
            self.runtime.proc_padenc.terminate()
            self.log(f"odr-padenc stopped (SIGTERM pid={self.runtime.proc_padenc.pid})")

        if restart and not audio_running and not pad_running:
            self.runtime.restart_pending = False
            self._restart_after_stop()
            return

        self._set_label_color(self.lbl_audio_st, "red")
        self.lbl_audio_st.set_text("● Stopped")
        self._set_label_color(self.lbl_pad_st, "red")
        self.lbl_pad_st.set_text("● Stopped")
        if restart:
            self.btn_start.set_sensitive(False)
            self.btn_stop_enc.set_sensitive(False)
            self.btn_restart_enc.set_sensitive(False)
        else:
            self.btn_start.set_sensitive(True)
            self.btn_stop_enc.set_sensitive(False)
            self.runtime.applied_encoder_signature = None
            self._refresh_restart_button()

    def _on_audioenc_data(self, source, condition):
        if condition & GLib.IO_HUP:
            return False
        try:
            data = source.read(4096)
            if data:
                self.runtime.last_audioenc_data_at = time.monotonic()
                text = data.decode(errors="replace")
                vu, log_lines = parse_audioenc_chunk(text)
                if vu:
                    self.runtime.monitor_vu_left, self.runtime.monitor_vu_right = vu
                    self._refresh_monitor_vu()
                for line in log_lines:
                    self.log(f"[audioenc] {line}")
        except (BlockingIOError, OSError):
            pass
        return True

    def _on_padenc_data(self, source, condition):
        if condition & GLib.IO_HUP:
            return False
        try:
            data = source.read(4096)
            if data:
                for line in data.decode(errors="replace").splitlines():
                    line = line.strip()
                    if line:
                        self.log(f"[padenc] {line}")
        except (BlockingIOError, OSError):
            pass
        return True

    def _on_audioenc_exit(self, pid, status):
        if self.runtime.stopping_audio:
            self.runtime.audio_crash = False
            self.runtime.stopping_audio = False
        else:
            self.runtime.audio_crash = (status != 0)
        self.runtime.reset_monitor_vu()
        self.runtime.reset_silence_state()
        self._refresh_monitor_vu()
        code = decode_exit_status(status)
        self.log(f"odr-audioenc exited (code={code})")
        self._update_status()
        if not is_running(self.runtime.proc_padenc):
            cleanup_pad_artifacts()
            if self.runtime.restart_pending:
                self.runtime.restart_pending = False
                GLib.idle_add(self._restart_after_stop)
            else:
                self.btn_start.set_sensitive(True)
                self.btn_stop_enc.set_sensitive(False)
                self.runtime.applied_encoder_signature = None
                self._refresh_restart_button()

    def _on_padenc_exit(self, pid, status):
        if self.runtime.stopping_pad:
            self.runtime.pad_crash = False
            self.runtime.stopping_pad = False
        else:
            self.runtime.pad_crash = (status != 0)
        code = decode_exit_status(status)
        self.log(f"odr-padenc exited (code={code})")
        self._update_status()
        if not is_running(self.runtime.proc_audioenc):
            cleanup_pad_artifacts()
            if self.runtime.restart_pending:
                self.runtime.restart_pending = False
                GLib.idle_add(self._restart_after_stop)
            else:
                self.runtime.applied_encoder_signature = None
                self._refresh_restart_button()

    def _restart_after_stop(self):
        self.log("Restarting encoder...")
        self._start_all()
        return False

    def _encoder_running(self):
        return is_running(self.runtime.proc_audioenc) or is_running(self.runtime.proc_padenc)

    def _current_encoder_signature(self):
        return (
            self.cmb_bitrate.get_active_text() or "128",
            self.cmb_channels.get_active(),
            self.cmb_samplerate.get_active(),
            self.cmb_codec.get_active(),
            self.cmb_pad_len.get_active_text() or "58",
            int(self.spn_gain.get_value()),
            self._get_output_uri(),
        )

    def _refresh_restart_button(self):
        if not hasattr(self, "btn_restart_enc"):
            return
        enabled = (
            self._encoder_running()
            and not self.runtime.restart_pending
            and self.runtime.applied_encoder_signature is not None
            and self._current_encoder_signature() != self.runtime.applied_encoder_signature
        )
        self.btn_restart_enc.set_sensitive(enabled)

    def _encoder_options(self):
        return EncoderOptions(
            loop_card=self.runtime.loop_card,
            codec_index=self.cmb_codec.get_active(),
            channels_index=self.cmb_channels.get_active(),
            bitrate=self.cmb_bitrate.get_active_text() or "128",
            samplerate_text=self.cmb_samplerate.get_active_text() or "48000 Hz",
            gain=int(self.spn_gain.get_value()),
            silence=int(self.spn_silence.get_value()),
            zmq_out=self._get_output_uri(),
            pad_len=self.cmb_pad_len.get_active_text() or "58",
            default_dls_text=self.txt_dls.get_text(),
            force_default_dls=self.chk_force_default_dls.get_active(),
            dls_from_file=self.chk_dls_from_file.get_active(),
            dl_plus=self.chk_dl_plus.get_active(),
            sls_enabled=self.chk_sls.get_active(),
            sls_title_card=self.chk_sls_title_card.get_active(),
            sls_default_logo=self.chk_sls_default_logo.get_active(),
            slide_dir="\n".join(self.default_logo_paths),
            slide_wait=int(self.spn_slide_wait.get_value()),
        )

    def _default_dls_forced(self):
        return self.chk_force_default_dls.get_active()

    def _effective_dls_from_file(self):
        return self.chk_dls_from_file.get_active() and not self._default_dls_forced()

    def _effective_dl_plus(self):
        return self.chk_dl_plus.get_active() and self._effective_dls_from_file()

    def _refresh_dls_controls(self):
        force_default = self._default_dls_forced()
        self.chk_dls_from_file.set_sensitive(not force_default)
        self.chk_dl_plus.set_sensitive(not force_default and self.chk_dls_from_file.get_active())

    # ============================================================
    # FICHIER DLS
    # ============================================================
    def write_dls_file(self, track_override=None):
        artist = title = ""
        track = track_override if track_override is not None else self.playlist.current_track()
        if (
            self._effective_dls_from_file()
            and (track_override is not None or self.runtime.proc_player is not None)
            and track is not None
        ):
            artist = track.artist
            title = track.title
        try:
            with open(DLS_FILE, "w", encoding="utf-8") as f:
                f.write(
                    build_dls_content(
                        self.txt_dls.get_text(),
                        self._effective_dls_from_file(),
                        self._effective_dl_plus(),
                        artist,
                        title,
                    )
                )
        except Exception as e:
            self.log(f"DLS write error: {e}")

    # ============================================================
    # TIMER STATUT + MONITEUR
    # ============================================================
    def _on_status_timer(self):
        self._refresh_app_audio_title()
        self._update_player_countdown()
        self._update_player_seek_scale()
        self._update_player_retry_label()
        self._update_silence_warning_state()
        self._update_status()
        self._update_monitor()
        return True

    def _refresh_app_audio_title(self):
        track = self.playlist.current_track()
        if self.runtime.proc_player is None or track is None:
            return
        if not is_pulse_monitor_source(track.path):
            return
        if track.manual_metadata:
            return
        app_info = current_captured_app_info()
        if app_info:
            try:
                track.source_pid = int(app_info.get("process_id") or 0)
            except (TypeError, ValueError):
                track.source_pid = 0
            track.source_app_name = str(app_info.get("app_name") or track.source_app_name or "").strip()
        if int(getattr(track, "source_pid", 0) or 0) <= 0:
            return
        now = time.monotonic()
        if (now - self._last_app_audio_title_refresh) < 2.0:
            return
        self._last_app_audio_title_refresh = now

        title = self._window_title_for_pid(track.source_pid)
        title = self._clean_app_audio_title(title)
        if not title and app_info:
            app_name = str(app_info.get("app_name") or "").strip()
            media_name = str(app_info.get("media_name") or "").strip()
            if media_name and media_name not in {"Playback", "Playback Stream", "—"}:
                title = f"{app_name} — {media_name}".strip(" —")
            else:
                title = app_name
        if not title:
            return

        next_artist = ""
        next_title = title
        split_artist, split_title = split_app_audio_title(title)
        if split_artist and split_title:
            next_artist = split_artist
            next_title = split_title

        if next_artist == track.artist and next_title == track.title:
            return

        track.artist = next_artist
        track.title = next_title
        info = now_playing_label(track)
        self._set_now_playing_label(track)
        self._refresh_pl()
        self._highlight_current(self.playlist.current_idx)
        self.write_dls_file(track)
        self._update_sls_source_preview(track_override=track)
        self._update_monitor()

    def _window_title_for_pid(self, pid):
        # The app-audio title refresh runs in the GTK timer loop. Keep the
        # lookup strictly non-blocking and fall back to app/media names instead
        # of traversing the full accessibility tree.
        return self._window_title_from_wmctrl(pid)

    def _window_title_from_wmctrl(self, pid):
        candidate_pids = self._window_candidate_pids(pid)
        if not candidate_pids:
            return ""
        try:
            output = subprocess.check_output(
                ["wmctrl", "-lp"],
                text=True,
                errors="replace",
                timeout=1,
            )
        except Exception:
            return ""

        for line in output.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            try:
                window_pid = int(parts[2])
            except (TypeError, ValueError):
                continue
            if window_pid in candidate_pids:
                title = parts[4].strip()
                if title:
                    return title
        return ""

    def _window_title_from_atspi(self, pid):
        try:
            import pyatspi
        except Exception:
            return ""

        try:
            desktop = pyatspi.Registry.getDesktop(0)
        except Exception:
            return ""

        candidate_pids = self._window_candidate_pids(pid)
        stack = [desktop]
        while stack:
            node = stack.pop()
            try:
                if node.getRoleName() == "frame" and node.get_process_id() in candidate_pids:
                    name = (node.name or "").strip()
                    if name:
                        return name
                for index in range(node.childCount - 1, -1, -1):
                    stack.append(node.getChildAtIndex(index))
            except Exception:
                pass
        return ""

    def _window_candidate_pids(self, pid):
        candidates = []
        seen = set()
        current = int(pid or 0)
        for _ in range(8):
            if current <= 0 or current in seen:
                break
            seen.add(current)
            candidates.append(current)
            try:
                with open(f"/proc/{current}/stat", encoding="utf-8") as handle:
                    stat = handle.read().strip()
                end = stat.rfind(")")
                if end < 0:
                    break
                rest = stat[end + 2 :].split()
                if len(rest) < 2:
                    break
                current = int(rest[1])
            except Exception:
                break
        return tuple(candidates)

    def _clean_app_audio_title(self, title):
        text = (title or "").strip()
        if not text:
            return ""
        text = re.sub(
            r"\s+[—-]\s+(Mozilla Firefox|Firefox|Google Chrome|Chromium|Brave|Microsoft Edge)$",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        text = re.sub(r"\s+", " ", text)
        return text

    def _update_player_countdown(self):
        if not hasattr(self, "lbl_now_countdown"):
            return

        track = self.playlist.current_track()
        if self.runtime.proc_player is None or track is None:
            self._set_player_countdown_label("—")
            return

        remaining_seconds = self._query_remaining_seconds()
        if remaining_seconds is None:
            remaining_seconds = self._parse_track_duration_seconds(track.duration)

        if remaining_seconds is None:
            self._set_player_countdown_label("—")
            return

        self._set_player_countdown_label(self._format_countdown(remaining_seconds))

    def _query_remaining_seconds(self):
        try:
            ok_duration, duration_ns = self.runtime.proc_player.query_duration(Gst.Format.TIME)
            ok_position, position_ns = self.runtime.proc_player.query_position(Gst.Format.TIME)
        except Exception:
            return None

        if not ok_duration or duration_ns <= 0:
            return None

        if not ok_position or position_ns < 0:
            position_ns = 0

        remaining_ns = max(0, duration_ns - position_ns)
        return int(remaining_ns / Gst.SECOND)

    def _query_player_position_ns(self):
        if self.runtime.proc_player is None:
            return None
        try:
            ok_position, position_ns = self.runtime.proc_player.query_position(Gst.Format.TIME)
        except Exception:
            return None
        if not ok_position or position_ns < 0:
            return None
        return position_ns

    def _query_player_duration_ns(self):
        if self.runtime.proc_player is None:
            return None
        try:
            ok_duration, duration_ns = self.runtime.proc_player.query_duration(Gst.Format.TIME)
        except Exception:
            return None
        if not ok_duration or duration_ns <= 0:
            return None
        return duration_ns

    def _player_seek_supported(self, track=None):
        track = track if track is not None else self.playlist.current_track()
        if track is None or self.runtime.proc_player is None:
            return False
        return not (
            is_stream_url(track.path)
            or is_pulse_monitor_source(track.path)
            or is_pulse_source(track.path)
        )

    def _reset_player_seek_scale(self):
        if not hasattr(self, "scale_player_seek"):
            return
        self._player_seek_updating = True
        try:
            self.scale_player_seek.set_sensitive(False)
            self.scale_player_seek.set_range(0, 100)
            self.scale_player_seek.set_value(0)
        finally:
            self._player_seek_updating = False

    def _update_player_seek_scale(self):
        if not hasattr(self, "scale_player_seek"):
            return

        track = self.playlist.current_track()
        if not self._player_seek_supported(track):
            self._reset_player_seek_scale()
            return

        duration_ns = self._query_player_duration_ns()
        if duration_ns is None:
            duration_seconds = self._parse_track_duration_seconds(track.duration)
            if duration_seconds is None:
                self._reset_player_seek_scale()
                return
            duration_seconds = max(1, duration_seconds)
        else:
            duration_seconds = max(1, int(duration_ns / Gst.SECOND))

        position_ns = self._query_player_position_ns()
        position_seconds = 0 if position_ns is None else max(0, int(position_ns / Gst.SECOND))
        position_seconds = min(duration_seconds, position_seconds)

        page_increment = max(5, duration_seconds // 20)
        self._player_seek_updating = True
        try:
            self.scale_player_seek.set_sensitive(True)
            self.scale_player_seek.set_range(0, duration_seconds)
            self.scale_player_seek.set_increments(1, page_increment)
            if not self._player_seek_dragging:
                self.scale_player_seek.set_value(position_seconds)
        finally:
            self._player_seek_updating = False

    def _apply_player_seek_from_scale(self):
        track = self.playlist.current_track()
        if not self._player_seek_supported(track):
            return
        target_seconds = max(0, int(self.scale_player_seek.get_value()))
        try:
            self.runtime.proc_player.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                target_seconds * Gst.SECOND,
            )
        except Exception:
            return
        self._update_player_countdown()
        self._update_player_seek_scale()

    def on_player_seek_press(self, widget, event):
        if self._player_seek_supported():
            self._player_seek_dragging = True
        return False

    def on_player_seek_release(self, widget, event):
        if self._player_seek_dragging:
            self._player_seek_dragging = False
            self._apply_player_seek_from_scale()
        return False

    def on_player_seek_value_changed(self, widget):
        if self._player_seek_updating or self._player_seek_dragging:
            return
        self._apply_player_seek_from_scale()

    def _restart_player_with_current_position(self):
        if self.runtime.proc_player is None:
            return
        current_idx = self.playlist.current_idx
        if current_idx < 0:
            return
        position_ns = self._query_player_position_ns()
        was_paused = self.playlist.paused
        self._play_track(current_idx, start_position_ns=position_ns, start_paused=was_paused)

    def _parse_track_duration_seconds(self, duration_text):
        value = (duration_text or "").strip()
        if not value or value == "?":
            return None

        parts = value.split(":")
        try:
            if len(parts) == 2:
                minutes, seconds = parts
                return int(minutes) * 60 + int(seconds)
            if len(parts) == 3:
                hours, minutes, seconds = parts
                return int(hours) * 3600 + int(minutes) * 60 + int(seconds)
        except ValueError:
            return None

        return None

    def _format_countdown(self, remaining_seconds):
        total = max(0, int(remaining_seconds))
        hours, rem = divmod(total, 3600)
        minutes, seconds = divmod(rem, 60)
        if hours > 0:
            return f"-{hours}:{minutes:02d}:{seconds:02d}"
        return f"-{minutes}:{seconds:02d}"

    def _set_player_countdown_label(self, text):
        self.lbl_now_countdown.set_text(text)

    def _update_player_retry_label(self):
        track = self.playlist.current_track()
        retry_count = int(self.runtime.player_recovery_count or 0)
        if (
            self.runtime.proc_player is None
            or track is None
            or retry_count <= 0
            or not (
                is_stream_url(track.path)
                or is_pulse_source(track.path)
                or is_pulse_monitor_source(track.path)
            )
        ):
            self.lbl_now_retry.hide()
            self.lbl_now_retry.set_text("")
            return

        self.lbl_now_retry.set_markup(
            f"<span foreground='#c01c28'><i>retry connection: {retry_count}</i></span>"
        )
        self.lbl_now_retry.show()

    def _update_status(self):
        audio_ok = is_running(self.runtime.proc_audioenc)
        if audio_ok:
            if self.runtime.silence_warning_active:
                threshold = int(self.spn_silence.get_value())
                warning_text = (
                    "no data detected"
                    if self.runtime.silence_warning_kind == "no-data"
                    else "no audio detected"
                )
                self.lbl_audio_st.set_text(
                    f"● Running, {warning_text}  (>{threshold}s, PID {self.runtime.proc_audioenc.pid})"
                )
                self._set_label_color(self.lbl_audio_st, "orange")
            else:
                self.lbl_audio_st.set_text(f"● Running  (PID {self.runtime.proc_audioenc.pid})")
                self._set_label_color(self.lbl_audio_st, "green")
        elif self.runtime.audio_crash:
            self.lbl_audio_st.set_text("● Error (non-zero exit code)")
            self._set_label_color(self.lbl_audio_st, "orange")
        else:
            self.lbl_audio_st.set_text("● Stopped")
            self._set_label_color(self.lbl_audio_st, "red")

        pad_ok = is_running(self.runtime.proc_padenc)
        if pad_ok:
            if self.runtime.silence_warning_active:
                self.lbl_pad_st.set_text(
                    f"● Running, source inactive  (PID {self.runtime.proc_padenc.pid})"
                )
                self._set_label_color(self.lbl_pad_st, "orange")
            else:
                self.lbl_pad_st.set_text(f"● Running  (PID {self.runtime.proc_padenc.pid})")
                self._set_label_color(self.lbl_pad_st, "green")
        elif self.runtime.pad_crash:
            self.lbl_pad_st.set_text("● Error (non-zero exit code)")
            self._set_label_color(self.lbl_pad_st, "orange")
        else:
            self.lbl_pad_st.set_text("● Stopped")
            self._set_label_color(self.lbl_pad_st, "red")

    def _update_silence_warning_state(self, force_recompute=False):
        threshold = int(self.spn_silence.get_value())
        if threshold <= 0:
            if self.runtime.silence_warning_active:
                self.log("Audio/data warning disabled.")
            self.runtime.reset_silence_state()
            self.runtime.reset_player_recovery()
            return
        if not is_running(self.runtime.proc_audioenc):
            self.runtime.reset_silence_state()
            return

        now = time.monotonic()
        last_data = float(self.runtime.last_audioenc_data_at or 0.0)
        if last_data <= 0.0:
            self.runtime.last_audioenc_data_at = now
            last_data = now

        no_data = (now - last_data) >= threshold
        if no_data:
            if (
                not self.runtime.silence_warning_active
                or self.runtime.silence_warning_kind != "no-data"
            ):
                self.runtime.silence_warning_active = True
                self.runtime.silence_warning_kind = "no-data"
                self.log(
                    f"WARNING: no data detected for more than {threshold} s. "
                    "Encoder kept running."
                )
            self._attempt_player_recovery("no data detected")
            return

        silent = (
            int(self.runtime.monitor_vu_left) <= 0
            and int(self.runtime.monitor_vu_right) <= 0
        )

        if not silent:
            if self.runtime.silence_warning_active:
                self.log("Audio/data returned after warning.")
            self.runtime.reset_silence_state()
            self.runtime.reset_player_recovery()
            self.runtime.last_audioenc_data_at = now
            return

        if self.runtime.silence_started_at <= 0.0:
            self.runtime.silence_started_at = now

        elapsed = int(now - self.runtime.silence_started_at)
        if elapsed >= threshold and (
            not self.runtime.silence_warning_active
            or self.runtime.silence_warning_kind != "no-audio"
        ):
            self.runtime.silence_warning_active = True
            self.runtime.silence_warning_kind = "no-audio"
            self.log(
                f"WARNING: no audio detected for more than {threshold} s. "
                "Encoder kept running."
            )
        if elapsed >= threshold:
            self._attempt_player_recovery("no audio detected")

    def _attempt_player_recovery(self, reason):
        track = self.playlist.current_track()
        if self.runtime.proc_player is None or track is None or self.playlist.paused:
            return

        current_path = track.path or ""
        is_live_source = (
            is_stream_url(current_path)
            or is_pulse_source(current_path)
            or is_pulse_monitor_source(current_path)
        )
        now = time.monotonic()

        if self.runtime.player_recovery_path != current_path:
            self.runtime.reset_player_recovery()
            self.runtime.player_recovery_path = current_path

        if is_live_source:
            retry_interval = 30.0
            if (
                self.runtime.player_recovery_last_attempt_at > 0.0
                and (now - self.runtime.player_recovery_last_attempt_at) < retry_interval
            ):
                return
        elif self.runtime.player_recovery_attempted:
            return

        self.runtime.player_recovery_attempted = True
        self.runtime.player_recovery_path = current_path
        self.runtime.player_recovery_last_attempt_at = now

        if is_live_source:
            self.runtime.player_recovery_count += 1
            self.log(f"Player recovery: restarting current source after {reason}.")
            self._play_track(self.playlist.current_idx, start_paused=False)
            return

        position_ns = self._query_player_position_ns()
        self.log(f"Player recovery: restarting current track after {reason}.")
        self._play_track(
            self.playlist.current_idx,
            start_position_ns=position_ns,
            start_paused=False,
        )

    def _update_monitor(self):
        snapshot = read_monitor_snapshot(
            DLS_FILE,
            self.playlist,
            self.playlist.current_idx,
            self.chk_sls.get_active(),
            SLIDE_DUMP,
            self.runtime.slide_mtime,
            self.runtime.slide_paths,
            self.runtime.slide_wait_seconds,
            self.runtime.slide_rotation_started_at,
            self.runtime.slide_preview_override_path,
            self.runtime.slide_preview_override_until,
        )
        options = self._encoder_options()
        out_addr, out_port = output_endpoint_parts(options.zmq_out)
        playback_active = self.runtime.proc_player is not None
        dl_plus_active = (
            playback_active
            and
            self._effective_dl_plus()
            and snapshot.artist != "—"
            and snapshot.title != "—"
        )

        self.lbl_mon_dls.set_text(snapshot.dls)
        self.lbl_mon_dl_plus.set_text("Active" if dl_plus_active else "Inactive")
        self.lbl_mon_title.set_text(snapshot.title if dl_plus_active else "-")
        self.lbl_mon_artist.set_text(snapshot.artist if dl_plus_active else "-")
        self.lbl_mon_codec.set_text(codec_label(options.codec_index))
        self.lbl_mon_bitrate.set_text(f"{options.bitrate} kbps")
        self.lbl_mon_samplerate.set_text(options.samplerate_text or "—")
        self.lbl_mon_channels.set_text(channels_label(options.channels_index))
        self.lbl_mon_gain.set_text(f"{options.gain} dB")
        self.lbl_mon_output_mode.set_text(
            "EDI (udp)" if self.cmb_output_proto.get_active() == 1 else "ZMQ (tcp)"
        )
        self.lbl_mon_out_addr.set_text(out_addr or "—")
        self.lbl_mon_out_port.set_text(out_port or "—")

        if not self.chk_sls.get_active():
            self._set_output_slide_image(None, "Slideshows desactivate")
        elif snapshot.slide_path:
            try:
                self._set_output_slide_image(snapshot.slide_path)
                self.runtime.slide_mtime = snapshot.slide_mtime
            except Exception:
                self._set_output_slide_image(None)
        elif os.path.isfile(SLIDE_INPUT_FILE):
            self._set_output_slide_image(SLIDE_INPUT_FILE)
        else:
            self._set_output_slide_image(None)

    # ============================================================
    # LOG
    # ============================================================
    def log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        end = self.buf_log.get_end_iter()
        self.buf_log.insert(end, f"{ts}  {msg}\n")
        self._trim_log_buffer()
        self._scroll_log_to_end()

    def _trim_log_buffer(self):
        line_count = self.buf_log.get_line_count()
        if line_count <= self.LOG_MAX_LINES:
            return

        trim_lines = max(1, line_count - self.LOG_TRIM_TO_LINES)
        start = self.buf_log.get_start_iter()
        end = self.buf_log.get_iter_at_line(trim_lines)
        self.buf_log.delete(start, end)

    def _scroll_log_to_end(self):
        if self.tv_log is None:
            return
        end = self.buf_log.get_end_iter()
        self.tv_log.scroll_to_iter(end, 0, False, 0, 0)

    # ============================================================
    # FERMETURE
    # ============================================================
    def on_close(self, win, event):
        player_alive = self.runtime.proc_player is not None
        audio_alive = is_running(self.runtime.proc_audioenc)
        pad_alive = is_running(self.runtime.proc_padenc)
        if player_alive or audio_alive or pad_alive:
            dlg = Gtk.MessageDialog(parent=self, modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Quit ODR Media Player?")
            dlg.format_secondary_text(
                "Closing the application will stop playback and all encoder processes."
            )
            response = dlg.run()
            dlg.destroy()
        if response != Gtk.ResponseType.YES:
            return True

        self.save_config()
        self._stop_folder_monitors()
        self.playlist.manual_skip = True
        self._stop_player()
        if audio_alive or pad_alive:
            self._stop_all()
        Gtk.main_quit()
        return True

    # ============================================================
    # CONFIG
    # ============================================================
    def save_config(self):
        config = self._collect_app_config()
        settings, playlist, sls_logos = config.to_storage()
        settings["__sls_logos__"] = sls_logos
        write_flat_config(CONF_FILE, settings, playlist)

    def load_config(self):
        if not os.path.isfile(CONF_FILE): return
        settings, playlist_entries, sls_logos = read_config_file(CONF_FILE)
        if not settings and not playlist_entries and not sls_logos:
            return

        config = AppConfig.from_storage(settings, playlist_entries, sls_logos)
        self._apply_app_config(config)

    def _collect_app_config(self):
        return AppConfig(
            bitrate=self.cmb_bitrate.get_active_text() or "128",
            channels=self.cmb_channels.get_active(),
            zmq_out=self._get_output_uri(),
            silence=int(self.spn_silence.get_value()),
            sample_rate=self.cmb_samplerate.get_active(),
            codec=self.cmb_codec.get_active(),
            gain=int(self.spn_gain.get_value()),
            volume=self.PLAYER_UNITY_VOLUME,
            dls_text=self.txt_dls.get_text(),
            force_default_dls=self.chk_force_default_dls.get_active(),
            dls_from_file=self.chk_dls_from_file.get_active(),
            dl_plus_on=self.chk_dl_plus.get_active(),
            sls_on=self.chk_sls.get_active(),
            sls_title_card=self.chk_sls_title_card.get_active(),
            sls_cover_local=self.chk_sls_cover_local.get_active(),
            sls_cover_online=self.chk_sls_cover_online.get_active(),
            sls_default_logo=self.chk_sls_default_logo.get_active(),
            slide_dir="",
            sls_logos=list(self.default_logo_paths),
            slide_wait=int(self.spn_slide_wait.get_value()),
            pad_len=self.cmb_pad_len.get_active_text() or "58",
            playlist_autostart=self.chk_playlist_autostart.get_active(),
            encoder_autostart=self.chk_encoder_autostart.get_active(),
            shuffle=self.chk_shuffle.get_active(),
            repeat_mode=self._repeat_mode(),
            local_monitor=self.chk_local_monitor.get_active(),
            watch_loaded_folders=True,
            last_logo_dir=self.last_logo_dir,
            playlist=self.playlist.paths(),
            playlist_folder_roots=list(self.playlist_folder_roots),
            playlist_group_states=dict(self.playlist_group_enabled),
            playlist_overrides=self._collect_playlist_overrides(),
        )

    def _apply_app_config(self, config):
        self._applying_config = True
        try:
            if config.bitrate in BITRATES:
                self.cmb_bitrate.set_active(BITRATES.index(config.bitrate))
            self.cmb_channels.set_active(config.channels)
            _uri = config.zmq_out or "tcp://localhost:9000"
            host, port = output_endpoint_parts(_uri)
            if _uri.startswith("udp://"):
                self.cmb_output_proto.set_active(1)
            else:
                self.cmb_output_proto.set_active(0)
            self.txt_output_host.set_text(host if host and host != "—" else "localhost")
            try:
                self.spn_output_port.set_value(int(port))
            except (TypeError, ValueError):
                self.spn_output_port.set_value(9000)
            self.spn_silence.set_value(config.silence)
            self.cmb_samplerate.set_active(config.sample_rate)
            self.cmb_codec.set_active(config.codec)
            self.spn_gain.set_value(config.gain)
            self.txt_dls.set_text(config.dls_text)
            self.chk_force_default_dls.set_active(config.force_default_dls)
            self.chk_dls_from_file.set_active(config.dls_from_file)
            self.chk_dl_plus.set_active(config.dl_plus_on)
            self.chk_sls.set_active(config.sls_on)
            self.chk_sls_title_card.set_active(config.sls_title_card)
            self.chk_sls_cover_local.set_active(config.sls_cover_local)
            self.chk_sls_cover_online.set_active(config.sls_cover_online)
            self.chk_sls_default_logo.set_active(config.sls_default_logo)
            self.default_logo_paths = normalize_default_logo_paths(config.sls_logos)
            if not self.default_logo_paths and config.slide_dir and os.path.isfile(config.slide_dir):
                try:
                    self.default_logo_paths = [import_default_logo(config.slide_dir)]
                    self.log("Legacy SLS logo imported into internal storage.")
                    self.save_config()
                except Exception:
                    self.default_logo_paths = []
            self.spn_slide_wait.set_value(config.slide_wait)
            self._refresh_default_logo_library()
            self._update_sls_source_preview()
            if config.pad_len in PAD_LENGTHS:
                self.cmb_pad_len.set_active(PAD_LENGTHS.index(config.pad_len))
            self.chk_playlist_autostart.set_active(config.playlist_autostart)
            self.chk_encoder_autostart.set_active(config.encoder_autostart)
            self.chk_shuffle.set_active(config.shuffle)
            self._set_repeat_mode(config.repeat_mode)
            self.chk_local_monitor.set_active(config.local_monitor)
            self.last_logo_dir = config.last_logo_dir if os.path.isdir(config.last_logo_dir) else ""
            self.playlist_folder_roots = [
                folder for folder in config.playlist_folder_roots if os.path.isdir(folder)
            ]
            self.playlist_group_enabled = {
                str(key): bool(value) for key, value in config.playlist_group_states.items()
            }
            self.playlist_group_expanded = {}
            self._refresh_dls_controls()

            self.playlist.clear()
            if config.playlist:
                self._start_config_playlist_load(config.playlist, config.playlist_overrides)
            else:
                self._refresh_pl()
        finally:
            self._applying_config = False

    def _collect_playlist_overrides(self):
        overrides = {}
        for index, track in enumerate(self.playlist):
            if not getattr(track, "manual_metadata", False):
                continue
            overrides[str(index)] = {
                "artist": track.artist,
                "title": track.title,
                "album": track.album,
            }
        return overrides

    def _prepare_sls_runtime_asset(self, track_override=None, allow_placeholder=False):
        track = track_override
        if track is None and self.runtime.proc_player is not None:
            track = self.playlist.current_track()

        result = build_sls_slide_set(
            default_logo_paths=self.default_logo_paths,
            include_title_card=self.chk_sls_title_card.get_active(),
            use_local_cover=self.chk_sls_cover_local.get_active(),
            fetch_cover_online=self.chk_sls_cover_online.get_active(),
            online_cache_only=self.chk_sls_cover_online.get_active(),
            include_default_logo=self.chk_sls_default_logo.get_active(),
            track=track,
            default_text=self.txt_dls.get_text().strip() or "Standby",
            allow_placeholder=allow_placeholder,
        )
        self.runtime.slide_paths = list(result.get("paths", ()))
        self.runtime.slide_wait_seconds = int(self.spn_slide_wait.get_value())
        self.runtime.slide_rotation_started_at = time.monotonic()
        self.runtime.slide_mtime = 0
        self.runtime.slide_preview_override_path = result["preview_path"]
        self.runtime.slide_preview_override_until = time.monotonic() + 4.0
        return (
            result["preview_path"],
            result["preview_generated"],
            result.get("preview_source", ""),
            result["count"],
        )

    def _describe_slide_file_markup(self, path, generated=False, slide_source="", slide_count=1):
        fmt, width, height = GdkPixbuf.Pixbuf.get_file_info(path)
        size_bytes = os.path.getsize(path)
        format_name = (
            fmt.get_name().upper()
            if fmt is not None and getattr(fmt, "get_name", None)
            else os.path.splitext(path)[1].lstrip(".").upper() or "UNKNOWN"
        )
        prefix = "Generated title card, " if generated else ""
        if slide_source:
            prefix += f"{slide_source}, "
        rotation = ""
        if slide_count > 1:
            rotation = f", {slide_count} slides, {int(self.spn_slide_wait.get_value())} s rotation"
        return f"<i>{prefix}{width}x{height}, {self._format_file_size(size_bytes)}, {format_name}{rotation}</i>"

    def _refresh_sls_preview_visibility(self):
        title_card_path = getattr(self, "sls_title_card_preview_path", None)
        title_card_visible = (
            hasattr(self, "chk_sls_title_card")
            and self.chk_sls_title_card.get_active()
            and bool(title_card_path)
            and os.path.isfile(title_card_path)
        )
        if hasattr(self, "box_sls_title_card_preview"):
            self.box_sls_title_card_preview.set_visible(title_card_visible)
        if hasattr(self, "chk_sls_cover_local"):
            self.chk_sls_cover_local.set_sensitive(self.chk_sls_title_card.get_active())
        if hasattr(self, "chk_sls_cover_online"):
            self.chk_sls_cover_online.set_sensitive(self.chk_sls_title_card.get_active())
        if hasattr(self, "box_sls_default_logo_preview"):
            self.box_sls_default_logo_preview.set_visible(
                hasattr(self, "chk_sls_default_logo")
                and self.chk_sls_default_logo.get_active()
            )

    def _build_sls_library_card(
        self,
        path,
        *,
        title_text=None,
        title_tooltip=None,
        removable=False,
        remove_path=None,
        info_markup=None,
        tooltip_markup=None,
    ):
        card = Gtk.Frame()
        card.set_shadow_type(Gtk.ShadowType.IN)
        card.set_size_request(178, 156)
        card.set_halign(Gtk.Align.START)
        card.set_valign(Gtk.Align.START)

        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        row.set_border_width(6)
        row.set_halign(Gtk.Align.FILL)
        row.set_valign(Gtk.Align.START)
        row.set_hexpand(True)
        card.add(row)

        header = Gtk.Box(spacing=4)
        header.set_size_request(-1, 34)
        title_label = Gtk.Label(label=title_text or self._compact_logo_name(path), xalign=0)
        title_label.set_tooltip_text(title_tooltip or os.path.basename(path))
        header.pack_start(title_label, True, True, 0)

        if removable and remove_path:
            remove_btn = Gtk.Button(label="✕")
            remove_btn.set_relief(Gtk.ReliefStyle.NONE)
            remove_btn.set_size_request(24, 24)
            remove_btn.connect("clicked", self.on_remove_default_logo, remove_path)
            header.pack_end(remove_btn, False, False, 0)
        else:
            spacer = Gtk.Box()
            spacer.set_size_request(24, 24)
            header.pack_end(spacer, False, False, 0)
        row.pack_start(header, False, False, 0)

        thumb = Gtk.Image()
        try:
            preview = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 88, 66, True)
            thumb.set_from_pixbuf(preview)
        except Exception:
            thumb.set_from_pixbuf(None)
        if tooltip_markup:
            thumb.set_tooltip_markup(tooltip_markup)

        thumb_box = Gtk.Box()
        thumb_box.set_size_request(88, 66)
        thumb_box.set_hexpand(True)
        thumb_box.set_halign(Gtk.Align.CENTER)
        thumb_box.set_valign(Gtk.Align.CENTER)
        thumb.set_halign(Gtk.Align.CENTER)
        thumb.set_valign(Gtk.Align.CENTER)
        thumb.set_hexpand(True)
        thumb.set_vexpand(True)
        thumb_box.pack_start(thumb, True, True, 0)

        click_box = Gtk.EventBox()
        click_box.set_visible_window(False)
        click_box.set_tooltip_text("Click to enlarge")
        click_box.connect("button-press-event", self._on_preview_click, path)
        click_box.add(thumb_box)
        row.pack_start(click_box, False, False, 0)

        info_label = Gtk.Label(xalign=0)
        info_label.set_use_markup(True)
        try:
            info_label.set_markup(info_markup or self._compact_logo_info_markup(path))
        except Exception:
            info_label.set_markup("<i>Preview unavailable.</i>")
        if tooltip_markup:
            info_label.set_tooltip_markup(tooltip_markup)
        row.pack_start(info_label, False, False, 0)
        return card

    def _refresh_title_card_preview(self):
        if not hasattr(self, "box_sls_title_card_slot"):
            return
        for child in list(self.box_sls_title_card_slot.get_children()):
            self.box_sls_title_card_slot.remove(child)

        title_card_path = getattr(self, "sls_title_card_preview_path", None)
        title_card_tooltip = getattr(self, "sls_title_card_preview_tooltip", None)
        title_card_visible = (
            self.chk_sls_title_card.get_active()
            and bool(title_card_path)
            and os.path.isfile(title_card_path)
        )
        if title_card_visible:
            self.box_sls_title_card_slot.pack_start(
                self._build_sls_library_card(
                    title_card_path,
                    title_text="Title card",
                    title_tooltip="Generated title card",
                    info_markup=self._compact_logo_info_markup(title_card_path),
                    tooltip_markup=title_card_tooltip,
                ),
                False,
                False,
                0,
            )
        if hasattr(self, "box_sls_title_card_preview"):
            self.box_sls_title_card_preview.set_visible(title_card_visible)
            self.box_sls_title_card_preview.show_all()
        self._refresh_preview_window_if_matching(title_card_path if title_card_visible else "")

    def _refresh_default_logo_library(self):
        if not hasattr(self, "box_sls_default_logos"):
            return
        for child in list(self.box_sls_default_logos.get_children()):
            self.box_sls_default_logos.remove(child)

        paths = normalize_default_logo_paths(self.default_logo_paths)
        self.default_logo_paths = paths
        include_default_logo = self.chk_sls_default_logo.get_active()
        if hasattr(self, "box_sls_default_logo_preview"):
            self.box_sls_default_logo_preview.set_visible(include_default_logo)

        if not include_default_logo:
            return

        if not paths:
            self.lbl_sls_default_logo_info = Gtk.Label(xalign=0)
            self.lbl_sls_default_logo_info.set_use_markup(True)
            self.lbl_sls_default_logo_info.set_markup("<i>No default logos loaded.</i>")
            self.box_sls_default_logos.pack_start(self.lbl_sls_default_logo_info, False, False, 0)
            self.box_sls_default_logos.show_all()
            return

        for path in paths:
            self.box_sls_default_logos.pack_start(
                self._build_sls_library_card(path, removable=True, remove_path=path),
                False,
                False,
                0,
            )

        self.box_sls_default_logos.show_all()

    def _compact_logo_name(self, path):
        name = os.path.basename(path)
        if len(name) <= 14:
            return name
        root, ext = os.path.splitext(name)
        compact_root = root[:10].rstrip("-_ ")
        return f"{compact_root}…{ext}"

    def _compact_logo_info_markup(self, path):
        fmt, width, height = GdkPixbuf.Pixbuf.get_file_info(path)
        size_bytes = os.path.getsize(path)
        format_name = (
            fmt.get_name().upper()
            if fmt is not None and getattr(fmt, "get_name", None)
            else os.path.splitext(path)[1].lstrip(".").upper() or "UNKNOWN"
        )
        return f"<i>{width}x{height}\n{self._format_file_size(size_bytes)} {format_name}</i>"

    def _update_sls_source_preview(self, track_override=None):
        if not hasattr(self, "box_sls_default_logos"):
            return
        self.sls_title_card_preview_path = None
        self.sls_title_card_preview_tooltip = None
        current_track = track_override
        if current_track is None and self.runtime.proc_player is not None:
            current_track = self.playlist.current_track()

        try:
            preview_path, generated, slide_source, slide_count = self._prepare_sls_runtime_asset(
                track_override=track_override,
                allow_placeholder=True,
            )
            if self.chk_sls_title_card.get_active() and current_track is not None:
                self.sls_title_card_preview_path = preview_path
                self.sls_title_card_preview_tooltip = self._describe_slide_file_markup(
                    preview_path,
                    generated=generated,
                    slide_source=slide_source,
                    slide_count=slide_count,
                )
            self._refresh_title_card_preview()
            self._refresh_default_logo_library()
            self._refresh_sls_preview_visibility()
            self._update_sls_pad_estimate()
            if self.chk_sls.get_active():
                self._set_output_slide_image(preview_path)
            else:
                self._set_output_slide_image(None, "Slideshows desactivate")
            self._schedule_async_cover_fetch(current_track, slide_source)
        except ValueError:
            self.runtime.slide_paths = []
            self.runtime.slide_rotation_started_at = 0.0
            self.runtime.slide_mtime = 0
            self.runtime.slide_preview_override_path = ""
            self.runtime.slide_preview_override_until = 0.0
            self._refresh_title_card_preview()
            self._refresh_default_logo_library()
            self._refresh_sls_preview_visibility()
            self._update_sls_pad_estimate()
            self._set_output_slide_image(None, "Slideshows desactivate" if not self.chk_sls.get_active() else None)
        except Exception:
            self.runtime.slide_paths = []
            self.runtime.slide_rotation_started_at = 0.0
            self.runtime.slide_mtime = 0
            self.runtime.slide_preview_override_path = ""
            self.runtime.slide_preview_override_until = 0.0
            self._refresh_title_card_preview()
            self._refresh_default_logo_library()
            self._refresh_sls_preview_visibility()
            self._update_sls_pad_estimate()
            self._set_output_slide_image(None, "Slideshows desactivate" if not self.chk_sls.get_active() else None)

    def _schedule_async_cover_fetch(self, track, slide_source=""):
        if track is None:
            return
        if not self.chk_sls_title_card.get_active() or not self.chk_sls_cover_online.get_active():
            return
        if slide_source.startswith("embedded cover art") or slide_source.startswith("local cover art"):
            return
        if slide_source.startswith("online cover art"):
            return

        key = self._cover_fetch_key(track)
        if not key or key in self._pending_cover_fetch_keys:
            return

        self._pending_cover_fetch_keys.add(key)
        worker = threading.Thread(
            target=self._async_cover_fetch_worker,
            args=(
                key,
                track.path,
                track.artist,
                track.title,
                track.album,
                self.chk_sls_cover_local.get_active(),
            ),
            daemon=True,
        )
        worker.start()

    def _cover_fetch_key(self, track):
        if track is None:
            return ""
        path = (track.path or "").strip()
        artist = (track.artist or "").strip()
        title = (track.title or "").strip()
        album = (track.album or "").strip()
        if not path or (not artist and not title):
            return ""
        return "\x1f".join((path, artist, title, album))

    def _async_cover_fetch_worker(self, key, path, artist, title, album, use_local_cover):
        artwork_path = ""
        try:
            track = Track(path=path, artist=artist, title=title, album=album, duration="")
            artwork_path, _artwork_source = resolve_track_artwork(
                track,
                use_local=use_local_cover,
                fetch_online=True,
                online_cache_only=False,
            )
        except Exception:
            artwork_path = ""
        GLib.idle_add(self._on_async_cover_fetch_done, key, path, artist, title, album, bool(artwork_path))

    def _on_async_cover_fetch_done(self, key, path, artist, title, album, found_artwork):
        self._pending_cover_fetch_keys.discard(key)
        if not found_artwork:
            return False
        if not self.chk_sls_title_card.get_active() or not self.chk_sls_cover_online.get_active():
            return False

        current = self.playlist.current_track()
        if current is None:
            return False
        if self._cover_fetch_key(current) != key:
            return False
        if current.path != path or current.artist != artist or current.title != title or current.album != album:
            return False

        self._update_sls_source_preview(track_override=current)
        self._update_monitor()
        self.log(f"Cover art cached: {now_playing_label(current)}")
        return False

    def _format_file_size(self, size_bytes):
        size = float(max(0, size_bytes))
        units = ["B", "KB", "MB", "GB"]
        unit_index = 0
        while size >= 1024.0 and unit_index < len(units) - 1:
            size /= 1024.0
            unit_index += 1
        if unit_index == 0:
            return f"{int(size)} {units[unit_index]}"
        return f"{size:.1f} {units[unit_index]}"

    def _update_sls_pad_estimate(self):
        if not hasattr(self, "lbl_sls_pad_estimate"):
            return

        if not self.chk_sls.get_active():
            self.lbl_sls_pad_estimate.set_markup("<i>SLS disabled.</i>")
            return

        try:
            pad_len = int((self.cmb_pad_len.get_active_text() or "58").strip())
        except ValueError:
            pad_len = 58

        rotation_seconds = int(self.spn_slide_wait.get_value())
        estimate = estimate_sls_delivery(self.runtime.slide_paths, pad_len, rotation_seconds)
        if estimate["count"] <= 0:
            self.lbl_sls_pad_estimate.set_markup("<i>No active slideshow image.</i>")
            return

        useful_rate = estimate["useful_bytes_per_sec"] / 1024.0
        max_size = self._format_file_size(estimate["max_size"])
        avg_size = self._format_file_size(estimate["avg_size"])
        seconds_per_slide = max(1, int(round(estimate["seconds_per_slide"])))

        summary = (
            f"<b>PAD/SLS estimate:</b> {estimate['count']} slide(s), avg {avg_size}, max {max_size}, "
            f"PAD {pad_len} ≈ {useful_rate:.1f} KB/s useful, ~{seconds_per_slide} s for the largest slide."
        )
        if rotation_seconds < estimate["seconds_per_slide"]:
            status = (
                f"<span foreground='#c01c28'><b>Warning:</b> {rotation_seconds} s rotation is likely too fast. "
                f"Recommended: at least {estimate['recommended_rotation']} s.</span>"
            )
        else:
            status = (
                f"<span foreground='#2b7a0b'><b>OK:</b> {rotation_seconds} s rotation should fit. "
                f"Recommended minimum with margin: {estimate['recommended_rotation']} s.</span>"
            )
        self.lbl_sls_pad_estimate.set_markup(f"{summary}\n{status}")

    def _set_output_slide_image(self, path, placeholder_text=None):
        if not hasattr(self, "img_slide"):
            return

        placeholder = getattr(self, "lbl_slide_placeholder", None)
        if not path or not os.path.isfile(path):
            self.current_output_slide_path = ""
            self.img_slide.set_from_pixbuf(None)
            self._refresh_preview_window_if_live_output("")
            if placeholder is not None:
                text = placeholder_text or "Slideshows desactivate"
                placeholder.set_markup(f"<i>{GLib.markup_escape_text(text)}</i>")
                placeholder.show()
            return
        try:
            width, height = getattr(self, "output_slide_size", (320, 240))
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, width, height, True)
            self.current_output_slide_path = path
            self.img_slide.set_from_pixbuf(pb)
            self._refresh_preview_window_if_live_output(path)
            if placeholder is not None:
                placeholder.hide()
        except Exception:
            self.current_output_slide_path = ""
            self.img_slide.set_from_pixbuf(None)
            self._refresh_preview_window_if_live_output("")
            if placeholder is not None:
                text = placeholder_text or "Slideshows desactivate"
                placeholder.set_markup(f"<i>{GLib.markup_escape_text(text)}</i>")
                placeholder.show()

    def _on_preview_click(self, widget, event, path):
        if getattr(event, "button", 0) != 1:
            return False
        self._open_preview_image(path, live_output=False)
        return True

    def _open_preview_image(self, path, live_output=False):
        path = (path or "").strip()
        if not path or not os.path.isfile(path):
            return

        pixbuf = self._load_preview_pixbuf(path)
        if pixbuf is None:
            return

        if self.preview_window is None:
            win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
            win.set_transient_for(self)
            win.set_modal(False)
            win.set_decorated(False)
            win.set_resizable(False)
            win.set_skip_taskbar_hint(True)
            win.set_skip_pager_hint(True)
            win.set_keep_above(True)
            win.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
            win.connect("destroy", self._on_preview_window_destroy)
            win.connect("key-press-event", self._on_preview_window_key)

            click_box = Gtk.EventBox()
            click_box.set_visible_window(False)
            click_box.connect("button-press-event", self._on_preview_window_click)

            self.preview_image = Gtk.Image()
            self.preview_image.set_halign(Gtk.Align.CENTER)
            self.preview_image.set_valign(Gtk.Align.CENTER)
            click_box.add(self.preview_image)
            win.add(click_box)
            self.preview_window = win
            win.show_all()

        self.preview_window_live_output = bool(live_output)
        self.preview_window_path = path
        self._set_preview_window_pixbuf(pixbuf)
        self.preview_window.present()

    def _load_preview_pixbuf(self, path):
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
        except Exception:
            return None

        workarea = self._get_monitor_workarea()
        max_width = 900
        max_height = 700
        if workarea is not None:
            max_width = max(320, min(1100, int(workarea.width * 0.72)))
            max_height = max(240, min(820, int(workarea.height * 0.78)))

        width = pixbuf.get_width()
        height = pixbuf.get_height()
        if width > max_width or height > max_height:
            scale = min(max_width / float(width), max_height / float(height))
            width = max(1, int(round(width * scale)))
            height = max(1, int(round(height * scale)))
            pixbuf = pixbuf.scale_simple(width, height, GdkPixbuf.InterpType.BILINEAR)
        return pixbuf

    def _set_preview_window_pixbuf(self, pixbuf):
        if self.preview_window is None or self.preview_image is None or pixbuf is None:
            return
        self.preview_image.set_from_pixbuf(pixbuf)
        self.preview_window.resize(
            max(1, pixbuf.get_width()),
            max(1, pixbuf.get_height()),
        )

    def _refresh_preview_window_if_live_output(self, path):
        if self.preview_window is None or not self.preview_window_live_output:
            return
        self._refresh_preview_window(path)

    def _refresh_preview_window_if_matching(self, path):
        if self.preview_window is None or self.preview_window_live_output:
            return
        if self.preview_window_path != (path or "").strip():
            return
        self._refresh_preview_window(path)

    def _refresh_preview_window(self, path):
        path = (path or "").strip()
        if not path or not os.path.isfile(path):
            if self.preview_window is not None:
                self.preview_window.destroy()
            return
        pixbuf = self._load_preview_pixbuf(path)
        if pixbuf is None:
            return
        self.preview_window_path = path
        self._set_preview_window_pixbuf(pixbuf)

    def _on_preview_window_click(self, widget, event):
        if self.preview_window is not None:
            self.preview_window.destroy()
        return True

    def _on_preview_window_key(self, widget, event):
        if event.keyval in (Gdk.KEY_Escape, Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            widget.destroy()
            return True
        return False

    def _on_preview_window_destroy(self, widget):
        if widget is self.preview_window:
            self.preview_window = None
            self.preview_image = None
            self.preview_window_path = ""
            self.preview_window_live_output = False

    # ============================================================
    # UTILITAIRES
    # ============================================================
    def _set_label_color(self, lbl, color):
        set_status_label_markup(lbl, color)

    def _msg_info(self, text):
        show_message(self, Gtk.MessageType.INFO, text)

    def _msg_warn(self, text):
        show_message(self, Gtk.MessageType.WARNING, text)

    def _msg_err(self, text):
        show_message(self, Gtk.MessageType.ERROR, text)


# ============================================================
# POINT D'ENTRÉE
# ============================================================
if __name__ == "__main__":
    import locale
    locale.setlocale(locale.LC_NUMERIC, "C")
    app = ODRFilePlayer()
    Gtk.main()
