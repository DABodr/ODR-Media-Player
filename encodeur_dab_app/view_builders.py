from gi.repository import Gtk

from .app_config import DEFAULT_DLS_TEXT
from .constants import BITRATES, PAD_LENGTHS


def build_ui(owner):
    _build_headerbar(owner)
    owner.root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    owner.root_box.set_border_width(4)
    owner.add(owner.root_box)
    owner.notebook = Gtk.Notebook()
    owner.root_box.pack_start(owner.notebook, True, True, 0)
    _build_tab_lecteur(owner)
    _build_tab_pad(owner)
    _build_tab_moniteur(owner)
    _build_player_strip(owner)


def _append_tab(owner, child, title, scrollable=False):
    page = child
    if scrollable:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.NONE)
        scroller.add(child)
        page = scroller
    owner.notebook.append_page(page, Gtk.Label(label=title))


def _build_headerbar(owner):
    hb = Gtk.HeaderBar()
    hb.set_show_close_button(True)
    hb.set_title("ODR Media Player V1.1.2")

    owner.set_titlebar(hb)


def _build_tab_lecteur(owner):
    vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.set_shadow_type(Gtk.ShadowType.IN)
    owner.store_pl = Gtk.TreeStore(int, str, str, bool, bool, bool)
    owner.tv_pl = Gtk.TreeView(model=owner.store_pl)
    toggle_renderer = Gtk.CellRendererToggle()
    toggle_renderer.connect("toggled", owner.on_playlist_group_toggled)
    toggle_col = Gtk.TreeViewColumn("", toggle_renderer)
    toggle_col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
    toggle_col.set_fixed_width(34)
    toggle_col.set_cell_data_func(toggle_renderer, owner._playlist_toggle_cell_data)
    owner.tv_pl.append_column(toggle_col)

    text_renderer = Gtk.CellRendererText()
    text_renderer.set_property("ellipsize", 3)
    col = Gtk.TreeViewColumn("Track", text_renderer)
    col.set_cell_data_func(text_renderer, owner._playlist_text_cell_data)
    owner.tv_pl.append_column(col)
    owner.tv_pl.set_expander_column(col)
    owner.tv_pl.set_headers_visible(False)
    owner.tv_pl.set_reorderable(True)
    owner.tv_pl.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
    owner.tv_pl.connect("row-activated", owner._on_pl_dblclick)
    owner.tv_pl.connect("row-expanded", owner.on_playlist_row_expanded)
    owner.tv_pl.connect("row-collapsed", owner.on_playlist_row_collapsed)
    owner.store_pl.connect("rows-reordered", owner.on_playlist_rows_reordered)
    scroll.add(owner.tv_pl)

    owner.player_stack = Gtk.Stack()
    owner.player_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
    owner.player_stack.set_transition_duration(120)
    owner.player_stack.add_named(scroll, "playlist")

    empty_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    empty_box.set_hexpand(True)
    empty_box.set_vexpand(True)
    empty_box.set_halign(Gtk.Align.CENTER)
    empty_box.set_valign(Gtk.Align.CENTER)

    empty_title = Gtk.Label(xalign=0.5)
    empty_title.set_use_markup(True)
    empty_title.set_markup("<b>No tracks loaded</b>")
    owner.player_empty_title = empty_title
    empty_box.pack_start(empty_title, False, False, 0)

    empty_hint = Gtk.Label(xalign=0.5)
    empty_hint.set_use_markup(True)
    empty_hint.set_markup("<i>Use Add or Folder to build grouped music folders.</i>")
    owner.player_empty_hint = empty_hint
    empty_box.pack_start(empty_hint, False, False, 0)

    owner.player_stack.add_named(empty_box, "empty")
    owner.player_stack.set_visible_child_name("empty")
    vbox.pack_start(owner.player_stack, True, True, 0)

    hb = Gtk.Box(spacing=4)
    owner.player_actions_bar = hb
    for label, cb in [
        ("Add", owner.on_add_files),
        ("Add playlist", owner.on_add_playlist),
        ("Add URL", owner.on_add_url),
        ("App audio", owner.on_add_app_audio),
        ("Audio input", owner.on_add_audio_input),
        ("Folder", owner.on_add_folder),
        ("Edit", owner.on_edit_entry),
        ("↑", owner.on_move_up),
        ("↓", owner.on_move_down),
        ("Remove", owner.on_remove),
        ("Clear", owner.on_clear),
    ]:
        button = Gtk.Button(label=label)
        button.connect("clicked", cb)
        hb.pack_start(button, False, False, 0)

    vbox.pack_start(hb, False, False, 0)

    _append_tab(owner, vbox, "  Player  ")


