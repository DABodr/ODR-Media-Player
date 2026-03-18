import configparser
import os


def read_config_file(path):
    if not os.path.isfile(path):
        return {}, [], []

    with open(path, encoding="utf-8") as handle:
        lines = handle.readlines()

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            return _read_ini_config(path)
        break

    return _read_flat_config(lines)


def write_flat_config(path, settings, playlist):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ordered_keys = [
        "Bitrate",
        "Channels",
        "ZmqOut",
        "Silence",
        "SampleRate",
        "Codec",
        "Gain",
        "Volume",
        "DLSText",
        "ForceDefaultDLS",
        "DlsFromFile",
        "DLPlusOn",
        "SLSOn",
        "SLSTitleCard",
        "SLSCoverLocal",
        "SLSCoverOnline",
        "SLSDefaultLogo",
        "SlideDir",
        "SlideWait",
        "PadLen",
        "PlaylistAutostart",
        "EncoderAutostart",
        "Shuffle",
        "Repeat",
        "RepeatMode",
        "LocalMonitor",
        "WatchLoadedFolders",
        "LastLogoDir",
        "PlaylistFolderRoots",
        "PlaylistGroupStates",
        "PlaylistOverrides",
    ]

    with open(path, "w", encoding="utf-8") as handle:
        for key in ordered_keys:
            handle.write(f"{key}={settings.get(key, '')}\n")
        for entry in settings.get("__sls_logos__", []):
            handle.write(f"SLSLogo={entry}\n")
        for entry in playlist:
            handle.write(f"PLEntry={entry}\n")


def _read_flat_config(lines):
    settings = {}
    playlist = []
    sls_logos = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == "PLEntry":
            playlist.append(value)
        elif key == "SLSLogo":
            sls_logos.append(value)
        else:
            settings[key] = value

    return settings, playlist, sls_logos


def _read_ini_config(path):
    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")

    settings = {}
    playlist = []
    sls_logos = []

    if cfg.has_section("encodeur"):
        settings.update(cfg["encodeur"])
    if cfg.has_section("playlist"):
        for _, value in sorted(cfg["playlist"].items()):
            playlist.append(value)
    if cfg.has_section("slslogos"):
        for _, value in sorted(cfg["slslogos"].items()):
            sls_logos.append(value)

    key_map = {
        "bitrate": "Bitrate",
        "channels": "Channels",
        "zmqout": "ZmqOut",
        "silence": "Silence",
        "samplerate": "SampleRate",
        "codec": "Codec",
        "gain": "Gain",
        "volume": "Volume",
        "dlstext": "DLSText",
        "forcedefaultdls": "ForceDefaultDLS",
        "dlsfromfile": "DlsFromFile",
        "dlpluson": "DLPlusOn",
        "slson": "SLSOn",
        "slstitlecard": "SLSTitleCard",
        "slscoverlocal": "SLSCoverLocal",
        "slscoveronline": "SLSCoverOnline",
        "slsdefaultlogo": "SLSDefaultLogo",
        "slidedir": "SlideDir",
        "slidewait": "SlideWait",
        "padlen": "PadLen",
        "playlistautostart": "PlaylistAutostart",
        "encoderautostart": "EncoderAutostart",
        "shuffle": "Shuffle",
        "repeat": "Repeat",
        "repeatmode": "RepeatMode",
        "localmonitor": "LocalMonitor",
        "watchloadedfolders": "WatchLoadedFolders",
        "lastlogodir": "LastLogoDir",
        "playlistfolderroots": "PlaylistFolderRoots",
        "playlistgroupstates": "PlaylistGroupStates",
        "playlistoverrides": "PlaylistOverrides",
    }

    normalized = {}
    for key, value in settings.items():
        normalized[key_map.get(key, key)] = value

    return normalized, playlist, sls_logos
