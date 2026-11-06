# -*- coding: utf-8 -*-
"""
Streamlit 入口。

启动：
    streamlit run app.py
    （可选）streamlit run app.py --server.port 8501 --server.address 127.0.0.1

这一层只做三件事：收集问题、调用 core.pipeline.answer()、把结果摆出来。
**不放任何业务逻辑**——意图判断、SQL 生成、统计计算、报告撰写全在 core/ 里，
这样 CLI（main.py）、Web（web/server.py）、Streamlit 三个入口共用一条链路，
换界面不会换出两套结论。

依赖：streamlit。其余（pandas/numpy/scipy/statsmodels）都是 core 里已有的。
"""

import os
import sys

# 允许 `streamlit run app.py` 与 `python app.py` 都能找到 core 包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import streamlit as st
except ImportError:
    sys.stderr.write(
        "\n缺少 streamlit。安装：\n"
        "    pip install streamlit\n"
        "（当前 Python %d.%d；streamlit 1.13+ 需要 Python 3.8+）\n\n"
        % (sys.version_info[0], sys.version_info[1])
    )
    raise

from core import charts as charts_mod
from core import config, pipeline


def _rerun():
    """兼容 streamlit 新旧版本的刷新接口（st.rerun 是 1.27+ 才有的名字）。"""
    if hasattr(st, "rerun"):
        st.rerun()
    else:
        st.experimental_rerun()

# ----------------------------------------------------------------- 页面设置
st.set_page_config(page_title="经济统计分析助手", page_icon="📊", layout="wide")

EXAMPLES = [
    ("描述统计", "2023年各县农村居民收入的分布情况"),
    ("分组检验", "贫困县和非贫困县的收入有显著差异吗"),
    ("相关检验", "2015年到2023年各县居民收入与农业总产值的关系"),
    ("异常诊断", "找出产值异常偏高的县"),
    ("时序预测", "预测未来三年全县平均GDP的走势"),
]