def _build_player_strip(owner):
    frame = Gtk.Frame(label="Now Playing")
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    content.set_border_width(8)
    frame.add(content)

    owner.scale_player_seek = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
    owner.scale_player_seek.set_draw_value(False)
    owner.scale_player_seek.set_hexpand(True)
    owner.scale_player_seek.set_sensitive(False)
    owner.scale_player_seek.connect("button-press-event", owner.on_player_seek_press)
    owner.scale_player_seek.connect("button-release-event", owner.on_player_seek_release)
    owner.scale_player_seek.connect("value-changed", owner.on_player_seek_value_changed)
    content.pack_start(owner.scale_player_seek, False, False, 0)

    now_row = Gtk.Box(spacing=8)

    owner.lbl_now = Gtk.Label(label="—")
    owner.lbl_now.set_xalign(0)
    owner.lbl_now.set_use_markup(True)
    owner.lbl_now.set_ellipsize(3)
    owner.lbl_now.set_hexpand(False)
    now_row.pack_start(owner.lbl_now, False, False, 0)

    owner.lbl_now_countdown = Gtk.Label(label="—")
    owner.lbl_now_countdown.set_xalign(1)
    owner.lbl_now_countdown.set_width_chars(6)
    owner.lbl_now_countdown.set_hexpand(False)
    owner.lbl_now_countdown.set_margin_start(10)
    now_row.pack_start(owner.lbl_now_countdown, False, False, 0)

    owner.lbl_now_retry = Gtk.Label(label="")
    owner.lbl_now_retry.set_xalign(0)
    owner.lbl_now_retry.set_use_markup(True)
    owner.lbl_now_retry.set_no_show_all(True)
    now_row.pack_start(owner.lbl_now_retry, False, False, 0)

    content.pack_start(now_row, False, False, 0)

    controls_row = Gtk.Box(spacing=8)
    controls_row.set_margin_top(4)

    transport = Gtk.Box(spacing=4)
    owner.btn_prev = Gtk.Button(label="◀◀")
    owner.btn_prev.connect("clicked", owner.on_prev)
    owner.chk_playlist_autostart = Gtk.CheckButton(label="Autostart")
    owner.btn_play = Gtk.Button(label="▶  Play")
    owner.btn_play.connect("clicked", owner.on_play_pause)
    owner.btn_stop_pl = Gtk.Button(label="■")
    owner.btn_stop_pl.connect("clicked", owner.on_stop_play)
    owner.btn_next = Gtk.Button(label="▶▶")
    owner.btn_next.connect("clicked", owner.on_next)
    owner.chk_shuffle = Gtk.CheckButton(label="Shuffle")
    owner.btn_repeat_mode = Gtk.MenuButton()
    owner.btn_repeat_mode.set_size_request(126, -1)
    repeat_box = Gtk.Box(spacing=4)
    owner.lbl_repeat_mode = Gtk.Label(label="Repeat off")
    repeat_box.pack_start(owner.lbl_repeat_mode, False, False, 0)
    repeat_box.pack_start(Gtk.Label(label="▾"), False, False, 0)
    owner.btn_repeat_mode.add(repeat_box)

    owner.repeat_mode_popover = Gtk.Popover.new(owner.btn_repeat_mode)
    repeat_menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    repeat_menu_box.set_border_width(6)
    for mode, label in owner.REPEAT_MODE_OPTIONS:
        btn = Gtk.ModelButton(label=label)
        btn.set_halign(Gtk.Align.FILL)
        btn.connect("clicked", owner.on_repeat_mode_selected, mode)
        repeat_menu_box.pack_start(btn, False, False, 0)
    owner.repeat_mode_popover.add(repeat_menu_box)
    repeat_menu_box.show_all()
    owner.btn_repeat_mode.set_popover(owner.repeat_mode_popover)
    owner.chk_local_monitor = Gtk.CheckButton(label="Local monitor")
    owner.chk_local_monitor.connect("toggled", owner.on_local_monitor_toggled)
    for widget in [
        owner.btn_prev,
        owner.chk_playlist_autostart,
        owner.btn_play,
        owner.btn_stop_pl,
        owner.btn_next,
        owner.chk_shuffle,
        owner.btn_repeat_mode,
        owner.chk_local_monitor,
    ]:
        transport.pack_start(widget, False, False, 0)
    controls_row.pack_start(transport, False, False, 0)

    content.pack_start(controls_row, False, False, 0)
    owner.root_box.pack_start(frame, False, False, 0)


