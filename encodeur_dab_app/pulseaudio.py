import json
import subprocess


CAPTURE_SINK_NAME = "odr_fileplayer_capture"
CAPTURE_SINK_DESCRIPTION = "ODR_FilePlayer_Capture"


def list_audio_applications():
    items = _pactl_json(["list", "sink-inputs"])
    if not isinstance(items, list):
        return []

    apps = []
    for item in items:
        properties = item.get("properties") or {}
        app_name = str(properties.get("application.name", "") or "").strip()
        media_name = str(properties.get("media.name", "") or "").strip()
        process_id = str(properties.get("application.process.id", "") or "").strip()
        process_binary = str(properties.get("application.process.binary", "") or "").strip()
        sink_index = int(item.get("sink") or 0)
        sink_input_index = int(item.get("index") or 0)

        if sink_input_index <= 0:
            continue
        if app_name.lower() in {"speech-dispatcher-dummy", "odr-fileplayer"}:
            continue

        apps.append(
            {
                "index": sink_input_index,
                "sink_index": sink_index,
                "app_name": app_name or "Unknown application",
                "media_name": media_name,
                "process_id": process_id,
                "process_binary": process_binary,
            }
        )
    return apps


def list_audio_inputs():
    items = _pactl_json(["list", "sources"])
    if not isinstance(items, list):
        return []

    sources = []
    for item in items:
        properties = item.get("properties") or {}
        name = str(item.get("name", "") or "").strip()
        if not name:
            continue

        device_class = str(properties.get("device.class", "") or "").strip().lower()
        if device_class == "monitor" or name.endswith(".monitor"):
            continue

        card_name = str(properties.get("alsa.card_name", "") or "").strip()
        if card_name.lower() == "loopback" or "snd_aloop" in name:
            continue

        description = str(
            properties.get("device.description")
            or item.get("description")
            or ""
        ).strip()
        if not description or description == "(null)":
            description = card_name or name

        active_port = item.get("active_port") or ""
        port_name = ""
        if isinstance(active_port, dict):
            port_name = str(active_port.get("description", "") or active_port.get("name", "") or "").strip()
        elif active_port:
            port_name = str(active_port).strip()
        if port_name and port_name != "(null)" and port_name not in description:
            description = f"{description} — {port_name}"

        state = str(item.get("state", "") or "").strip().title() or "Unknown"
        sample_spec = str(item.get("sample_specification", "") or "").strip()
        sources.append(
            {
                "name": name,
                "description": description,
                "card_name": card_name or description,
                "state": state,
                "sample_specification": sample_spec,
            }
        )

    sources.sort(key=lambda item: (item["description"].casefold(), item["name"].casefold()))
    return sources


def ensure_capture_sink():
    sinks = _pactl_json(["list", "short", "sinks"])
    if isinstance(sinks, list):
        for sink in sinks:
            if str(sink.get("name", "") or "") == CAPTURE_SINK_NAME:
                return CAPTURE_SINK_NAME

    try:
        _pactl(
            [
                "load-module",
                "module-null-sink",
                f"sink_name={CAPTURE_SINK_NAME}",
                f"sink_properties=device.description={CAPTURE_SINK_DESCRIPTION}",
            ]
        )
    except Exception:
        _pactl(
            [
                "load-module",
                "module-null-sink",
                f"sink_name={CAPTURE_SINK_NAME}",
            ]
        )
    return CAPTURE_SINK_NAME


def capture_monitor_source_name():
    ensure_capture_sink()
    return f"{CAPTURE_SINK_NAME}.monitor"


def route_app_to_capture(sink_input_index):
    sink_name = ensure_capture_sink()
    default_sink = default_sink_name()
    capture_sink_index = sink_index_by_name(sink_name)

    if capture_sink_index is not None and default_sink:
        for item in list_audio_applications():
            if item["index"] == sink_input_index:
                continue
            if item["sink_index"] == capture_sink_index:
                _pactl(["move-sink-input", str(item["index"]), default_sink])

    _pactl(["move-sink-input", str(sink_input_index), sink_name])
    return capture_monitor_source_name()


def current_captured_app_info():
    sink_name = ensure_capture_sink()
    capture_sink_index = sink_index_by_name(sink_name)
    if capture_sink_index is None:
        return {}

    for item in list_audio_applications():
        if item["sink_index"] == capture_sink_index:
            return item
    return {}


def default_sink_name():
    info = _pactl_json(["info"])
    if not isinstance(info, dict):
        return ""
    return str(info.get("default_sink_name", "") or "").strip()


def sink_index_by_name(name):
    sinks = _pactl_json(["list", "short", "sinks"])
    if not isinstance(sinks, list):
        return None
    for sink in sinks:
        if str(sink.get("name", "") or "") == name:
            try:
                return int(sink.get("index"))
            except (TypeError, ValueError):
                return None
    return None


def _pactl_json(args):
    output = _pactl(["--format=json", *args])
    try:
        return json.loads(output)
    except Exception:
        return None


def _pactl(args):
    result = subprocess.run(
        ["pactl", *args],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "pactl failed").strip())
    return result.stdout
