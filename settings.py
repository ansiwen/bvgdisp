import json

SETTINGS_FILE = '/settings.json'

DEFAULT_SETTINGS = {
    "WIFI_SSID": "",
    "WIFI_PASSWORD": "",
    "API_URL": "https://v6.bvg.transport.rest",
    "FILTERED": [],
    "WALK_DELAY": 0,
    "COLORED": True,
    "SUBWAY_COLORS": True,
    "STATION_ID": 900100003,
    "DEST_OFFSET": 23,
    "SHOW_BUS": True,
    "SHOW_TRAM": True,
    "SHOW_SUBWAY": True,
    "SHOW_REGIONAL": True,
    "SHOW_SUBURBAN": True,
    "SHOW_FERRY": True,
    "SHOW_EXPRESS": True,
    "PASSWORD_SALT": "e1b917a40fb57d7ee8feb37a626d7ddd",
    "PASSWORD_HASH": "5ab4742e8e8b4cdd1b9df66879fdad290f09a45e5a8de6ce00d5e72eb4bc8048"
}

_settings = None

def _load():
    """Load settings from file, merging with defaults"""
    global _settings
    try:
        with open(SETTINGS_FILE, 'r') as f:
            file_settings = json.load(f)
    except:
        file_settings = {}
    _settings = {key: file_settings.get(key, default) for key, default in DEFAULT_SETTINGS.items()}

def _save():
    """Save current settings to file"""
    with open(SETTINGS_FILE, 'w') as f:
        data = json.dumps(_settings, separators=(',\n', ': '))
        f.write(data.replace("{", "{\n").replace("}", "\n}\n"))
    print(f"Settings saved to {SETTINGS_FILE}")

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