def _build_encoding_panel(owner):
    frame = Gtk.Frame(label="Encoder settings")
    panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    panel.set_border_width(6)
    frame.add(panel)

    owner.lbl_src_info = Gtk.Label(label="Source: ALSA Loopback  (hw:?,1)")
    owner.lbl_src_info.set_xalign(0)
    owner.lbl_src_info.set_hexpand(True)
    owner.lbl_src_info.set_ellipsize(3)
    panel.pack_start(owner.lbl_src_info, False, False, 0)

    owner.cmb_bitrate = Gtk.ComboBoxText()
    for item in BITRATES:
        owner.cmb_bitrate.append_text(item)
    owner.cmb_bitrate.set_active(15)
    owner.cmb_bitrate.set_size_request(92, -1)

    owner.cmb_channels = Gtk.ComboBoxText()
    for item in ["Mono  (1)", "Stereo  (2)"]:
        owner.cmb_channels.append_text(item)
    owner.cmb_channels.set_active(1)
    owner.cmb_channels.connect("changed", owner.on_codec_change)
    owner.cmb_channels.set_size_request(118, -1)

    owner.cmb_samplerate = Gtk.ComboBoxText()
    for item in ["32000 Hz", "48000 Hz"]:
        owner.cmb_samplerate.append_text(item)
    owner.cmb_samplerate.set_active(1)
    owner.cmb_samplerate.set_size_request(110, -1)

    owner.cmb_codec = Gtk.ComboBoxText()
    for item in ["AAC-LC", "HE-AAC v1  (SBR)", "HE-AAC v2  (SBR + PS)"]:
        owner.cmb_codec.append_text(item)
    owner.cmb_codec.set_active(1)
    owner.cmb_codec.connect("changed", owner.on_codec_change)
    owner.cmb_codec.set_size_request(185, -1)

    owner.cmb_pad_len = Gtk.ComboBoxText()
    for length in PAD_LENGTHS:
        owner.cmb_pad_len.append_text(length)
    owner.cmb_pad_len.set_active(3)
    owner.cmb_pad_len.set_size_request(72, -1)

    owner.spn_gain = Gtk.SpinButton.new_with_range(-30, 30, 1)
    owner.spn_gain.set_value(0)
    owner.spn_gain.set_width_chars(5)
    owner.spn_gain.set_size_request(80, -1)

    owner.cmb_output_proto = Gtk.ComboBoxText()
    for p in ["ZMQ (tcp)", "EDI (udp)"]:
        owner.cmb_output_proto.append_text(p)
    owner.cmb_output_proto.set_active(0)
    owner.cmb_output_proto.set_size_request(105, -1)

    owner.txt_output_host = Gtk.Entry()
    owner.txt_output_host.set_text("localhost")
    owner.txt_output_host.set_width_chars(14)
    owner.txt_output_host.set_max_width_chars(24)
    owner.txt_output_host.set_size_request(170, -1)

    owner.spn_output_port = Gtk.SpinButton.new_with_range(1, 65535, 1)
    owner.spn_output_port.set_value(9000)
    owner.spn_output_port.set_width_chars(6)
    owner.spn_output_port.set_size_request(95, -1)

    owner.spn_silence = Gtk.SpinButton.new_with_range(0, 3600, 1)
    owner.spn_silence.set_value(180)
    owner.spn_silence.set_width_chars(6)
    owner.spn_silence.set_size_request(92, -1)

    row1 = Gtk.Box(spacing=8)
    row2 = Gtk.Box(spacing=8)
    panel.pack_start(row1, False, False, 0)
    panel.pack_start(row2, False, False, 0)

    def add_inline(container, label_text, widget):
        field = Gtk.Box(spacing=6)
        label = Gtk.Label(label=label_text, xalign=0)
        field.pack_start(label, False, False, 0)
        field.pack_start(widget, False, False, 0)
        container.pack_start(field, False, False, 0)

    add_inline(row1, "Bitrate", owner.cmb_bitrate)
    add_inline(row1, "Channels", owner.cmb_channels)
    add_inline(row1, "Sample rate", owner.cmb_samplerate)
    add_inline(row1, "Codec", owner.cmb_codec)
    add_inline(row1, "PAD", owner.cmb_pad_len)

    add_inline(row2, "Gain", owner.spn_gain)
    add_inline(row2, "Output", owner.cmb_output_proto)
    add_inline(row2, "URL", owner.txt_output_host)
    add_inline(row2, "Port", owner.spn_output_port)
    add_inline(row2, "Silence warn", owner.spn_silence)

    return frame


