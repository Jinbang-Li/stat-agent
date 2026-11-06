# -*- coding: utf-8 -*-
"""
链路编排：把六层串成一次可观测、可审计、可降级的调用。

一次提问的完整路径：

    意图识别 → NL2SQL → 安全校验/执行 → 数据清洗 → 统计路由 → 计算 → 报告
       (LLM)     (LLM)      (4 层防御)      (代码)     (规则)    (代码)  (LLM)

每一段都记耗时，任何一段失败都不向上抛异常，而是降级并把原因写进 `degraded`。
最后无论成败都写一条审计记录——**失败的分析同样需要留痕**，否则没人知道
用户问过什么、系统为什么没答上来。
"""

import re
import time

import pandas as pd

from . import analysis as analysis_mod
from . import audit, config, schema as schema_mod
from .charts import render as render_svg
from .data_loader import cleaning_notes, detect_structure, profile, to_frame
from .intent import classify
from .llm_client import LLMClient
from .logging_setup import Timer, get_logger, log_event, new_trace_id, set_stage
from .nlsql import (FIELD_ALIASES, NL2SQL, county_names, detect_county,
                    detect_fields)
from .report_gen import generate as generate_report
from .stats_router import route

log = get_logger("pipeline")

_SCHEMA_CACHE = {}


class Result(object):
    def __init__(self):
        self.trace_id = ""
        self.question = ""
        self.session_id = ""
        self.actor = "local"
        self.intent = {}
        self.sql = ""
        self.sql_source = ""
        self.sql_note = ""
        self.sql_checks = []
        self.columns = []
        self.rows = []
        self.n_rows = 0
        self.profile = {}
        self.plan = {}
        self.analysis = {}
        self.report = {}
        self.charts = []
        self.timings = {}
        self.degraded = []
        self.error = ""
        self.tokens = {}
        self.ts = time.strftime("%Y-%m-%d %H:%M:%S")

    @property
    def ok(self):
        return not self.error

    def to_dict(self, include_rows=True):
        d = {
            "trace_id": self.trace_id, "question": self.question,
            "session_id": self.session_id, "ts": self.ts,
            "intent": self.intent, "sql": self.sql, "sql_source": self.sql_source,
            "sql_note": self.sql_note, "sql_checks": self.sql_checks,
            "n_rows": self.n_rows, "columns": self.columns,
            "profile": self.profile, "plan": self.plan,
            "analysis": self.analysis, "report": self.report,
            "charts": self.charts, "timings": self.timings,
            "degraded": self.degraded, "error": self.error,
            "tokens": self.tokens, "ok": self.ok,
        }
        if include_rows:
            d["rows"] = self.rows[:200]
        return d


def schema_prompt(source):
    """表结构提示词只生成一次：它由数据库实时 introspect 得到，不是手写文件。"""
    key = id(source)
    if key not in _SCHEMA_CACHE:
        snap = schema_mod.snapshot(source)
        _SCHEMA_CACHE[key] = snap
    return _SCHEMA_CACHE[key]["prompt"]


def warmup(llm=None, source=None):
    """启动自检。返回健康状态，供 /health 与 CLI 首屏使用。"""
    from .datasource import default_source

    source = source or default_source()
    config.ensure_dirs()
    audit.init()
    snap = schema_mod.snapshot(source)
    llm = llm or LLMClient(quiet=True)
    return {
        "datasource": source.describe() if hasattr(source, "describe") else source.dialect,
        "tables": [t.get("name") for t in snap["tables"]],
        "schema_blocking": snap["blocking"],
        "schema_issues": [i.to_dict() for i in snap["issues"]][:20],
        "prompt_chars": snap["prompt_chars"],
        "llm": llm.describe(),
        "llm_online": llm.online,
        "model": getattr(llm, "model", ""),
        "temperature": getattr(llm, "temperature", None),
        "audit_db": config.AUDIT_DB_PATH,
    }


