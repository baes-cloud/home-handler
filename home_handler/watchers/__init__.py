"""Watcher registry. Each module appends its class to REGISTRY on import."""

REGISTRY = []


def load_all():
    from . import watchdog  # noqa: F401
    for mod in ("space", "devices", "ha_health", "quiet"):
        try:
            __import__(f"{__name__}.{mod}")
        except ModuleNotFoundError as e:
            if e.name != f"{__name__}.{mod}":
                raise
    return list(REGISTRY)
