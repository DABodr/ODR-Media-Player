from dataclasses import dataclass
import fcntl
import os
import re
import shlex
from urllib.parse import urlparse

from .constants import DLS_FILE, PAD_ID, SLIDE_DUMP, SLIDE_INPUT_DIR


@dataclass
class EncoderOptions:
    loop_card: int
    codec_index: int
    channels_index: int
    bitrate: str
    samplerate_text: str
    gain: int
    silence: int
    zmq_out: str
    pad_len: str
    default_dls_text: str
    force_default_dls: bool
    dls_from_file: bool
    dl_plus: bool
    sls_enabled: bool
    sls_title_card: bool
    sls_default_logo: bool
    slide_dir: str
    slide_wait: int


_VU_RE = re.compile(r"In: \[([^|]*)\|([^\]]*)\]")


def effective_dls_from_file(options):
    return options.dls_from_file and not options.force_default_dls


def effective_dl_plus(options):
    return options.dl_plus and effective_dls_from_file(options)


def use_pad(options):
    return (
        bool(options.default_dls_text.strip())
        or effective_dls_from_file(options)
        or effective_dl_plus(options)
        or options.sls_enabled
    )


def build_audio_cmd(options):
    parts = ["odr-audioenc"]
    if options.codec_index == 1:
        parts.append("--sbr")
    elif options.codec_index == 2:
        parts.extend(["--sbr", "--ps"])

    output_flag = "-e" if _is_edi_uri(options.zmq_out) else "-o"

    parts.extend(
        [
            "-l",
            "-d",
            # Lock capture to snd-aloop subdevice 0 so it always matches the
            # playback side of the same loopback pair on systems with multiple
            # loopback substreams, notably Raspberry Pi.
            f"plughw:{options.loop_card},1,0",
            "-D",
            "-c",
            "1" if options.channels_index == 0 else "2",
            "-b",
            options.bitrate,
            "-r",
            (options.samplerate_text or "48000 Hz")[:5],
            "-g",
            str(int(options.gain)),
            output_flag,
            options.zmq_out,
        ]
    )

    if use_pad(options):
        parts.extend(["-p", options.pad_len or "58", "-P", PAD_ID])

    return " ".join(shlex.quote(part) for part in parts)


def build_pad_cmd(options):
    parts = ["odr-padenc", "-t", DLS_FILE, "-o", PAD_ID]
    if options.sls_enabled and (
        options.slide_dir.strip() or options.sls_title_card or options.sls_default_logo
    ):
        parts.extend(
            [
                "-d",
                SLIDE_INPUT_DIR,
                "-s",
                str(int(options.slide_wait)),
                f"--dump-current-slide={SLIDE_DUMP}",
            ]
        )
    return " ".join(shlex.quote(part) for part in parts)


def set_nonblocking(stream):
    fd = stream.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def parse_audioenc_chunk(text):
    vu = None
    matches = list(_VU_RE.finditer(text))
    if matches:
        vu = _compute_vu(matches[-1].group(1), matches[-1].group(2))

    log_lines = []
    for line in text.splitlines():
        line = line.strip()
        if line and "In: [" not in line and "ODR-PadEnc" not in line:
            log_lines.append(line)

    return vu, log_lines


def decode_exit_status(status):
    return os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1


def is_running(process):
    return bool(process and process.poll() is None)


def codec_label(codec_index):
    labels = ["AAC-LC", "HE-AAC v1 (SBR)", "HE-AAC v2 (SBR + PS)"]
    if 0 <= codec_index < len(labels):
        return labels[codec_index]
    return "—"


def channels_label(channels_index):
    labels = ["Mono (1)", "Stereo (2)"]
    if 0 <= channels_index < len(labels):
        return labels[channels_index]
    return "—"


def output_endpoint_parts(zmq_out):
    try:
        parsed = urlparse(zmq_out or "")
    except Exception:
        parsed = None

    if parsed and parsed.hostname:
        host = parsed.hostname
        try:
            port = str(parsed.port) if parsed.port is not None else "—"
        except ValueError:
            port = "—"
        return host, port

    return (zmq_out or "—"), "—"


def _compute_vu(left_bar, right_bar):
    left = _bar_percent(left_bar)
    right = _bar_percent(right_bar)
    return left, right


def _bar_percent(bar):
    if not bar:
        return 0
    active = sum(1 for char in bar if char in "=-")
    return int(active * 100 / len(bar))


def _is_edi_uri(uri):
    try:
        return (urlparse(uri or "").scheme or "").lower() == "udp"
    except Exception:
        return False