def answer(question, session_id="local", actor="local", llm=None, source=None,
           persist=True):
    """跑一次完整链路。永远返回 Result。"""
    from .datasource import default_source

    started = time.time()
    r = Result()
    r.question = (question or "").strip()
    r.session_id = session_id or "local"
    r.actor = actor or "local"
    r.trace_id = new_trace_id()
    log_event(log, "question_received", question=r.question, session=r.session_id)

    if not r.question:
        r.error = "问题为空"
        r.report = {"markdown": "请输入一个问题。", "source": "guard", "flags": []}
        return r

    source = source or default_source()
    llm = llm or LLMClient(quiet=True)

    # ---- 1.5 前置闸门：明显越界或不安全的请求，不生成 SQL，直接说明
    gate = _precheck(r.question)
    if gate:
        r.report = gate["report"]
        r.degraded.append(gate["degraded"])
        r.plan = {"reject": True, "reject_code": gate["degraded"], "reason": gate["reason"],
                  "label": "未执行", "steps": [], "warnings": [], "notes": []}
        r.timings["total_ms"] = int((time.time() - started) * 1000)
        if persist:
            _write_audit(r)
        log_event(log, "question_blocked", reason=gate["degraded"])
        return r

    try:
        _run(r, llm, source)
    except Exception as exc:                              # noqa: BLE001
        r.error = "%s: %s" % (type(exc).__name__, exc)
        r.report = {"markdown": _crash_report(r), "source": "guard", "flags": ["pipeline:error"]}
        log_event(log, "pipeline_error", level="error", error=r.error)

    r.timings["total_ms"] = int((time.time() - started) * 1000)
    if llm is not None:
        r.tokens = dict(getattr(llm, "total_usage", {}) or {})

    if persist:
        _write_audit(r)
    log_event(log, "question_done", ms=r.timings.get("total_ms"), ok=r.ok,
              n_rows=r.n_rows, degraded=",".join(r.degraded) or None)
    return r


def _run(r, llm, source):
    # ---- 1 意图
    set_stage("intent")
    with Timer() as t:
        intent = classify(r.question, llm if llm.online else None)
    r.timings["intent_ms"] = t.ms
    r.intent = intent.to_dict()
    if intent.causal_request:
        r.degraded.append("intent:causal_requested")

    # ---- 2 NL2SQL
    set_stage("nlsql")
    with Timer() as t:
        gen = NL2SQL(llm=llm, schema_prompt=schema_prompt(source), source=source)
        sq = gen.generate(r.question, intent)
    r.timings["sql_ms"] = t.ms
    r.sql = sq.sql
    r.sql_source = sq.source
    r.sql_note = sq.note
    r.sql_checks = sq.guard.to_list() if sq.guard else []
    r.columns = list(sq.columns or [])
    r.rows = list(sq.rows or [])
    r.n_rows = len(r.rows)
    if sq.degraded:
        r.degraded.append(sq.degraded)
    if sq.out_of_scope:
        # 模型说"答不了"。这里刻意不回退模板：模板只能拼出一条回答了别的问题的
        # SQL（比如把"监利市的预测"变成"48 个县的均值"），那种答案比拒绝更糟。
        r.error = "超出当前数据范围：%s" % (sq.note or "模型判定无法用现有字段回答")
        r.report = {"markdown": _scope_report(r, source), "source": "scope",
                    "flags": ["sql:out_of_scope"]}
        return
    if not sq.ok:
        r.error = "SQL 未能通过安全校验或执行失败：%s" % sq.note
        r.report = {"markdown": _sql_fail_report(r), "source": "guard",
                    "flags": ["sql:blocked"]}
        return

    # ---- 3 数据装配与清洗
    set_stage("load")
    with Timer() as t:
        df = to_frame(r.columns, r.rows)
    r.timings["load_ms"] = t.ms
    r.profile = profile(df)

    if len(df) == 0:
        r.error = "查询返回 0 行，无法分析"
        r.report = {"markdown": _empty_report(r), "source": "guard",
                    "flags": ["data:empty"]}
        return

    # ---- 4 统计路由（这里可能"拒绝"，拒绝不是错误）
    set_stage("route")
    with Timer() as t:
        plan = route(df, intent, r.question)
    r.timings["route_ms"] = t.ms
    r.plan = plan.to_dict()

    used = []
    for key in ("numeric", "categorical", "time", "unit"):
        used.extend(plan.columns.get(key) or [])
    notes = cleaning_notes(df, [c for c in used if c])

    # ---- 5 计算
    set_stage("analysis")
    with Timer() as t:
        an = analysis_mod.run(df, plan)
    r.timings["analysis_ms"] = t.ms
    r.analysis = an.to_dict()

    # ---- 6 图表
    set_stage("charts")
    with Timer() as t:
        charts = []
        for spec in an.charts[:3]:
            charts.append({"id": spec.get("id"), "title": spec.get("title"),
                           "type": spec.get("type"), "svg": render_svg(spec)})
    r.timings["charts_ms"] = t.ms
    r.charts = charts

    if an.error and not plan.reject:
        r.error = an.error
        r.degraded.append("analysis:error")

    # ---- 7 报告
    set_stage("report")
    with Timer() as t:
        ctx = {"sql": r.sql, "n_rows": len(df), "n_cols": len(df.columns),
               "sql_source": sq.source, "cleaning_notes": notes}
        rep = generate_report(r.question, an, plan, llm=llm, context=ctx)
    r.timings["report_ms"] = t.ms
    r.report = rep.to_dict()
    r.degraded.extend(rep.flags)