def _build_tab_pad(owner):
    vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    vbox.set_border_width(4)

    frame_dls = Gtk.Frame(label="DLS")
    grid = Gtk.Grid(row_spacing=6, column_spacing=8)
    grid.set_border_width(8)
    frame_dls.add(grid)

    grid.attach(Gtk.Label(label="Default DLS:", xalign=1), 0, 0, 1, 1)
    owner.txt_dls = Gtk.Entry()
    owner.txt_dls.set_text(DEFAULT_DLS_TEXT)
    owner.txt_dls.set_hexpand(True)
    grid.attach(owner.txt_dls, 1, 0, 1, 1)
    btn_send = Gtk.Button(label="Send")
    btn_send.connect("clicked", owner.on_send_dls)
    grid.attach(btn_send, 2, 0, 1, 1)

    owner.chk_force_default_dls = Gtk.CheckButton(
        label="Force default DLS (ignore track metadata)"
    )
    owner.chk_force_default_dls.connect("toggled", owner.on_dls_settings_changed)
    grid.attach(owner.chk_force_default_dls, 1, 1, 2, 1)

    owner.chk_dls_from_file = Gtk.CheckButton(
        label="DLS from audio file (artist / title)"
    )
    owner.chk_dls_from_file.connect("toggled", owner.on_dls_settings_changed)
    grid.attach(owner.chk_dls_from_file, 1, 2, 2, 1)

    owner.chk_dl_plus = Gtk.CheckButton(label="Enable DL+ (structured format)")
    owner.chk_dl_plus.connect("toggled", owner.on_dls_settings_changed)
    grid.attach(owner.chk_dl_plus, 1, 3, 2, 1)
    vbox.pack_start(frame_dls, False, False, 0)

    frame_sls = Gtk.Frame(label="SLS — Logo")
    grid2 = Gtk.Grid(row_spacing=6, column_spacing=8)
    grid2.set_border_width(8)
    frame_sls.add(grid2)

    owner.chk_sls = Gtk.CheckButton(label="Enable SLS logo")
    owner.chk_sls.connect("toggled", owner.on_slide_source_changed)
    grid2.attach(owner.chk_sls, 0, 0, 3, 1)

    grid2.attach(Gtk.Label(label="Default logos:", xalign=1), 0, 1, 1, 1)
    owner.btn_add_default_logo = Gtk.Button(label="Add logo")
    owner.btn_add_default_logo.connect("clicked", owner.on_add_default_logo)
    owner.btn_add_default_logo.set_halign(Gtk.Align.START)
    owner.btn_add_default_logo.set_size_request(120, -1)
    grid2.attach(owner.btn_add_default_logo, 1, 1, 1, 1)

    owner.chk_sls_title_card = Gtk.CheckButton(label="Generate title card from metadata")
    owner.chk_sls_title_card.connect("toggled", owner.on_slide_source_changed)
    grid2.attach(owner.chk_sls_title_card, 1, 2, 2, 1)

    owner.chk_sls_cover_local = Gtk.CheckButton(label="Use local cover art")
    owner.chk_sls_cover_local.set_active(True)
    owner.chk_sls_cover_local.connect("toggled", owner.on_slide_source_changed)
    grid2.attach(owner.chk_sls_cover_local, 1, 3, 2, 1)

    owner.chk_sls_cover_online = Gtk.CheckButton(label="Fetch cover art online")
    owner.chk_sls_cover_online.connect("toggled", owner.on_slide_source_changed)
    grid2.attach(owner.chk_sls_cover_online, 1, 4, 2, 1)

    owner.chk_sls_default_logo = Gtk.CheckButton(label="Include default logo")
    owner.chk_sls_default_logo.set_active(True)
    owner.chk_sls_default_logo.connect("toggled", owner.on_slide_source_changed)
    grid2.attach(owner.chk_sls_default_logo, 1, 5, 2, 1)

    grid2.attach(Gtk.Label(label="Rotation (s):", xalign=1), 0, 6, 1, 1)
    owner.spn_slide_wait = Gtk.SpinButton.new_with_range(1, 3600, 1)
    owner.spn_slide_wait.set_value(10)
    owner.spn_slide_wait.connect("value-changed", owner.on_slide_source_changed)
    grid2.attach(owner.spn_slide_wait, 1, 6, 1, 1)

    note = Gtk.Label(label="Automatic conversion to optimized 320x240 JPEG", xalign=0)
    note.set_hexpand(True)
    grid2.attach(note, 1, 7, 2, 1)

    owner.lbl_sls_pad_estimate = Gtk.Label(xalign=0)
    owner.lbl_sls_pad_estimate.set_use_markup(True)
    owner.lbl_sls_pad_estimate.set_line_wrap(True)
    owner.lbl_sls_pad_estimate.set_max_width_chars(78)
    grid2.attach(owner.lbl_sls_pad_estimate, 1, 8, 2, 1)

    assets_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    assets_box.set_halign(Gtk.Align.START)
    assets_box.set_valign(Gtk.Align.START)

    title_card_frame = Gtk.Frame(label="Title card")
    owner.box_sls_title_card_preview = title_card_frame
    title_card_frame.set_halign(Gtk.Align.START)
    title_card_frame.set_valign(Gtk.Align.START)

    owner.box_sls_title_card_slot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    owner.box_sls_title_card_slot.set_halign(Gtk.Align.START)
    owner.box_sls_title_card_slot.set_valign(Gtk.Align.START)
    owner.box_sls_title_card_slot.set_margin_top(6)
    owner.box_sls_title_card_slot.set_margin_bottom(6)
    owner.box_sls_title_card_slot.set_margin_start(6)
    owner.box_sls_title_card_slot.set_margin_end(6)
    title_card_frame.add(owner.box_sls_title_card_slot)
    assets_box.pack_start(title_card_frame, False, False, 0)

    logos_frame = Gtk.Frame(label="Default logo library")
    owner.box_sls_default_logo_preview = logos_frame
    logos_frame.set_halign(Gtk.Align.START)
    logos_frame.set_valign(Gtk.Align.START)
    logos_frame.set_size_request(940, -1)
    logos_scroller = Gtk.ScrolledWindow()
    logos_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
    logos_scroller.set_min_content_height(106)
    logos_scroller.set_min_content_width(916)
    logos_frame.add(logos_scroller)

    owner.box_sls_default_logos = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    owner.box_sls_default_logos.set_halign(Gtk.Align.START)
    owner.box_sls_default_logos.set_valign(Gtk.Align.START)
    owner.box_sls_default_logos.set_margin_top(6)
    owner.box_sls_default_logos.set_margin_bottom(6)
    owner.box_sls_default_logos.set_margin_start(6)
    owner.box_sls_default_logos.set_margin_end(6)
    logos_scroller.add(owner.box_sls_default_logos)

    owner.lbl_sls_default_logo_info = Gtk.Label(xalign=0)
    owner.lbl_sls_default_logo_info.set_use_markup(True)
    owner.lbl_sls_default_logo_info.set_markup("<i>No default logos loaded.</i>")
    owner.box_sls_default_logos.pack_start(owner.lbl_sls_default_logo_info, False, False, 0)

    assets_box.pack_start(logos_frame, False, False, 0)
    grid2.attach(assets_box, 1, 9, 2, 2)
    vbox.pack_start(frame_sls, False, False, 0)

    _append_tab(owner, vbox, "  PAD / DLS  ", scrollable=True)


