from dataclasses import dataclass, field


@dataclass
class RuntimeState:
    loop_card: int = -1
    applied_encoder_signature: tuple | None = None
    stream_station_name: str = ""
    stream_codec: str = ""
    stream_bitrate: str = ""
    stream_live_metadata_seen: bool = False
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
    last_audioenc_data_at: float = 0.0
    silence_started_at: float = 0.0
    silence_warning_active: bool = False
    silence_warning_kind: str = ""
    player_recovery_attempted: bool = False
    player_recovery_path: str = ""
    player_recovery_last_attempt_at: float = 0.0
    player_recovery_count: int = 0

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
        self.stream_live_metadata_seen = False

    def reset_silence_state(self):
        self.last_audioenc_data_at = 0.0
        self.silence_started_at = 0.0
        self.silence_warning_active = False
        self.silence_warning_kind = ""

    def reset_player_recovery(self):
        self.player_recovery_attempted = False
        self.player_recovery_path = ""
        self.player_recovery_last_attempt_at = 0.0
        self.player_recovery_count = 0