def _write_audit(r):
    try:
        audit.record(
            trace_id=r.trace_id, session_id=r.session_id, actor=r.actor,
            question=r.question, intent=(r.intent or {}).get("intent", ""),
            sql=r.sql, sql_source=r.sql_source,
            ok=bool(r.ok and not (r.plan or {}).get("reject")),
            row_count=r.n_rows, latency_ms=r.timings.get("total_ms"),
            degraded=",".join(r.degraded) or None, error=r.error,
            extra={"plan_method": (r.plan or {}).get("method"),
                   "reject": (r.plan or {}).get("reject"),
                   "reject_code": (r.plan or {}).get("reject_code"),
                   "report_source": (r.report or {}).get("source"),
                   "tokens": r.tokens, "timings": r.timings},
        )
    except Exception as exc:                              # noqa: BLE001
        log_event(log, "audit_failed", level="warning", error=str(exc))


# ------------------------------------------------------------------ 兜底报告
def _precheck(question):
    """前置闸门：把"明显不该执行"的请求挡在生成 SQL 之前。

    两道闸门：
      1. 写操作/破坏性指令 —— 本系统只读，且不允许用户通过自然语言改写数据。
         即使 SQL 层有四层防御，也没必要先让模型生成一句 DROP 再拒绝。
      2. 完全超出数据范围的问题（没有命中任何已知字段，也没有分析意图）——
         与其让模型硬凑一个 SQL 返回一堆无关数字，不如直接说"查不到"。
    """
    q = question or ""
    if _DESTRUCTIVE.search(q):
        return {
            "degraded": "guard:write_intent",
            "reason": "请求包含写操作或破坏性指令",
            "report": {"source": "guard", "flags": ["guard:write_intent"], "markdown": "\n".join([
                "# %s" % q, "",
                "## 结论：本次请求被拒绝", "",
                "**原因**：系统对数据源只有只读权限，不支持插入、更新、删除、改表等操作；"
                "这条请求没有生成 SQL，也没有读取任何数据。", "",
                "**建议**：如需调整数据，请由数据管理员在数据库侧操作，并留存变更记录。", "",
            ])},
        }

    fields = detect_fields(q)
    if not fields and not _ANALYSIS_WORDS.search(q):
        names = []
        for col, meta in (FIELD_ALIASES or {}).items():
            label = analysis_mod.LABELS.get(col, col)
            if label not in names:
                names.append(label)
        return {
            "degraded": "guard:out_of_scope",
            "reason": "问题没有命中任何已知字段或分析意图",
            "report": {"source": "guard", "flags": ["guard:out_of_scope"], "markdown": "\n".join([
                "# %s" % q, "",
                "## 结论：超出数据范围", "",
                "**原因**：这个问题没有对应到当前数据源里的任何字段，也没有可识别的分析意图。"
                "为避免编造字段或答非所问，系统没有生成 SQL。", "",
                "**可分析的内容**：%s。" % "、".join(names[:12]), "",
                "**示例**：2023 年各县 GDP 的均值是多少 / 居民收入和农业总产值是否显著相关 / "
                "贫困县与非贫困县的收入差异是否显著。", "",
            ])},
        }
    return None