CSS = """
<style>
  .block-container {padding-top: 2.2rem; max-width: 1180px;}
  .sa-hero h1 {font-size: 1.65rem; margin-bottom: .2rem;}
  .sa-sub {color: #6b7280; font-size: .92rem; margin-bottom: 1.1rem;}
  .sa-pill {display:inline-block; padding:.12rem .55rem; border-radius:1rem;
            font-size:.78rem; margin-right:.35rem; border:1px solid #e5e7eb;
            background:#f9fafb; color:#374151;}
  .sa-pill.ok {background:#ecfdf5; border-color:#a7f3d0; color:#065f46;}
  .sa-pill.warn {background:#fffbeb; border-color:#fde68a; color:#92400e;}
  .sa-pill.bad {background:#fef2f2; border-color:#fecaca; color:#991b1b;}
  .sa-chart {border:1px solid #eef0f3; border-radius:.6rem; padding:.4rem;
             margin-bottom:.6rem; background:#fff; overflow-x:auto;}
  .sa-chart svg {max-width:100%; height:auto;}
  div[data-testid="stMetricValue"] {font-size: 1.15rem;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ----------------------------------------------------------------- 启动自检
def _warmup():
    """自检只做一次，放在 session_state 里，避免每次重跑都去 introspect 数据库。"""
    if "health" not in st.session_state:
        st.session_state["health"] = pipeline.warmup()
    return st.session_state["health"]


def _pill(text, kind=""):
    return '<span class="sa-pill %s">%s</span>' % (kind, text)


# ----------------------------------------------------------------- 侧栏
health = _warmup()
tables = health.get("tables") or []
online = health.get("llm_online")
schema_ok = not health.get("schema_blocking")

st.sidebar.markdown("### 运行状态")
st.sidebar.markdown(
    _pill("数据源 " + str(health.get("datasource", ""))[:28], "ok")
    + _pill("表 %d" % len(tables))
    + _pill("模型 %s" % ("在线" if online else "离线"), "ok" if online else "warn"),
    unsafe_allow_html=True,
)
st.sidebar.markdown(
    _pill("Schema 校验%s" % ("通过" if schema_ok else "有阻塞项"), "ok" if schema_ok else "bad"),
    unsafe_allow_html=True,
)
st.sidebar.caption(
    "模型：%s　温度：%s\n\n表结构提示词：%d 字符（运行时由数据库结构生成）"
    % (health.get("model", "-"), health.get("temperature", "-"),
       health.get("prompt_chars", 0))
)

issues = health.get("schema_issues") or []
if issues:
    with st.sidebar.expander("Schema 提示（%d）" % len(issues)):
        for it in issues:
            st.write("· [%s] %s.%s %s" % (it.get("level"), it.get("table"),
                                          it.get("column"), it.get("message")))

st.sidebar.markdown("---")
st.sidebar.markdown("### 能回答什么")
for label, q in EXAMPLES:
    st.sidebar.caption("%s · %s" % (label, q))

st.sidebar.markdown("---")
st.sidebar.markdown("### 边界")
st.sidebar.caption(
    "只读：不会生成或执行任何写操作。\n\n"
    "只做统计：相关不等于因果；样本不足或前提不满足时直接拒绝建模，不给结论。\n\n"
    "数字全部由 Python 计算，模型只负责措辞；报告里的每个数字都会与计算结果对账。"
)

# ----------------------------------------------------------------- 主区
st.markdown(
    '<div class="sa-hero"><h1>经济统计分析助手</h1>'
    '<div class="sa-sub">用自然语言提问，系统负责取数、选统计方法、做检验、出图、写报告。</div></div>',
    unsafe_allow_html=True,
)

if "sid" not in st.session_state:
    import uuid
    st.session_state["sid"] = "web-" + uuid.uuid4().hex[:8]

question = st.text_area(
    "问题",
    value=st.session_state.get("question_input", ""),
    height=88,
    placeholder="例如：贫困县和非贫困县的收入有显著差异吗（Ctrl/⌘ + Enter 提交）",
    label_visibility="collapsed",
)

col_a, col_b, col_c = st.columns([1, 1, 4])
run = col_a.button("开始分析", type="primary", use_container_width=True)
col_b.button("清空", on_click=lambda: st.session_state.update(
    {"question_input": "", "last": None}), use_container_width=True)

chips = st.columns(len(EXAMPLES))
for i, (label, q) in enumerate(EXAMPLES):
    if chips[i].button(label, use_container_width=True, key="chip_%d" % i):
        st.session_state["question_input"] = q
        st.session_state["autostart"] = True
        _rerun()

if st.session_state.pop("autostart", False):
    run = True

# ----------------------------------------------------------------- 执行
if run:
    q = (question or "").strip()
    if not q:
        st.warning("先写一个问题。")
    else:
        with st.spinner("正在取数、选方法、算检验……离线模式下也只需要 1~2 秒"):
            try:
                res = pipeline.answer(q, session_id=st.session_state["sid"], actor="web")
                st.session_state["last"] = res.to_dict()
            except Exception as exc:  # 界面层兜底，绝不白屏
                st.session_state["last"] = None
                st.error("这次没跑通：%s: %s" % (type(exc).__name__, exc))

res = st.session_state.get("last")
if not res:
    st.stop()

# ----------------------------------------------------------------- 结果
st.markdown("---")

degraded = res.get("degraded") or []
badges = []
if bool(health.get("llm_online")) is False:
    badges.append(_pill("离线模式（无模型调用）", "warn"))
if degraded:
    badges.append(_pill("降级：" + "、".join(degraded), "warn"))
if res.get("sql_source") == "template":
    badges.append(_pill("SQL 由模板生成", ""))
if res.get("ok"):
    badges.append(_pill("trace " + res.get("trace_id", ""), "ok"))
if badges:
    st.markdown(" ".join(badges), unsafe_allow_html=True)

mt = st.columns(4)
mt[0].metric("样本行数", res.get("n_rows", 0))
mt[1].metric("统计方法", (res.get("plan") or {}).get("label") or "—")
mt[2].metric("数据文件耗时", "%d ms" % ((res.get("timings") or {}).get("total_ms") or 0))
mt[3].metric("意图", (res.get("intent") or {}).get("label") or (res.get("intent") or {}).get("intent") or "—")

if res.get("error"):
    st.error(res["error"])

# 图表：SVG 直接嵌进页面，中文字体用客户端系统字体，服务端不需要配字体
specs = res.get("charts") or []
if specs:
    st.markdown("### 图表")
    for i, spec in enumerate(specs):
        try:
            svg = charts_mod.render(spec)
        except Exception:
            continue
        if i % 2 == 0:
            row = st.columns(2) if i + 1 < len(specs) else [st.container(), None]
            row[0].markdown('<div class="sa-chart">%s</div>' % svg, unsafe_allow_html=True)
        else:
            if row[1] is not None:
                row[1].markdown('<div class="sa-chart">%s</div>' % svg, unsafe_allow_html=True)
            else:
                st.markdown('<div class="sa-chart">%s</div>' % svg, unsafe_allow_html=True)

# 报告
st.markdown("### 分析报告")
report = res.get("report") or {}
st.markdown(report.get("markdown") or "（没有生成报告）")
st.caption(
    "报告来源：%s｜报告数字对账：%s"
    % ({"llm": "模型撰写", "template": "规则模板"}.get(report.get("source"), report.get("source") or "-"),
       "发现不一致已回退模板" if "report:number_mismatch" in (report.get("flags") or []) else "通过")
)
st.download_button(
    "下载 Markdown 报告",
    data=(report.get("markdown") or "").encode("utf-8"),
    file_name="report_%s.md" % (res.get("trace_id") or "stat"),
    mime="text/markdown",
)

# 明细标签页
tab_sql, tab_data, tab_plan, tab_run = st.tabs(["SQL 与校验", "数据预览", "决策轨迹", "运行信息"])

with tab_sql:
    st.markdown("**执行的 SQL**（`%s`）" % (res.get("sql_source") or "-"))
    st.code(res.get("sql") or "（没有生成 SQL）", language="sql")
    checks = res.get("sql_checks") or []
    if checks:
        for c in checks:
            passed = c.get("passed") is not False and c.get("ok") is not False
            st.markdown("%s **%s** — %s" % ("✅" if passed else "⛔",
                                            c.get("layer"), c.get("note")))
    if res.get("sql_note"):
        st.info(res["sql_note"])
    if not res.get("ok") and not (res.get("sql") or "").strip():
        st.warning("本次没有执行任何 SQL：请求在生成阶段就被拦截了。")

with tab_data:
    rows = res.get("rows") or []
    cols = res.get("columns") or []
    if rows:
        try:
            import pandas as pd
            st.dataframe(pd.DataFrame(rows[:200], columns=cols), use_container_width=True, height=320)
        except Exception:
            st.write(rows[:50])
        st.caption("最多展示 200 行；统计计算使用的是全部 %d 行。" % res.get("n_rows", 0))
    else:
        st.write("没有数据行。")
    prof = res.get("profile") or {}
    if prof:
        st.caption("重复行 %s｜缺失 %s" % (prof.get("duplicated_rows", 0),
                                          prof.get("notes") or "无"))

with tab_plan:
    plan = res.get("plan") or {}
    if plan.get("reject"):
        st.error("拒绝建模：%s\n\n建议：%s" % (plan.get("reason"), plan.get("suggestion")))
    for s in plan.get("steps") or []:
        st.markdown("%s %s — %s" % ("·" if s.get("ok", True) else "✗",
                                    s.get("name"), s.get("detail")))
    for w in plan.get("warnings") or []:
        st.warning(w)

with tab_run:
    t = res.get("timings") or {}
    order = ["intent_ms", "sql_ms", "load_ms", "route_ms", "analysis_ms", "charts_ms", "report_ms"]
    for k in order:
        if k in t:
            st.markdown("%s：%d ms" % (k.replace("_ms", ""), t[k] or 0))
    st.json({"trace_id": res.get("trace_id"), "session": res.get("session_id"),
             "ts": res.get("ts"), "degraded": degraded, "tokens": res.get("tokens") or {},
             "intent": res.get("intent")}, expanded=False)

st.markdown("---")
st.caption(
    "数据为**合成数据**（48 个县 × 2015–2023），仅用于演示方法链路；"
    "贫困县标签为规则生成的合成标签，不代表真实行政区划认定。"
)
