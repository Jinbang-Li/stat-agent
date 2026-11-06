# -*- coding: utf-8 -*-
"""
审计日志。

商业场景里"审计"要回答四个问题，缺一不可：
    谁       -> actor（工号/账号；单机部署时是 anonymous 或环境变量指定的持有人）
    何时     -> ts
    问了什么 -> question / intent
    做了什么 -> sql / ok / row_count / 降级情况 / 耗时

另外做了两件容易被忽略但很关键的事：
1. **独立库**：审计写 data/audit.db，和业务库物理隔离。审计不能因为业务库被删就没了。
2. **哈希链**：每条记录带 prev_hash 和 hash，改动任何一条历史记录都会让后续链断裂，
   可以用 verify_chain() 一键验真。这比"写个日志文件"强一个量级。

审计失败**不允许**影响主流程：record() 内部吞掉异常并打到应用日志里。
但这本身就是一条告警——生产上要监控 audit_fail 计数。
"""

import hashlib
import json
import os
import sqlite3
import threading
import time

from . import config

_LOCK = threading.Lock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    trace_id    TEXT,
    session_id  TEXT,
    actor       TEXT,
    question    TEXT,
    intent      TEXT,
    sql         TEXT,
    sql_source  TEXT,
    ok          INTEGER,
    row_count   INTEGER,
    latency_ms  REAL,
    degraded    TEXT,
    error       TEXT,
    payload     TEXT,
    prev_hash   TEXT,
    hash        TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_events(session_id);
"""


def _connect():
    config.ensure_dirs()
    conn = sqlite3.connect(config.AUDIT_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    with _LOCK:
        conn = _connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()


def _canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(prev_hash, payload_json):
    h = hashlib.sha256()
    h.update((prev_hash or "").encode("utf-8"))
    h.update(payload_json.encode("utf-8"))
    return h.hexdigest()


def record(question="", intent="", sql="", sql_source="", ok=True, row_count=0,
           latency_ms=0.0, degraded=None, error="", extra=None, actor=None,
           session_id=None, trace_id=None):
    """写一条审计。任何异常都吞掉，只记到应用日志。"""
    try:
        from .logging_setup import get_logger, get_trace_id
        logger = get_logger("audit")
        trace_id = trace_id or get_trace_id()
    except Exception:                                   # pragma: no cover
        logger = None
        trace_id = trace_id or "-"

    payload = {
        "trace_id": trace_id,
        "session_id": session_id or "-",
        "question": question,
        "intent": intent,
        "sql": sql,
        "sql_source": sql_source,
        "ok": bool(ok),
        "row_count": int(row_count or 0),
        "latency_ms": float(latency_ms or 0),
        "degraded": degraded or "",
        "error": (error or "")[:2000],
    }
    if extra:
        payload["extra"] = extra
    payload_json = _canonical(payload)

    try:
        init()
        with _LOCK:
            conn = _connect()
            try:
                cur = conn.execute("SELECT hash FROM audit_events ORDER BY id DESC LIMIT 1")
                row = cur.fetchone()
                prev_hash = row["hash"] if row else ""
                h = _hash(prev_hash, payload_json)
                conn.execute(
                    "INSERT INTO audit_events (ts, trace_id, session_id, actor, question, "
                    "intent, sql, sql_source, ok, row_count, latency_ms, degraded, error, "
                    "payload, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (time.strftime("%Y-%m-%d %H:%M:%S"),
                     trace_id, payload["session_id"],
                     actor or os.environ.get("STAT_AGENT_ACTOR", "anonymous"),
                     question, intent, sql, sql_source, int(bool(ok)), payload["row_count"],
                     payload["latency_ms"], payload["degraded"] or "", payload["error"],
                     payload_json, prev_hash, h))
                conn.commit()
            finally:
                conn.close()
        return True
    except Exception as e:                              # noqa: BLE001
        if logger:
            logger.warning("审计写入失败（不影响主流程）：%s", e)
        return False


def recent(limit=50, session_id=None):
    init()
    conn = _connect()
    try:
        if session_id:
            cur = conn.execute(
                "SELECT * FROM audit_events WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, int(limit)))
        else:
            cur = conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?",
                               (int(limit),))
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            d.pop("payload", None)
            d.pop("prev_hash", None)
            d.pop("hash", None)
            rows.append(d)
        return rows
    finally:
        conn.close()


def summary(days=7):
    """给运维面板用的聚合：请求数、失败率、平均耗时、降级次数。"""
    init()
    conn = _connect()
    try:
        since = time.strftime("%Y-%m-%d 00:00:00",
                              time.localtime(time.time() - days * 86400))
        cur = conn.execute(
            "SELECT COUNT(*) n, SUM(ok) ok_n, AVG(latency_ms) avg_ms, "
            "SUM(CASE WHEN degraded<>'' THEN 1 ELSE 0 END) deg_n, "
            "SUM(row_count) rows_n FROM audit_events WHERE ts >= ?", (since,))
        r = cur.fetchone()
        n = r["n"] or 0
        return {
            "window_days": days,
            "requests": n,
            "failed": n - (r["ok_n"] or 0),
            "fail_rate": round((n - (r["ok_n"] or 0)) / n, 4) if n else 0.0,
            "avg_latency_ms": round(r["avg_ms"] or 0, 1),
            "degraded": r["deg_n"] or 0,
            "rows_returned": r["rows_n"] or 0,
        }
    finally:
        conn.close()


def verify_chain(limit=None):
    """校验哈希链完整性。返回 (ok, 第一个断点 id 或 None, 总条数)。"""
    init()
    conn = _connect()
    try:
        sql = "SELECT id, payload, prev_hash, hash FROM audit_events ORDER BY id ASC"
        if limit:
            sql += " LIMIT %d" % int(limit)
        cur = conn.execute(sql)
        rows = cur.fetchall()
        prev = ""
        for r in rows:
            if (r["prev_hash"] or "") != prev:
                return False, r["id"], len(rows)
            if _hash(prev, r["payload"]) != r["hash"]:
                return False, r["id"], len(rows)
            prev = r["hash"]
        return True, None, len(rows)
    finally:
        conn.close()
