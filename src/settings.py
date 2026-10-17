import json
import os

SETTINGS_FILE = "/settings.json"

DEFAULT_SETTINGS = {
    "WIFI_SSID": "",
    "WIFI_PASSWORD": "",
    # README and the web UI's fallback (assets/index.html) name
    # https://v6.bvg.transport.rest instead
    "API_URL": "http://vbb.a6n.de",
    "FILTERED": [],
    "WALK_DELAY": 0,
    "COLORED": True,
    "SUBWAY_COLORS": True,
    "STATION_ID": 900100003,
    "SHOW_BUS": True,
    "SHOW_TRAM": True,
    "SHOW_SUBWAY": True,
    "SHOW_REGIONAL": True,
    "SHOW_SUBURBAN": True,
    "SHOW_FERRY": True,
    "SHOW_EXPRESS": True,
    "TIMEZONE": 1,
    "NIGHT_START": "22:00",
    "NIGHT_END": "06:00",
    "NIGHT_DIMMING": 3,
    "PASSWORD_SALT": "e1b917a40fb57d7ee8feb37a626d7ddd",
    "PASSWORD_HASH": "5ab4742e8e8b4cdd1b9df66879fdad290f09a45e5a8de6ce00d5e72eb4bc8048",
}

_settings = None
_updated = False

def _load():
    """Load settings from file, merging with defaults"""
    global _settings
    try:
        with open(SETTINGS_FILE, "r") as f:
            file_settings = json.load(f)
    except:
        file_settings = {}
    _settings = {
        key: file_settings.get(key, default)
        for key, default in DEFAULT_SETTINGS.items()
    }


def _save():
    """Save current settings to file"""
    data = json.dumps(_settings, separators=(",\n", ": "))
    # put the outer braces on their own lines; a replace() on the whole
    # string would also hit braces inside values like the WiFi password
    data = "{\n" + data[1:-1] + "\n}\n"
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
    # littlefs renames atomically, so a power loss during the write can't
    # leave a truncated settings file (which would load as all defaults)
    os.rename(tmp, SETTINGS_FILE)
    print(f"Settings saved to {SETTINGS_FILE}")


@micropython.native
def get(key=None):
    """Get a setting value by key, or all settings if no key provided"""
    if _settings is None:
        _load()
    if key is None:
        return _settings.copy()
    return _settings.get(key, DEFAULT_SETTINGS.get(key))


def set(key_or_dict, value=None):
    """Set one or more settings and save to file

    Usage:
        set('WIFI_SSID', 'my_network')
        set({'WIFI_SSID': 'my_network', 'WIFI_PASSWORD': 'secret'})
    """
    global _updated
    if _settings is None:
        _load()

    if isinstance(key_or_dict, dict):
        for k, v in key_or_dict.items():
            if k in DEFAULT_SETTINGS:
                _settings[k] = v
    else:
        if key_or_dict in DEFAULT_SETTINGS:
            _settings[key_or_dict] = value

    _save()
    _updated = True

def check():
    global _updated
    if _updated:
        _updated = False
        return True
    return False