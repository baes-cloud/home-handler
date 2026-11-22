"""The evening note: one push and a page section, only when something happened."""

import asyncio
import json
import os
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from . import TZ, brain, notify

PROMPT = os.path.join(os.path.dirname(__file__), "prompts", "evening.md")
SCHEMA = {"type": "object", "required": ["push", "headline", "sections"], "properties": {
    "push": {"type": "string", "maxLength": 180}, "headline": {"type": "string"},
    "sections": {"type": "array", "items": {"type": "object", "required": ["title", "items"], "properties": {
        "title": {"type": "string"}, "items": {"type": "array", "items": {"type": "string"}}}}}}}
SEV = {0: "info", 1: "notice", 2: "warn", 3: "urgent"}


def _clean(s):
    return s.replace(" — ", ", ").replace("—", ", ").replace("–", "-") if isinstance(s, str) else s


def _scrub(payload):
    return {"push": _clean(payload.get("push", ""))[:180], "headline": _clean(payload.get("headline", "")),
            "sections": [{"title": _clean(s.get("title", "")), "items": [_clean(x) for x in s.get("items", [])]}
                         for s in payload.get("sections", [])]}


def news(ctx, since):
    """Watcher incidents not yet reported, or worse than when last reported. Standing conditions stay on the page."""
    reported = ctx.journal.kv_get("evening_reported", {})
    return [i for i in ctx.journal.incidents(since=since) if i.source == "watcher"
            and (str(i.id) not in reported or i.severity > reported[str(i.id)])]


def digest(ctx, since, incs):
    le_open = ctx.journal.incidents(status=("open", "waiting"), source="loose_ends")
    return {
        "incidents": [{"watcher": i.watcher, "title": i.title, "severity": SEV.get(i.severity), "times": i.count,
                       "status": i.status, "new_today": i.first_seen >= since,
                       "detail": {k: v for k, v in i.detail.items() if k not in ("outages", "devices")}}
                      for i in sorted(incs, key=lambda i: -i.severity)][:40],
        "loose_ends": {"open": len(le_open), **Counter(i.sort_label or "unsorted" for i in le_open)},
        "watchers_unwell": [s["name"] for s in ctx.journal.watcher_stats() if s.get("errors")],
    }


def fallback(d):
    items = [f"{x['title']} ({x['severity']})" for x in d["incidents"]]
    n = len(items)
    return {"push": f"House notes: {n} thing{'s' if n != 1 else ''} today. {items[0] if items else ''}"[:180],
            "headline": f"{n} thing{'s' if n != 1 else ''} today (plain version)",
            "sections": [{"title": "Spotted", "items": items[:20]},
                         {"title": "Note", "items": ["Claude was unavailable tonight, so this is the plain version."]}]}


async def run(ctx, day, ask=brain.ask_json):
    if ctx.journal.get_summary(day):
        return "done"
    since = ctx.journal.last_summary_ts() or (ctx.now() - 86400)
    incs = news(ctx, since)
    if not any(i.severity >= 2 for i in incs):  # notices alone never earn a push
        return "quiet"
    try:
        party = ctx.ha.rest("GET", "/api/states/input_boolean.auto_stay").get("state") == "on"
    except Exception:
        return "held"  # HA unreachable: a push couldn't go out anyway, so try again in 30 min
    if party:
        return "held"
    d = digest(ctx, since, incs)
    with open(PROMPT) as f:
        prompt = f.read() + "\n\nDigest (JSON):\n" + json.dumps(d, ensure_ascii=False, default=str)
    reply = await asyncio.to_thread(ask, prompt, SCHEMA, "evening", ctx.journal, ctx.cfg)
    status = "sent" if reply else "fallback"
    payload = _scrub(reply or fallback(d))
    payload["status"] = status
    if not notify.push(ctx.ha, payload["push"], title="House notes", url=ctx.cfg["evening"]["page_url"]):
        return "push_failed"  # nothing saved, so the next timer run retries
    ctx.journal.save_summary(day, payload, ts=ctx.now())
    reported = ctx.journal.kv_get("evening_reported", {})
    reported.update({str(i.id): i.severity for i in incs})
    ctx.journal.kv_set("evening_reported", reported)
    return status


async def main(ctx, force=False):
    day = datetime.fromtimestamp(ctx.now(), ZoneInfo(TZ)).strftime("%Y-%m-%d")
    last = ctx.cfg["evening"]["last"]
    now_hm = datetime.fromtimestamp(ctx.now(), ZoneInfo(TZ)).strftime("%H:%M")
    if not force and now_hm > last:
        return "late"
    return await run(ctx, day)
