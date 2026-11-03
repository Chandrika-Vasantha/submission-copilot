"""Persistence: SQLite for submissions + append-only audit log; files on disk.

Production mapping: Postgres (Aurora/Azure SQL) for these tables, S3/Blob with
object-lock for originals and page images, audit table replicated to a WORM
store / SIEM.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
  id TEXT PRIMARY KEY, received_at TEXT, sender TEXT, subject TEXT, insured TEXT,
  status TEXT, decision TEXT, team TEXT, assignee TEXT, rules_version TEXT, ai_engine TEXT,
  result_path TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, submission_id TEXT, ts TEXT, actor TEXT,
  action TEXT, detail TEXT);
"""


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        with self._c() as c:
            c.executescript(SCHEMA)

    def _c(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def upsert(self, **row):
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        cols = ",".join(row)
        q = ",".join("?" * len(row))
        upd = ",".join(f"{k}=excluded.{k}" for k in row if k != "id")
        with self._c() as c:
            c.execute(f"INSERT INTO submissions ({cols}) VALUES ({q}) ON CONFLICT(id) DO UPDATE SET {upd}", list(row.values()))

    def audit(self, sid, actor, action, detail=None):
        with self._c() as c:
            c.execute("INSERT INTO audit (submission_id, ts, actor, action, detail) VALUES (?,?,?,?,?)",
                      (sid, datetime.now(timezone.utc).isoformat(timespec="seconds"), actor, action,
                       json.dumps(detail, default=str) if detail is not None else None))

    def list(self):
        with self._c() as c:
            return [dict(r) for r in c.execute("SELECT * FROM submissions ORDER BY received_at DESC")]

    def get(self, sid):
        with self._c() as c:
            r = c.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
            return dict(r) if r else None

    def audit_log(self, sid):
        with self._c() as c:
            return [dict(r) for r in c.execute("SELECT * FROM audit WHERE submission_id=? ORDER BY seq", (sid,))]
