import copy
import os
import tomllib

from . import CONFIG_DIR

DEFAULT_PATH = os.path.join(CONFIG_DIR, "config.toml")

DEFAULTS = {
    "caps": {"triage": 12, "job": 4, "evening": 1, "ask": 30},
    "ha": {
        "ws_url": "ws://homeassistant.local:8123/api/websocket",
        "rest_url": "http://homeassistant.local:8123",
        "token_path": os.path.join(CONFIG_DIR, "ha_token"),
    },
    "space": {
        "floor_gb_nas": 50, "floor_gb_host": 10, "ha_disk_pct_ceiling": 95,
        "urgent_days": 7, "warn_days": 30, "window_days": 7, "min_points": 12,
        "ma_mb_max": 3000, "ma_mb_per_day": 500, "ma_every_s": 900,
    },
    "devices": {
        "min_episode_s": 120, "episodes_7d": 3, "long_episode_s": 21600,
        "long_dead_days": 7, "battery_floor": 15, "battery_days": 14,
    },
    "ha_health": {"blips_24h": 2, "restart_slow_s": 210, "entry_retry_s": 1800},
    "quiet": {"overdue_factor_urgent": 3},
    "watchdog": {"status_max_age_s": 900, "errors_per_hour": 3},
    "evening": {"start": "18:45", "last": "23:00", "page_url": "https://agent.example.com/#today"},
    "loose_ends": {"dir": os.path.expanduser("~/loose-ends")},
}


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(path=None):
    cfg = copy.deepcopy(DEFAULTS)
    path = path or DEFAULT_PATH
    try:
        with open(path, "rb") as f:
            _merge(cfg, tomllib.load(f))
    except FileNotFoundError:
        pass
    return cfg