def _build_control_panel(owner):
    frame_status = Gtk.Frame(label="Process status")
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    content.set_border_width(8)
    frame_status.add(content)

    status_grid = Gtk.Grid(row_spacing=6, column_spacing=8)
    status_grid.set_halign(Gtk.Align.START)
    status_grid.set_hexpand(False)
    content.pack_start(status_grid, False, False, 0)

    lbl_audio_name = Gtk.Label(label="odr-audioenc :", xalign=0)
    lbl_audio_name.set_halign(Gtk.Align.START)
    status_grid.attach(lbl_audio_name, 0, 0, 1, 1)
    owner.lbl_audio_st = Gtk.Label(label="● Stopped", xalign=0)
    owner.lbl_audio_st.set_halign(Gtk.Align.START)
    owner._set_label_color(owner.lbl_audio_st, "red")
    status_grid.attach(owner.lbl_audio_st, 1, 0, 1, 1)

    lbl_pad_name = Gtk.Label(label="odr-padenc :", xalign=0)
    lbl_pad_name.set_halign(Gtk.Align.START)
    status_grid.attach(lbl_pad_name, 0, 1, 1, 1)
    owner.lbl_pad_st = Gtk.Label(label="● Stopped", xalign=0)
    owner.lbl_pad_st.set_halign(Gtk.Align.START)
    owner._set_label_color(owner.lbl_pad_st, "red")
    status_grid.attach(owner.lbl_pad_st, 1, 1, 1, 1)

    hb = Gtk.Box(spacing=3)
    hb.set_halign(Gtk.Align.FILL)
    hb.set_hexpand(True)
    owner.chk_encoder_autostart = Gtk.CheckButton(label="Autostart")
    owner.btn_start = Gtk.Button(label="▶  Start")
    owner.btn_start.connect("clicked", owner.on_start)
    owner.btn_restart_enc = Gtk.Button(label="↻  Restart encoder")
    owner.btn_restart_enc.set_sensitive(False)
    owner.btn_restart_enc.connect("clicked", owner.on_restart_enc)
    owner.btn_stop_enc = Gtk.Button(label="■  Stop")
    owner.btn_stop_enc.set_sensitive(False)
    owner.btn_stop_enc.connect("clicked", owner.on_stop_enc)
    owner.btn_show_log = Gtk.Button(label="Log")
    owner.btn_show_log.connect("clicked", owner.on_show_log)
    btn_save = Gtk.Button(label="Save config")
    btn_save.connect("clicked", owner.on_save_config)
    hb.pack_start(owner.chk_encoder_autostart, False, False, 0)
    hb.pack_start(owner.btn_start, False, False, 0)
    hb.pack_start(owner.btn_restart_enc, False, False, 0)
    hb.pack_start(owner.btn_stop_enc, False, False, 0)
    hb.pack_start(owner.btn_show_log, False, False, 0)
    hb.pack_end(btn_save, False, False, 0)
    content.pack_start(hb, False, False, 0)
    owner.buf_log = Gtk.TextBuffer()
    owner.tv_log = None
    owner.log_window = None
    return frame_status


