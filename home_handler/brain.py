"""Tool-less Claude calls with structured output, counted against the daily cap."""

import json
import logging
import os
import shutil
import subprocess

from . import CONFIG_DIR, DATA_DIR

log = logging.getLogger("home_handler.brain")
ENV_FILE = os.path.join(CONFIG_DIR, "claude.env")
CWD = os.path.join(DATA_DIR, "claude-cwd")  # empty dir: no project CLAUDE.md or memory gets pulled in


def claude_bin():
    """Absolute path to the Claude CLI: systemd's PATH after a reboot may not include ~/.local/bin."""
    local = os.path.expanduser("~/.local/bin/claude")
    return local if os.path.exists(local) else (shutil.which("claude") or local)


def _env():
    env = dict(os.environ)
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"')
    except FileNotFoundError:
        pass
    return env


def _parse(out):
    d = json.loads(out)
    if d.get("is_error"):
        raise RuntimeError(str(d.get("result"))[:300])
    so = d.get("structured_output")
    if isinstance(so, dict):
        return so
    res = d.get("result")
    if isinstance(res, str):
        s = res.strip()
        if s.startswith("```"):
            s = s.strip("`").split("\n", 1)[-1]
        return json.loads(s[s.find("{"): s.rfind("}") + 1])
    raise RuntimeError("no structured output")


def ask_json(prompt, schema, kind, journal, cfg, runner=subprocess.run, timeout=600):
    """Return the structured reply as a dict, or None (cap reached, CLI error, timeout, bad JSON)."""
    cap = cfg["caps"].get(kind, 0)
    if journal.runs_today(kind) >= cap:
        log.info("cap reached for %s (%d)", kind, cap)
        return None
    cmd = [claude_bin(), "-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
           "--output-format", "json", "--json-schema", json.dumps(schema), prompt]
    os.makedirs(CWD, exist_ok=True)
    try:
        p = runner(cmd, capture_output=True, text=True, timeout=timeout, env=_env(), cwd=CWD)
        if p.returncode != 0 and not p.stdout:
            raise RuntimeError((p.stderr or "claude exited %d" % p.returncode)[:300])
        result = _parse(p.stdout)
        journal.record_run(kind, True, {"chars": len(p.stdout)})
        return result
    except subprocess.TimeoutExpired:
        journal.record_run(kind, False, {"error": "timeout"})
    except Exception as e:
        log.warning("claude run failed: %s", e)
        journal.record_run(kind, False, {"error": str(e)[:300]})
    return None
