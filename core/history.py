# -*- coding: utf-8 -*-
"""
历史查询记录。

和 `core/audit.py` 的分工要说清楚，否则很容易被当成重复造轮子：

    audit_events   为"追责"服务：谁、何时、问了什么、跑了什么 SQL、是否被拦，
                   只追加、不可改、带哈希链。它不关心分析内容。
    query_history  为"复用"服务：这条问题的结论是什么、当时用的什么方法、
                   完整报告长什么样，要能点开回看。它允许清理，也允许改文案。

两者都落在 `data/audit.db`（同一个 SQLite 文件，各自一张表）：历史记录本质上是
"给人看的审计视图"，但把完整的报告正文塞进哈希链里的审计表会让链变脆
（报告里有模型措辞，重跑一次就变），所以分表存。

存储取舍：报告正文（markdown）和图表（SVG）都直接存文本。单条通常 5–20 KB，
存最近几百条对 SQLite 来说毫无压力，换来的是"点开就是当时那份报告"，
不需要重跑（重跑会因为模型温度、数据更新而给出不同结论）。
"""

import json
import sqlite3
import time

from . import config

LIMIT_DEFAULT = 20

_SCHEMA = """
CREATE TABLE IF NOT EXISTS query_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    trace_id    TEXT,
    session_id  TEXT,
    actor       TEXT,
    question    TEXT NOT NULL,
    intent      TEXT,
    intent_label TEXT,
    sql         TEXT,
    method      TEXT,
    summary     TEXT,
    n_rows      INTEGER,
    latency_ms  INTEGER,
    degraded    TEXT,
    ok          INTEGER,
    data_source TEXT,
    report_md   TEXT,
    report_source TEXT,
    charts_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_ts ON query_history(ts DESC);
"""


def _conn():
    conn = sqlite3.connect(config.AUDIT_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    """建表。幂等，启动时调用。"""
    config.ensure_dirs()
    with _conn() as conn:
        conn.executescript(_SCHEMA)


# ------------------------------------------------------------------ 写
def record(result):
    """把一次问答写进历史。失败只警告，不影响主流程。"""
    try:
        init()
        row = _row_from(result)
        with _conn() as conn:
            cur = conn.execute(
                "INSERT INTO query_history (ts, trace_id, session_id, actor, question,"
                " intent, intent_label, sql, method, summary, n_rows, latency_ms,"
                " degraded, ok, data_source, report_md, report_source, charts_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["ts"], row["trace_id"], row["session_id"], row["actor"],
                 row["question"], row["intent"], row["intent_label"], row["sql"],
                 row["method"], row["summary"], row["n_rows"], row["latency_ms"],
                 row["degraded"], row["ok"], row["data_source"], row["report_md"],
                 row["report_source"], row["charts_json"]))
            return cur.lastrowid
    except Exception as exc:                              # noqa: BLE001
        from .logging_setup import get_logger, log_event
        log_event(get_logger("history"), "history_write_failed",
                  level="warning", error="%s: %s" % (type(exc).__name__, exc))
        return None


def _row_from(r):
    intent = getattr(r, "intent", None) or {}
    plan = getattr(r, "plan", None) or {}
    report = getattr(r, "report", None) or {}
    charts = getattr(r, "charts", None) or []
    return {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "trace_id": getattr(r, "trace_id", "") or "",
        "session_id": getattr(r, "session_id", "") or "",
        "actor": getattr(r, "actor", "") or "",
        "question": getattr(r, "question", "") or "",
        "intent": intent.get("intent", ""),
        "intent_label": intent.get("label", ""),
        "sql": getattr(r, "sql", "") or "",
        "method": plan.get("label") or plan.get("method")
                  or ("未建模" if plan.get("reject") else ""),
        "summary": summarize(r),
        "n_rows": int(getattr(r, "n_rows", 0) or 0),
        "latency_ms": int((getattr(r, "timings", {}) or {}).get("total_ms") or 0),
        "degraded": ",".join(getattr(r, "degraded", []) or []),
        "ok": 1 if getattr(r, "ok", False) else 0,
        "data_source": getattr(r, "data_source", "sql") or "sql",
        # 只存图表标题与 SVG，不存 points：报告里要复现的是当时那张图
        "report_md": report.get("markdown", "") or "",
        "report_source": report.get("source", "") or "",
        "charts_json": json.dumps(
            [{"title": c.get("title"), "type": c.get("type"), "svg": c.get("svg")}
             for c in charts], ensure_ascii=False),
    }


