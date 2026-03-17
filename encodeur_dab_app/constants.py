from pathlib import Path

DLS_FILE = "/tmp/dab-encodeur.dls"
PAD_ID = "dab-encodeur"
SLIDE_INPUT_DIR = f"/tmp/{PAD_ID}-slides"
SLIDE_INPUT_FILE = f"{SLIDE_INPUT_DIR}/slide.jpg"
SLIDE_DUMP = "/tmp/dab-current-slide.jpg"
CONF_FILE = str(Path.home() / ".config" / "encodeur-dab.conf")
APP_DATA_DIR = Path.home() / ".local" / "share" / "odr-fileplayer"
DEFAULT_LOGO_DIR = str(APP_DATA_DIR / "default-logos")
COVER_CACHE_DIR = str(APP_DATA_DIR / "cover-cache")
RESOURCE_DIR = Path(__file__).resolve().parents[1] / "resources"
DAB_LOGO_FILE = str(RESOURCE_DIR / "dab_logo.png")

BITRATES = [
    "8", "16", "24", "32", "40", "48", "56", "64", "72", "80", "88", "96",
    "104", "112", "120", "128", "136", "144", "152", "160", "168", "176", "184", "192",
]
PAD_LENGTHS = ["16", "23", "35", "58"]
