"""Commands typed into the phone widget's terminal line.

Fixed commands map to things the agent already does. Anything else is a free-text question for the
brain, answered read-only from the agent's own state (no tools, counted against the daily "ask" cap).
"""

import json
import os
import time

from . import brain, loose_ends

PROMPT = os.path.join(os.path.dirname(__file__), "prompts", "ask.md")
SCHEMA = {"type": "object", "properties": {"lines": {"type": "array", "items": {"type": "string"}}},
          "required": ["lines"]}
HELP = ["status · waiting · spotted · done <id> · reopen <id> · help",
        "anything else is a question for the agent"]


def _line(i):
    return f"#{i.id} {i.title}"[:90]


def run(text, journal, cfg, store=None, ask=brain.ask_json, state_fn=None):
    """Returns {"lines": [...], "kind": "fixed" | "ask" | "error"}."""
    text = (text or "").strip()
    if not text:
        return {"lines": HELP, "kind": "fixed"}
    words = text.split()
    cmd, args = words[0].lower(), words[1:]
    active = journal.incidents(status=("open", "waiting"))
    waiting = [i for i in active if i.status == "waiting" or i.sort_label == "needs_bae"]
    spotted = sorted([i for i in active if i.source == "watcher"], key=lambda i: (-i.severity, -i.last_seen))

    if cmd in ("help", "?", "commands"):
        return {"lines": HELP, "kind": "fixed"}
    if cmd == "status" and not args:
        loose = sum(1 for i in active if i.source == "loose_ends")
        out = [f"waiting on you {len(waiting)} · spotted {len(spotted)} · loose ends {loose}"]
        s = journal.latest_summary()
        if s and s.get("headline"):
            out.append(str(s["headline"])[:90])
        return {"lines": out, "kind": "fixed"}
    if cmd == "waiting" and not args:
        return {"lines": [_line(i) for i in waiting[:5]] or ["nothing waiting on you"], "kind": "fixed"}
    if cmd == "spotted" and not args:
        return {"lines": [_line(i) for i in spotted[:5]] or ["nothing spotted"], "kind": "fixed"}
    if cmd in ("done", "reopen") and len(args) == 1:
        try:
            iid = int(args[0].lstrip("#"))
            inc = journal.get(iid)
        except (ValueError, TypeError):
            return {"lines": [f"no item {args[0]}"], "kind": "error"}
        if cmd == "done":
            loose_ends.mark_done(cfg, journal, iid, by="you", store=store)
        else:
            loose_ends.reopen(cfg, journal, iid, store=store)
        return {"lines": [f"{cmd} #{iid} {inc.title}"[:90]], "kind": "fixed"}

    # Free text: a read-only question for the brain.
    with open(PROMPT) as f:
        prompt = f.read()
    snap = state_fn(journal) if state_fn else {}
    snap = {k: snap.get(k) for k in ("counts", "waiting", "open", "today", "watchers") if k in snap}
    prompt += "\n\nSnapshot:\n" + json.dumps(snap, default=str)[:20000] + "\n\nQuestion: " + text[:500]
    res = ask(prompt, SCHEMA, "ask", journal, cfg, timeout=120)
    if not res or not res.get("lines"):
        return {"lines": ["no answer (daily question cap reached, or the brain is offline)"], "kind": "error"}
    return {"lines": [str(x)[:120] for x in res["lines"][:3]], "kind": "ask"}
