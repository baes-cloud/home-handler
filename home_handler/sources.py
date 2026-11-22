import json
import os
import subprocess
from datetime import datetime

STATUS_PATH = os.path.expanduser(os.environ.get("HOME_HANDLER_STATUS", "~/homelab-status.json"))


def read_status(path, now):
    """Return (data, age_seconds) for the collector's status.json, or (None, None). Never raises."""
    try:
        with open(path) as f:
            data = json.load(f)
        gen = datetime.fromisoformat(data["generated"]).timestamp()
        return data, now - gen
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


def ssh(host, cmd, timeout=30):
    """Run cmd on host over ssh (BatchMode). Returns (rc, stdout, stderr); never raises."""
    try:
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, min(5, int(timeout)))}", host, cmd],
            capture_output=True, text=True, timeout=timeout + 2)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as e:
        return 127, "", str(e)
