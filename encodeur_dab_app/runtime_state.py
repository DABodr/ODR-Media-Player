from dataclasses import dataclass, field


@dataclass
class RuntimeState:
    loop_card: int = -1
    applied_encoder_signature: tuple | None = None
    stream_station_name: str = ""
    stream_codec: str = ""
    stream_bitrate: str = ""
    player_vu_left: int = 0
    player_vu_right: int = 0
    monitor_vu_left: int = 0
    monitor_vu_right: int = 0
    slide_mtime: float = 0
    slide_paths: list[str] = field(default_factory=list)
    slide_rotation_started_at: float = 0.0
    slide_wait_seconds: int = 10
    slide_preview_override_path: str = ""
    slide_preview_override_until: float = 0.0
    proc_player: object | None = None
    player_bus: object | None = None
    player_bus_handlers: tuple = ()
    proc_audioenc: object | None = None
    proc_padenc: object | None = None
    audio_crash: bool = False
    pad_crash: bool = False
    stopping_audio: bool = False
    stopping_pad: bool = False
    restart_pending: bool = False

    def reset_player_vu(self):
        self.player_vu_left = 0
        self.player_vu_right = 0

    def reset_monitor_vu(self):
        self.monitor_vu_left = 0
        self.monitor_vu_right = 0

    def reset_stream_metadata(self):
        self.stream_station_name = ""
        self.stream_codec = ""
        self.stream_bitrate = ""