_DESTRUCTIVE = re.compile(
    r"删除|清空|删掉|DROP\s+|TRUNCATE|DELETE\s+FROM|UPDATE\s+\w+\s+SET|INSERT\s+INTO"
    r"|ALTER\s+TABLE|CREATE\s+TABLE|覆盖数据|篡改|改表|清库", re.I)
_ANALYSIS_WORDS = re.compile(
    r"相关|关系|差异|显著|分布|离散|平均|均值|合计|总和|最高|最低|排名|前几|多少|"
    r"预测|趋势|走向|聚类|分组|分类|异常|离群|偏离|面板|历年|各年|逐年|占比|比重|"
    r"增速|增长|统计|描述|极值|中位数|四分位")


def _crash_report(r):
    return "\n".join([
        "# 处理失败", "",
        "本次请求在执行过程中出现异常：`%s`。" % r.error, "",
        "已记录 trace_id：`%s`，可凭此在审计日志中定位。" % r.trace_id, "",
        "建议：换一种问法，或缩小时间/地区范围后重试。",
    ])


def _sql_fail_report(r):
    lines = ["# SQL 未能生成或未通过安全校验", "",
             "**原因**：%s" % (r.sql_note or "未知"), ""]
    if r.sql:
        lines += ["**被拦截的语句**：", "", "```sql", r.sql, "```", ""]
    checks = [c for c in r.sql_checks if c.get("passed") is False]
    if checks:
        lines += ["**校验明细**：", ""]
        lines += ["- %s：%s" % (c.get("layer"), c.get("note")) for c in checks]
        lines += [""]
    lines += ["这条 SQL 没有执行，因此没有任何数据被读取或修改。",
              "如果是问题表述不清，可以换一种更具体的问法；"
              "如果确实需要读其他表，需要先在 `data/schema_meta.json` 的 `scope` 中授权。"]
    return "\n".join(lines)


def _scope_report(r, source=None):
    """模型判定"现有字段答不了"时的报告。

    要点是**不要给出一个似是而非的答案**：宁可明确说答不了，也不要用
    另一个问题的查询结果顶上。
    """
    lines = ["# 这个问题无法用现有数据回答", "",
             "**原因**：%s" % (r.sql_note or "超出当前数据范围"), ""]

    try:
        meta = config.load_schema_meta()
        names = [c.get("name") or k
                 for k, c in (meta.get("tables", {})
                              .get("county_economy", {}).get("columns", {}).items())]
    except Exception:                                   # noqa: BLE001
        names = []

    if names:
        lines += ["**数据里有的字段**：%s" % "、".join(names), ""]

    try:
        counties = county_names(source)
    except Exception:                                   # noqa: BLE001
        counties = []
    if counties:
        hit, _ = detect_county(r.question, source)
        if hit:
            lines += ["**县名**：`%s` 在数据中（年份 2015–2023）——如果你问的是它，"
                      "问题可能出在「预测/外推」这类 SQL 做不到的动作上；"
                      "系统会先取历史序列再由统计模块外推。" % hit, ""]
        else:
            preview = "、".join(counties[:12])
            lines += ["**数据里共有 %d 个县**，例如：%s……" % (len(counties), preview), ""]

    lines += ["**可以这样问**：",
              "- 2023 年各县 GDP 的平均值是多少",
              "- 预测监利市未来三年的 GDP",
              "- 贫困县和非贫困县的收入有显著差异吗",
              "",
              "本次没有执行任何 SQL，也没有读取数据。"]
    return "\n".join(lines)


def _empty_report(r):
    return "\n".join([
        "# 查询没有返回数据", "",
        "SQL 已执行，但结果集为 0 行。", "",
        "通常是因为筛选条件太严（年份、省份、指标组合不匹配）。"
        "可以去掉一部分条件后重试。", "",
        "```sql", r.sql, "```",
    ])


def last_traces(limit=20, session_id=None):
    return audit.recent(limit=limit, session_id=session_id)
