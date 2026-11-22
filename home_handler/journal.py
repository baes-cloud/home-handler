import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import TZ
from .models import Incident, Observation

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, watcher TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'watcher', ext_id TEXT, title TEXT NOT NULL,
  severity INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open', kind TEXT,
  sort_label TEXT, sort_reason TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
  count INTEGER NOT NULL DEFAULT 1, detail TEXT NOT NULL DEFAULT '{}', notes TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS inc_fp ON incidents(fingerprint, status);
CREATE UNIQUE INDEX IF NOT EXISTS inc_ext ON incidents(source, ext_id) WHERE ext_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY, incident_id INTEGER, watcher TEXT, fingerprint TEXT,
  severity INTEGER, title TEXT, detail TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS samples (key TEXT, ts REAL, value REAL);
CREATE INDEX IF NOT EXISTS samples_k ON samples(key, ts);
CREATE TABLE IF NOT EXISTS watcher_runs (name TEXT, ts REAL, ok INTEGER, fired INTEGER, error TEXT);
CREATE INDEX IF NOT EXISTS wr_name ON watcher_runs(name, ts);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, kind TEXT, ts REAL, ok INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS summaries (day TEXT PRIMARY KEY, ts REAL, payload TEXT);
CREATE TABLE IF NOT EXISTS replay_findings (watcher TEXT, fingerprint TEXT, title TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

ACTIVE = ("open", "waiting")


class Journal:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    # incidents -------------------------------------------------------
    def _row(self, r):
        d = dict(r)
        d["detail"] = json.loads(d["detail"])
        d["notes"] = json.loads(d["notes"])
        return Incident(**d)

    def add_observation(self, o: Observation) -> int:
        with self.lock:
            self.db.execute(
                "INSERT INTO observations(watcher,fingerprint,severity,title,detail,ts) VALUES(?,?,?,?,?,?)",
                (o.watcher, o.fingerprint, o.severity, o.title, json.dumps(o.detail), o.ts))
            oid = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
            r = self.db.execute(
                "SELECT id, severity FROM incidents WHERE fingerprint=? AND status IN ('open','waiting') "
                "ORDER BY id DESC LIMIT 1", (o.fingerprint,)).fetchone()
            if r:
                iid = r["id"]
                self.db.execute(
                    "UPDATE incidents SET count=count+1, last_seen=?, severity=MAX(severity,?), title=?, detail=? WHERE id=?",
                    (o.ts, o.severity, o.title, json.dumps(o.detail), iid))
            else:
                self.db.execute(
                    "INSERT INTO incidents(fingerprint,watcher,title,severity,first_seen,last_seen,detail) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (o.fingerprint, o.watcher, o.title, o.severity, o.ts, o.ts, json.dumps(o.detail)))
                iid = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
            self.db.execute("UPDATE observations SET incident_id=? WHERE id=?", (iid, oid))
            return iid

    def get(self, incident_id) -> Incident:
        return self._row(self.db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone())

    def incidents(self, status=None, since=None, source=None, limit=None):
        q, a = "SELECT * FROM incidents WHERE 1=1", []
        if status:
            st = [status] if isinstance(status, str) else list(status)
            q += " AND status IN (%s)" % ",".join("?" * len(st))
            a += st
        if since is not None:
            q += " AND last_seen >= ?"
            a.append(since)
        if source:
            q += " AND source=?"
            a.append(source)
        q += " ORDER BY last_seen DESC"
        if limit:
            q += " LIMIT %d" % int(limit)
        return [self._row(r) for r in self.db.execute(q, a)]

    def find_active(self, fingerprint):
        r = self.db.execute("SELECT * FROM incidents WHERE fingerprint=? AND status IN ('open','waiting') "
                            "ORDER BY id DESC LIMIT 1", (fingerprint,)).fetchone()
        return self._row(r) if r else None

    def set_status(self, incident_id, status, note, by):
        with self.lock:
            inc = self.get(incident_id)
            notes = inc.notes + [{"ts": time.time(), "by": by, "note": note, "status": status}]
            self.db.execute("UPDATE incidents SET status=?, notes=?, last_seen=? WHERE id=?",
                            (status, json.dumps(notes), time.time(), incident_id))

    def set_sort(self, incident_id, label, reason):
        with self.lock:
            self.db.execute("UPDATE incidents SET sort_label=?, sort_reason=? WHERE id=?", (label, reason, incident_id))

    def upsert_external(self, source, ext_id, title, status, detail):
        with self.lock:
            r = self.db.execute("SELECT id FROM incidents WHERE source=? AND ext_id=?", (source, ext_id)).fetchone()
            now = time.time()
            if r:
                self.db.execute("UPDATE incidents SET title=?, detail=? WHERE id=?", (title, json.dumps(detail), r["id"]))
                return r["id"], False
            self.db.execute(
                "INSERT INTO incidents(fingerprint,watcher,source,ext_id,title,severity,status,first_seen,last_seen,detail) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"{source}:{ext_id}", source, source, ext_id, title, 1, status, now, now, json.dumps(detail)))
            return self.db.execute("SELECT last_insert_rowid()").fetchone()[0], True

    # samples ---------------------------------------------------------
    def add_sample(self, key, ts, value):
        with self.lock:
            self.db.execute("INSERT INTO samples VALUES(?,?,?)", (key, float(ts), float(value)))

    def samples(self, key, since):
        return [(r[0], r[1]) for r in self.db.execute(
            "SELECT ts, value FROM samples WHERE key=? AND ts>=? ORDER BY ts", (key, since))]

    def last_sample(self, key):
        r = self.db.execute("SELECT ts, value FROM samples WHERE key=? ORDER BY ts DESC LIMIT 1", (key,)).fetchone()
        return (r[0], r[1]) if r else None

    # watchers --------------------------------------------------------
    def record_watcher_run(self, name, ok, fired, error=None, ts=None):
        with self.lock:
            self.db.execute("INSERT INTO watcher_runs VALUES(?,?,?,?,?)",
                            (name, ts or time.time(), int(ok), int(fired), error))

    def watcher_stats(self):
        out = []
        for r in self.db.execute(
                "SELECT name, COUNT(*) runs, SUM(fired) fires, SUM(1-ok) errors, MAX(ts) last_run, "
                "MAX(CASE WHEN ok=1 THEN ts END) last_ok FROM watcher_runs GROUP BY name ORDER BY name"):
            d = dict(r)
            e = self.db.execute("SELECT error FROM watcher_runs WHERE name=? AND ok=0 ORDER BY ts DESC LIMIT 1",
                                (d["name"],)).fetchone()
            d["last_error"] = e[0] if e else None
            out.append(d)
        return out

    def watcher_errors_since(self, name, since):
        return self.db.execute("SELECT COUNT(*) FROM watcher_runs WHERE name=? AND ok=0 AND ts>=?",
                               (name, since)).fetchone()[0]

    # claude runs -----------------------------------------------------
    def record_run(self, kind, ok, detail, ts=None):
        with self.lock:
            self.db.execute("INSERT INTO runs(kind,ts,ok,detail) VALUES(?,?,?,?)",
                            (kind, ts or time.time(), int(ok), json.dumps(detail)))
            return self.db.execute("SELECT last_insert_rowid()").fetchone()[0]

    def runs_today(self, kind, tz=TZ):
        start = datetime.now(ZoneInfo(tz)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return self.db.execute("SELECT COUNT(*) FROM runs WHERE kind=? AND ts>=?", (kind, start)).fetchone()[0]

    # summaries -------------------------------------------------------
    def save_summary(self, day, payload, ts=None):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO summaries VALUES(?,?,?)",
                            (day, ts or time.time(), json.dumps(payload)))

    def get_summary(self, day):
        r = self.db.execute("SELECT payload FROM summaries WHERE day=?", (day,)).fetchone()
        return json.loads(r[0]) if r else None

    def latest_summary(self):
        r = self.db.execute("SELECT day, ts, payload FROM summaries ORDER BY ts DESC LIMIT 1").fetchone()
        if not r:
            return None
        d = json.loads(r["payload"])
        d.setdefault("day", r["day"])
        d.setdefault("ts", r["ts"])
        return d

    def last_summary_ts(self):
        r = self.db.execute("SELECT MAX(ts) FROM summaries WHERE payload NOT LIKE '%\"held\": true%'").fetchone()
        return r[0] or 0

    # replay ----------------------------------------------------------
    def add_replay_finding(self, watcher, fingerprint, title, ts):
        with self.lock:
            self.db.execute("INSERT INTO replay_findings VALUES(?,?,?,?)", (watcher, fingerprint, title, ts))

    def clear_replay(self):
        with self.lock:
            self.db.execute("DELETE FROM replay_findings")

    def replay_findings(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM replay_findings ORDER BY ts DESC")]

    # small key/value state -------------------------------------------
    def kv_get(self, k, default=None):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def kv_set(self, k, v):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, json.dumps(v)))