def _build_tab_moniteur(owner):
    vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    vbox.set_border_width(4)
    vbox.set_valign(Gtk.Align.START)

    frame_status = _build_control_panel(owner)
    frame_status.set_vexpand(False)
    frame_status.set_valign(Gtk.Align.START)
    vbox.pack_start(frame_status, False, False, 0)

    frame = Gtk.Frame(label="Output")
    frame.set_vexpand(False)
    frame.set_valign(Gtk.Align.START)
    frame_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    frame_box.set_border_width(8)
    frame.add(frame_box)

    header_row = Gtk.Box(spacing=6)
    header_row.set_hexpand(True)
    owner.btn_output_versions = Gtk.Button(label="?")
    owner.btn_output_versions.set_size_request(28, 28)
    owner.btn_output_versions.set_focus_on_click(False)
    owner.btn_output_versions.set_tooltip_text("Show encoder and ImageMagick versions")
    owner.btn_output_versions.connect("clicked", owner.on_show_output_versions)
    header_row.pack_end(owner.btn_output_versions, False, False, 0)
    frame_box.pack_start(header_row, False, False, 0)

    level_row = Gtk.Box(spacing=8)
    level_row.pack_start(Gtk.Label(label="Level:", xalign=0), False, False, 0)
    owner.dwa_vu = Gtk.DrawingArea()
    owner.dwa_vu.set_size_request(340, 22)
    owner.dwa_vu.set_hexpand(False)
    owner.dwa_vu.set_halign(Gtk.Align.START)
    owner.dwa_vu.connect("draw", owner._draw_monitor_vu)
    level_row.pack_start(owner.dwa_vu, False, False, 0)
    frame_box.pack_start(level_row, False, False, 0)

    details_row = Gtk.Box(spacing=10)
    details_row.set_hexpand(True)
    frame_box.pack_start(details_row, False, False, 0)

    left_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    left_wrap.set_hexpand(True)
    details_row.pack_start(left_wrap, True, True, 0)

    left_grid = Gtk.Grid(row_spacing=8, column_spacing=8)
    left_grid.set_hexpand(True)
    left_wrap.pack_start(left_grid, False, False, 0)

    left_rows = [
        ("DLS :", "lbl_mon_dls"),
        ("DL+ :", "lbl_mon_dl_plus"),
        ("Title :", "lbl_mon_title"),
        ("Artist :", "lbl_mon_artist"),
    ]

    for row, (label, attr) in enumerate(left_rows, start=1):
        left_grid.attach(Gtk.Label(label=label, xalign=1), 0, row, 1, 1)
        widget = Gtk.Label(label="—", xalign=0)
        widget.set_ellipsize(3)
        widget.set_hexpand(True)
        setattr(owner, attr, widget)
        left_grid.attach(widget, 1, row, 1, 1)

    left_grid.attach(Gtk.Label(label="Slideshow:", xalign=1), 0, 5, 1, 1)
    slide_overlay = Gtk.Overlay()
    owner.output_slide_size = (160, 120)
    slide_overlay.set_size_request(*owner.output_slide_size)
    slide_overlay.set_halign(Gtk.Align.START)

    owner.img_slide = Gtk.Image()
    owner.img_slide.set_halign(Gtk.Align.CENTER)
    owner.img_slide.set_valign(Gtk.Align.CENTER)
    slide_overlay.add(owner.img_slide)

    owner.lbl_slide_placeholder = Gtk.Label()
    owner.lbl_slide_placeholder.set_use_markup(True)
    owner.lbl_slide_placeholder.set_markup("<i>Slideshows desactivate</i>")
    owner.lbl_slide_placeholder.set_halign(Gtk.Align.CENTER)
    owner.lbl_slide_placeholder.set_valign(Gtk.Align.CENTER)
    slide_overlay.add_overlay(owner.lbl_slide_placeholder)

    owner.evbox_output_slide = Gtk.EventBox()
    owner.evbox_output_slide.set_visible_window(False)
    owner.evbox_output_slide.connect("button-press-event", owner.on_output_slide_clicked)
    owner.evbox_output_slide.set_tooltip_text("Click to enlarge")
    owner.evbox_output_slide.add(slide_overlay)
    left_grid.attach(owner.evbox_output_slide, 1, 5, 1, 1)

    separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
    separator.set_vexpand(True)
    details_row.pack_start(separator, False, True, 0)

    right_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    right_wrap.set_hexpand(True)
    details_row.pack_start(right_wrap, True, True, 0)

    right_grid = Gtk.Grid(row_spacing=6, column_spacing=8)
    right_grid.set_halign(Gtk.Align.START)
    right_grid.set_hexpand(False)
    right_wrap.pack_start(right_grid, False, False, 0)

    right_rows = [
        ("Codec :", "lbl_mon_codec"),
        ("Bitrate :", "lbl_mon_bitrate"),
        ("Sample rate :", "lbl_mon_samplerate"),
        ("Channels:", "lbl_mon_channels"),
        ("Gain:", "lbl_mon_gain"),
        ("Output mode:", "lbl_mon_output_mode"),
        ("Address:", "lbl_mon_out_addr"),
        ("Port:", "lbl_mon_out_port"),
    ]
    for row, (label, attr) in enumerate(right_rows, start=1):
        right_grid.attach(Gtk.Label(label=label, xalign=1), 0, row, 1, 1)
        widget = Gtk.Label(label="—", xalign=0)
        widget.set_ellipsize(3)
        widget.set_hexpand(False)
        setattr(owner, attr, widget)
        right_grid.attach(widget, 1, row, 1, 1)

    vbox.pack_start(frame, False, False, 0)
    encoding_frame = _build_encoding_panel(owner)
    encoding_frame.set_vexpand(False)
    encoding_frame.set_valign(Gtk.Align.START)
    vbox.pack_start(encoding_frame, False, False, 0)

    _append_tab(owner, vbox, "  Output  ", scrollable=True)
