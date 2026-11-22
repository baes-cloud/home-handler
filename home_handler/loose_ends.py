"""Bridge to the old Loose Ends list: import items as incidents, sort them, keep data.json in step."""

import asyncio
import importlib.util
import json
import logging
import os
import shutil
from collections import Counter
from datetime import datetime

from . import brain

log = logging.getLogger("home_handler.loose_ends")
PROMPT = os.path.join(os.path.dirname(__file__), "prompts", "sort.md")
SCHEMA = {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {
    "type": "object", "required": ["ext_id", "label", "reason"], "properties": {
        "ext_id": {"type": "string"}, "label": {"enum": ["can_do", "needs_bae", "stale"]},
        "reason": {"type": "string", "maxLength": 140}}}}}}


def _dir(cfg):
    return cfg["loose_ends"]["dir"]


def store_module(cfg):
    """The real Loose Ends store.py (file-locked writes to data.json)."""
    path = os.path.join(_dir(cfg), "store.py")
    spec = importlib.util.spec_from_file_location("loose_ends_store", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _items(cfg):
    with open(os.path.join(_dir(cfg), "data.json")) as f:
        return json.load(f).get("items", {})


def sync(journal, cfg):
    """Upsert every Loose Ends item; returns how many were new."""
    new = 0
    for ext_id, it in _items(cfg).items():
        detail = {k: it.get(k) for k in ("area", "priority", "detail", "sessionId", "sessionTitle", "command", "link",
                                         "added") if it.get(k) is not None}
        status = "done" if it.get("done") else "open"
        iid, created = journal.upsert_external("loose_ends", ext_id, it.get("title", ext_id), status, detail)
        new += created
        if not created and it.get("done"):
            inc = journal.get(iid)
            if inc.status in ("open", "waiting"):
                journal.set_status(iid, "done", it.get("doneNote") or "ticked in Loose Ends", it.get("doneBy") or "you")
    return new


async def sync_forever(ctx, every_s=3600):
    while True:
        try:
            n = await asyncio.to_thread(sync, ctx.journal, ctx.cfg)
            if n:
                log.info("imported %d new Loose Ends items", n)
        except Exception:
            log.exception("loose ends sync failed")
        await asyncio.sleep(every_s)


def _ext_incident(journal, ext_id):
    for inc in journal.incidents(source="loose_ends"):
        if inc.ext_id == ext_id:
            return inc
    return None


def sort_open(journal, cfg, dry_run=False, ask=brain.ask_json, store=None):
    """One tool-less Claude call labels every open, unsorted item. Returns counts per label."""
    todo = [i for i in journal.incidents(status=("open", "waiting"), source="loose_ends") if not i.sort_label]
    if not todo:
        return {}
    items = [{"ext_id": i.ext_id, "title": i.title, "area": i.detail.get("area"),
              "priority": i.detail.get("priority"), "added": i.detail.get("added"),
              "detail": (i.detail.get("detail") or "")[:500]} for i in todo]
    with open(PROMPT) as f:
        prompt = f.read() + "\n\nItems (JSON):\n" + json.dumps(items, ensure_ascii=False)
    reply = ask(prompt, SCHEMA, "triage", journal, cfg)
    if not reply:
        return {}
    by_ext = {i.ext_id: i for i in todo}
    items = [r for r in reply.get("items", [])
             if r.get("ext_id") in by_ext and r.get("label") in ("can_do", "needs_bae", "stale")]
    if dry_run:
        # the preview is saved, and --apply applies exactly this (Claude's answer can differ on a second ask)
        journal.kv_set("loose_sort_proposal", {"items": items})
        for r in items:
            print(f"{r['label']:10} {by_ext[r['ext_id']].title[:70]} | {r.get('reason', '')}")
        return dict(Counter(r["label"] for r in items))
    return _apply(journal, cfg, items, store or store_module(cfg))


def _apply(journal, cfg, items, store):
    counts = Counter()
    by_ext = {i.ext_id: i for i in journal.incidents(source="loose_ends")}
    for r in items:
        inc = by_ext.get(r["ext_id"])
        if not inc or inc.status not in ("open", "waiting"):
            continue
        counts[r["label"]] += 1
        journal.set_sort(inc.id, r["label"], r.get("reason", ""))
        if r["label"] == "stale":
            note = f"closed by house agent: {r.get('reason', '')}"
            journal.set_status(inc.id, "closed", note, "agent")
            store.resolve(inc.ext_id, note)
    return dict(counts)


def apply_proposal(journal, cfg, store=None):
    """Apply the saved --dry-run preview exactly, after backing up data.json. None if there is no preview."""
    prop = journal.kv_get("loose_sort_proposal")
    if not prop:
        return None
    src = os.path.join(_dir(cfg), "data.json")
    shutil.copy2(src, f"{src}.bak-home-handler-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    counts = _apply(journal, cfg, prop["items"], store or store_module(cfg))
    journal.kv_set("loose_sort_proposal", None)
    return counts


def mark_done(cfg, journal, incident_id, by="you", store=None):
    inc = journal.get(incident_id)
    journal.set_status(incident_id, "done", "ticked on the agent page", by)
    if inc.source == "loose_ends":
        (store or store_module(cfg)).set_done(inc.ext_id, True, by)


def reopen(cfg, journal, incident_id, store=None):
    inc = journal.get(incident_id)
    journal.set_status(incident_id, "open", "reopened on the agent page", "you")
    if inc.source == "loose_ends":
        (store or store_module(cfg)).set_done(inc.ext_id, False, "you")
