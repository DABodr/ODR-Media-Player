import json
from dataclasses import dataclass, field


DEFAULT_DLS_TEXT = "DABcast — Live"


@dataclass
class AppConfig:
    bitrate: str = "128"
    channels: int = 1
    zmq_out: str = "tcp://localhost:9000"
    silence: int = 180
    sample_rate: int = 1
    codec: int = 1
    gain: int = 0
    volume: int = 100
    dls_text: str = DEFAULT_DLS_TEXT
    force_default_dls: bool = False
    dls_from_file: bool = False
    dl_plus_on: bool = False
    sls_on: bool = False
    sls_title_card: bool = False
    sls_cover_local: bool = True
    sls_cover_online: bool = False
    sls_default_logo: bool = True
    slide_dir: str = ""
    sls_logos: list[str] = field(default_factory=list)
    slide_wait: int = 10
    pad_len: str = "58"
    playlist_autostart: bool = False
    encoder_autostart: bool = False
    shuffle: bool = False
    repeat: bool = False
    local_monitor: bool = False
    last_logo_dir: str = ""
    playlist: list[str] = field(default_factory=list)
    playlist_overrides: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def from_storage(cls, settings, playlist, sls_logos=None):
        return cls(
            bitrate=settings.get("Bitrate", "128"),
            channels=_as_int(settings.get("Channels"), 1),
            zmq_out=settings.get("ZmqOut", "tcp://localhost:9000"),
            silence=_as_int(settings.get("Silence"), 180),
            sample_rate=_as_int(settings.get("SampleRate"), 1),
            codec=_as_int(settings.get("Codec"), 1),
            gain=_as_int(settings.get("Gain"), 0),
            volume=_as_int(settings.get("Volume"), 100),
            dls_text=settings.get("DLSText", DEFAULT_DLS_TEXT),
            force_default_dls=settings.get("ForceDefaultDLS", "0") == "1",
            dls_from_file=settings.get("DlsFromFile", "0") == "1",
            dl_plus_on=settings.get("DLPlusOn", "0") == "1",
            sls_on=settings.get("SLSOn", "0") == "1",
            sls_title_card=settings.get("SLSTitleCard", "0") == "1",
            sls_cover_local=settings.get("SLSCoverLocal", "1") == "1",
            sls_cover_online=settings.get("SLSCoverOnline", "0") == "1",
            sls_default_logo=settings.get("SLSDefaultLogo", "1") == "1",
            slide_dir=settings.get("SlideDir", ""),
            sls_logos=list(sls_logos or ()),
            slide_wait=_as_int(settings.get("SlideWait"), 10),
            pad_len=settings.get("PadLen", "58"),
            playlist_autostart=settings.get("PlaylistAutostart", "0") == "1",
            encoder_autostart=settings.get("EncoderAutostart", "0") == "1",
            shuffle=settings.get("Shuffle", "0") == "1",
            repeat=settings.get("Repeat", "0") == "1",
            local_monitor=settings.get("LocalMonitor", "0") == "1",
            last_logo_dir=settings.get("LastLogoDir", ""),
            playlist=list(playlist),
            playlist_overrides=_load_playlist_overrides(settings.get("PlaylistOverrides", "")),
        )

    def to_storage(self):
        settings = {
            "Bitrate": self.bitrate,
            "Channels": str(self.channels),
            "ZmqOut": self.zmq_out,
            "Silence": str(self.silence),
            "SampleRate": str(self.sample_rate),
            "Codec": str(self.codec),
            "Gain": str(self.gain),
            "Volume": str(self.volume),
            "DLSText": self.dls_text,
            "ForceDefaultDLS": "1" if self.force_default_dls else "0",
            "DlsFromFile": "1" if self.dls_from_file else "0",
            "DLPlusOn": "1" if self.dl_plus_on else "0",
            "SLSOn": "1" if self.sls_on else "0",
            "SLSTitleCard": "1" if self.sls_title_card else "0",
            "SLSCoverLocal": "1" if self.sls_cover_local else "0",
            "SLSCoverOnline": "1" if self.sls_cover_online else "0",
            "SLSDefaultLogo": "1" if self.sls_default_logo else "0",
            "SlideDir": "",
            "SlideWait": str(self.slide_wait),
            "PadLen": self.pad_len,
            "PlaylistAutostart": "1" if self.playlist_autostart else "0",
            "EncoderAutostart": "1" if self.encoder_autostart else "0",
            "Shuffle": "1" if self.shuffle else "0",
            "Repeat": "1" if self.repeat else "0",
            "LocalMonitor": "1" if self.local_monitor else "0",
            "LastLogoDir": self.last_logo_dir,
            "PlaylistOverrides": _dump_playlist_overrides(self.playlist_overrides),
        }
        return settings, list(self.playlist), list(self.sls_logos)


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_playlist_overrides(value):
    payload = (value or "").strip()
    if not payload:
        return {}
    try:
        data = json.loads(payload)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    normalized = {}
    for key, item in data.items():
        if not isinstance(item, dict):
            continue
        normalized[str(key)] = {
            "artist": str(item.get("artist", "") or ""),
            "title": str(item.get("title", "") or ""),
            "album": str(item.get("album", "") or ""),
        }
    return normalized


def _dump_playlist_overrides(value):
    if not value:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return ""
