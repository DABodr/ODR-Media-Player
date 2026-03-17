import re


def sanitize_broadcast_metadata(artist, title):
    return _sanitize_broadcast_label(artist), _sanitize_broadcast_label(title)


def _sanitize_broadcast_label(value):
    value = (value or "").strip()
    value = re.sub(r"^\s*\d+[\W_]+", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -_")


def build_dls_content(default_text, use_file_metadata, use_dl_plus, artist, title):
    artist, title = sanitize_broadcast_metadata(artist, title)

    if not use_file_metadata or not (artist or title):
        return (default_text or "") + "\n"

    if use_dl_plus and artist and title:
        separator = " - "
        payload = f"{artist}{separator}{title}"
        artist_len = len(artist)
        title_start = artist_len + len(separator)
        title_len = len(title)
        lines = [
            "##### parameters { #####",
            "DL_PLUS=1",
            "DL_PLUS_ITEM_TOGGLE=0",
            "DL_PLUS_ITEM_RUNNING=1",
            f"DL_PLUS_TAG=4 0 {artist_len}",
            f"DL_PLUS_TAG=1 {title_start} {title_len}",
            "##### parameters } #####",
            payload,
        ]
        return "\n".join(lines) + "\n"

    if artist and title:
        return f"{artist} - {title}\n"
    if title:
        return f"{title}\n"
    return f"{artist}\n"
