import json

SETTINGS_FILE = 'settings.json'

DEFAULTS = {
    "location_override": None,   # str or None — None means use IP detection
    "led_signals_enabled": True, # bool
    "timezone": "GMT+2",         # passed to time.timezone()
    "send_interval_s": 60,       # int, seconds; aligned to wall-clock boundaries
}

_cached = None


def load():
    """Return merged settings (file values over DEFAULTS). Caches result."""
    global _cached
    if _cached is not None:
        return _cached
    merged = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, 'r') as f:
            data = json.loads(f.read())
        for k in DEFAULTS:
            if k in data:
                merged[k] = data[k]
    except (OSError, ValueError) as e:
        print("[settings] using defaults:", e)
    _cached = merged
    return merged


def _validate(d):
    out = dict(DEFAULTS)

    # location_override: empty string -> None
    loc = d.get("location_override", None)
    out["location_override"] = (loc.strip() if isinstance(loc, str) else None) or None

    # led_signals_enabled: coerce to bool
    out["led_signals_enabled"] = bool(d.get("led_signals_enabled", True))

    # timezone: must be a non-empty string
    tz = d.get("timezone", DEFAULTS["timezone"])
    out["timezone"] = tz.strip() if isinstance(tz, str) and tz.strip() else DEFAULTS["timezone"]

    # send_interval_s: int, clamped to 10-3600
    try:
        iv = int(d.get("send_interval_s", DEFAULTS["send_interval_s"]))
        out["send_interval_s"] = max(10, min(3600, iv))
    except (ValueError, TypeError):
        out["send_interval_s"] = DEFAULTS["send_interval_s"]

    return out


def save(new_dict):
    """Validate, write SETTINGS_FILE, invalidate cache. Returns saved dict."""
    global _cached
    validated = _validate(new_dict)
    with open(SETTINGS_FILE, 'w') as f:
        f.write(json.dumps(validated))
    _cached = validated
    return validated


def get(key):
    return load().get(key, DEFAULTS.get(key))