def summarize(r):
    """一句话说清结论。列表页只显示这一行，所以必须自带判断。"""
    plan = getattr(r, "plan", None) or {}
    an = getattr(r, "analysis", None) or {}
    stats = an.get("stats") or {}

    if plan.get("reject"):
        return "未建模：%s" % (plan.get("reason") or plan.get("reject_code") or "不满足前提")
    if an.get("error"):
        return "分析失败：%s" % an.get("error")
    if stats.get("pairs"):
        parts = []
        for p in stats["pairs"][:2]:
            r_val = p.get("r")
            p_val = p.get("p")
            mark = "显著" if (p_val is not None and p_val < 0.05) else "不显著"
            parts.append("r=%s，p=%s（%s）" % (_num(r_val, 3), _num(p_val, 4), mark))
        return "；".join(parts)
    if stats.get("group"):
        g = stats["group"]
        sig = "显著" if (g.get("p") is not None and g["p"] < 0.05) else "不显著"
        return "%s：p=%s（%s），n=%s" % (g.get("test") or "组间检验",
                                        _num(g.get("p"), 4), sig, g.get("n"))
    if stats.get("timeseries"):
        t = stats["timeseries"]
        if t.get("refused"):
            return "时序拒绝建模：%s" % (t.get("reason") or "")
        g = t.get("growth_pct")
        return "趋势：年均增速 %s%%，R²=%s" % (_num(g, 2), _num(t.get("r2"), 3))
    if stats.get("cluster"):
        c = stats["cluster"]
        return "聚类：k=%s，轮廓系数 %s" % (c.get("k"), _num(c.get("silhouette"), 3))
    if stats.get("outliers"):
        o = stats["outliers"]
        return "异常识别：确认 %s 个，疑似 %s 个" % (o.get("n_confirmed"), o.get("n_level_flag"))
    if stats.get("panel"):
        p = stats["panel"]
        coef = (p.get("coefs") or [{}])
        cw = [c for c in coef if c.get("name") == "双向去均值"] or coef
        return "面板相关：双向去均值 r=%s" % _num((cw[0] or {}).get("r"), 3)
    if stats.get("columns"):
        cols = stats["columns"]
        return "描述统计：%d 个变量，n=%s" % (
            len(cols), (cols[0] or {}).get("n") if cols else an.get("n_used"))
    if getattr(r, "error", ""):
        return r.error[:80]
    return "查询：返回 %s 行" % getattr(r, "n_rows", 0)


def _num(v, nd=2):
    try:
        return ("%." + str(nd) + "f") % float(v)
    except (TypeError, ValueError):
        return "—"


# ------------------------------------------------------------------ 读
def recent(limit=LIMIT_DEFAULT):
    """列表页用：不带报告正文，避免一次拉回几十份 markdown。"""
    init()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, ts, trace_id, question, intent, intent_label, method, summary,"
            " n_rows, latency_ms, degraded, ok, data_source, report_source"
            " FROM query_history ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def get(item_id):
    """回看用：整条记录，含报告与图表。"""
    init()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM query_history WHERE id = ?",
                           (int(item_id),)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["charts"] = json.loads(d.pop("charts_json") or "[]")
    except ValueError:
        d["charts"] = []
    d["report"] = {"markdown": d.pop("report_md", ""),
                   "source": d.pop("report_source", "")}
    return d


def clear(keep=0):
    """清理历史：默认全清；keep>0 时保留最近 keep 条。"""
    init()
    with _conn() as conn:
        if keep and keep > 0:
            conn.execute("DELETE FROM query_history WHERE id NOT IN "
                         "(SELECT id FROM query_history ORDER BY id DESC LIMIT ?)",
                         (int(keep),))
        else:
            conn.execute("DELETE FROM query_history")
    return count()


def count():
    init()
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM query_history").fetchone()[0]
