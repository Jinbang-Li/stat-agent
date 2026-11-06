# -*- coding: utf-8 -*-
"""
报告生成层：模型只做"把算好的数字说清楚"，不做"发现"。

两件事必须强调：

1. **数字白名单校验**。模型写完后，把报告里出现的每个数字抽出来，和
   `Analysis` 算出的数字集合比对。对不上的数字一律视为编造（哪怕只是把
   0.473 写成 0.47 之后又顺手改成 0.5），直接判定这次生成不可用，回退模板。
   这不是"让模型注意"，而是**事后可验证的硬约束**——幻觉控制如果只靠提示词，
   那就只是许愿。

2. **模板兜底不是降级体验，是常态能力**。LLM 挂掉、超时、返回乱码时，
   报告仍然要能出，且必须同样严谨。所以模板报告不是占位符，它是
   完整四段式报告，只是措辞固定。
"""

import json
import re

from . import config
from .llm_client import load_prompt


class Report(object):
    def __init__(self, markdown="", source="template", flags=None, error=""):
        self.markdown = markdown
        self.source = source          # llm | template
        self.flags = flags or []
        self.error = error
        self.tokens = {}

    def to_dict(self):
        return {"markdown": self.markdown, "source": self.source,
                "flags": self.flags, "error": self.error, "tokens": self.tokens}


def generate(question, analysis, plan, llm=None, context=None):
    """生成报告。永远返回一个 Report，不抛异常。"""
    payload = build_payload(question, analysis, plan, context)

    if llm is not None and getattr(llm, "online", False) and not plan.reject:
        try:
            text, tokens = _llm_report(payload, llm)
            if text:
                bad = verify_numbers(text, payload)
                if not bad:
                    r = Report(markdown=_with_verdict(text.strip(), payload),
                               source="llm")
                    r.tokens = tokens
                    return r
                # 数字对不上：宁可要一份措辞死板但数字可靠的报告
                r = Report(markdown=_with_verdict(_template_report(payload), payload),
                           source="template")
                r.flags.append("report:number_mismatch")
                r.error = "模型报告出现无法溯源的数字：%s" % "、".join(bad[:8])
                r.tokens = tokens
                return r
        except Exception as exc:                          # noqa: BLE001
            r = Report(markdown=_with_verdict(_template_report(payload), payload),
                       source="template")
            r.flags.append("report:llm_failed")
            r.error = "%s: %s" % (type(exc).__name__, exc)
            return r

    r = Report(markdown=_with_verdict(_template_report(payload), payload),
               source="template")
    if plan.reject:
        r.flags.append("report:rejected")
    elif llm is None or not getattr(llm, "online", False):
        r.flags.append("report:offline")
    return r


# ------------------------------------------------------------------ 载荷
def build_payload(question, analysis, plan, context=None):
    ctx = context or {}
    return {
        "question": question,
        "intent": {"name": plan.intent, "label": _intent_label(plan.intent)},
        "sql": ctx.get("sql", ""),
        "data": {
            "n_rows": ctx.get("n_rows", analysis.n_used),
            "n_cols": ctx.get("n_cols"),
            "structure": plan.structure.get("kind"),
            "structure_reason": plan.structure.get("reason"),
            "missing_notes": plan.notes,
            "cleaning_notes": ctx.get("cleaning_notes") or [],
            "sql_source": ctx.get("sql_source"),
        },
        "plan": {
            "method": plan.label, "reject": plan.reject,
            "reject_code": plan.reject_code, "reason": plan.reason,
            "suggestion": plan.suggestion,
            "steps": plan.steps, "warnings": plan.warnings,
        },
        "findings": analysis.findings,
        "labels": _labels_map(),
        "stats": analysis.stats,
        "tables": analysis.tables,
        "charts": [{"title": c.get("title")} for c in analysis.charts],
        "error": analysis.error,
    }


def _intent_label(name):
    try:
        from .intent import INTENT_LABELS
        return INTENT_LABELS.get(name, name)
    except Exception:                                     # noqa: BLE001
        return name


def _llm_report(payload, llm):
    system = load_prompt("report_prompt")
    user = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
    if len(user) > 12000:          # 控制输入长度，防止把整张表塞进上下文
        slim = dict(payload)
        slim["stats"] = _slim(payload.get("stats"))
        slim["tables"] = []
        user = json.dumps(slim, ensure_ascii=False, indent=1, default=str)
    raw = llm.chat(system, user)
    return raw, getattr(llm, "last_usage", {}) or {}


def _slim(stats, depth=0):
    if depth > 2:
        return "..."
    if isinstance(stats, dict):
        return dict((k, _slim(v, depth + 1)) for k, v in list(stats.items())[:20])
    if isinstance(stats, list):
        return [_slim(v, depth + 1) for v in stats[:8]]
    return stats


# ------------------------------------------------------------------ 数字校验
_NUM_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")

# 允许出现的常量：显著性水平、置信水平、IQR 系数、常见门槛
_ALLOWED_CONSTANTS = {0.05, 0.1, 0.01, 0.001, 95, 99, 90, 1.5, 3.5, 3, 5,
                      1, 2, 4, 6, 8, 10, 15, 20, 30, 50, 100}


def verify_numbers(text, payload):
    """返回无法溯源的数字列表。空列表 = 通过。"""
    allowed = _allowed_numbers(payload)
    bad = []
    for tok in _NUM_RE.findall(text or ""):
        raw = tok.replace(",", "")
        try:
            val = float(raw)
        except ValueError:
            continue
        if val in _ALLOWED_CONSTANTS:
            continue
        # 年份不是统计量："2023 年截面数据"里的 2023 不该被当成编造的数字。
        # 但只放行整数年份，且限 1900–2100——不给出一个宽到能藏住假系数的口子。
        if raw.isdigit() and 1900 <= val <= 2100:
            continue
        if _traceable(val, allowed):
            continue
        bad.append(tok)
    return _dedup(bad)


def _allowed_numbers(payload):
    """收集"模型见过的"所有数字。

    两类来源：
    1. 数值型字段——统计结果的原始值；
    2. 字符串里出现的数字——例如 SQL 里的 `LIMIT 5000`、结构说明里的年份。
       报告引用自己拿到的材料里的数字（比如把 SQL 抄进"分析过程"）不算编造，
       所以字符串同样要扫一遍；这也避免了"数字没编造，只是抄了 SQL"被误杀。
    """
    out = set()

    def walk(obj, depth=0):
        if depth > 6:
            return
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v, depth + 1)
        elif isinstance(obj, (list, tuple)):
            for v in obj[:200]:
                walk(v, depth + 1)
        elif isinstance(obj, bool):
            return
        elif isinstance(obj, (int, float)):
            out.add(round(float(obj), 6))
            out.add(round(abs(float(obj)), 6))
        elif isinstance(obj, str):
            for tok in _NUM_RE.findall(obj):
                try:
                    out.add(round(float(tok.replace(",", "")), 6))
                except ValueError:
                    continue

    walk(payload)
    return out


def _traceable(val, allowed, tol=0.0051):
    """数值比对允许四舍五入误差，也允许单位换算写法。

    模型把 1452470.1（万元）写成「145.2 亿」不是编造，是换算：这里放行
    ×/÷100（百分比）、×/÷1e4（万）、×/÷1e8（亿）三种缩放。
    缩放不放宽"必须能在原始结果里找到对应值"这一条——编造的数字依然对不上。

    容差必须随量级收缩：固定 0.005 的绝对容差会让 0.0001 和 0.0035 互相"匹配"，
    等于给假数字开了个后门（编造的 0.987 经 ×1e-4 缩放后就落进了这个区间）。
    """
    for scale in (1.0, 0.01, 100.0, 1e-4, 1e4, 1e-8, 1e8):
        cand = val * scale
        for a in allowed:
            abs_tol = min(tol, 0.005 * abs(a)) if a else tol
            if abs(cand - a) <= abs_tol + 1e-12:
                return True
            if a and abs(cand - a) / abs(a) < 0.002:
                return True
    return False


def _dedup(seq):
    seen, out = set(), []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ------------------------------------------------------------------ 模板报告
_ALPHA = 0.05


def _verdict(payload):
    """"显著 / 不显著"这句话由代码写，不交给模型措辞。

    模型写报告时措辞会飘：同一句话跑两次，一次写"未通过显著性检验"，
    一次写"不存在显著相关"。业务方读报告是要据此行动的，判定词不能随机。
    所以判定句统一由这里生成、附在报告末尾——模型负责解释，代码负责下判断。
    """
    st = payload.get("stats") or {}
    parts = []

    pairs = st.get("pairs") or []
    if isinstance(pairs, dict):
        pairs = [pairs]
    for pr in pairs:
        p = pr.get("p")
        if p is None:
            continue
        x, y = pr.get("x"), pr.get("y")
        labels = payload.get("labels") or {}
        name = "%s 与 %s" % (labels.get(x, x), labels.get(y, y))
        if p < _ALPHA:
            parts.append("%s：p=%.4f < 0.05（5%% 显著性水平），**显著**" % (name, p))
        else:
            parts.append("%s：p=%.4f ≥ 0.05（5%% 显著性水平），**不显著**" % (name, p))

    g = st.get("group") or {}
    if g.get("p") is not None:
        if g["p"] < _ALPHA:
            parts.append("组间差异：p=%.4f < 0.05（5%% 显著性水平），**显著**" % g["p"])
        else:
            parts.append("组间差异：p=%.4f ≥ 0.05（5%% 显著性水平），**不显著**" % g["p"])

    pn = st.get("panel") or {}
    w2 = pn.get("within_two_way") or {}
    if w2.get("p") is not None:
        parts.append("剔除个体与年份效应后的组内相关：p=%.4f，%s"
                     % (w2["p"], "**显著**" if w2["p"] < _ALPHA else "**不显著**"))

    ts = st.get("timeseries") or {}
    adf = ts.get("adf") or {}
    if adf.get("p") is not None and not ts.get("forecast"):
        parts.append("ADF 平稳性：p=%.4f，%s"
                     % (adf["p"], "序列平稳" if adf["p"] < _ALPHA else "**非平稳**"))

    if ts.get("forecast"):
        parts.append("预测为趋势外推，不含区间外的政策与冲击，只作量级参考")

    if not parts:
        return ""
    return "---\n\n**统计判定（系统生成，非模型表述）**：" + "；".join(parts) + "。"


def _with_verdict(markdown, payload):
    tail = _verdict(payload)
    if not tail:
        return markdown
    return markdown.rstrip() + "\n\n" + tail + "\n"


def _template_report(p):
    d = p.get("data") or {}
    pl = p.get("plan") or {}
    lines = ["# %s" % p.get("question", "").strip(), ""]

    if pl.get("reject"):
        lines += ["## 结论：本次不开展建模", "",
                  "**原因**：%s" % pl.get("reason", ""), ""]
        if pl.get("suggestion"):
            lines += ["**建议**：%s" % pl["suggestion"], ""]
        lines += _limits_section(p, rejected=True)
        return "\n".join(lines).strip() + "\n"

    # 一、数据概览
    lines += ["## 一、数据概览", ""]
    bits = []
    if d.get("n_rows") is not None:
        bits.append("有效样本 %s 条" % d["n_rows"])
    if d.get("n_cols"):
        bits.append("%s 个字段" % d["n_cols"])
    lines.append("- " + ("；".join(bits) + "。" if bits else "数据情况见下表。"))
    if d.get("structure"):
        kind = {"cross_section": "截面数据", "panel": "面板数据",
                "timeseries": "时间序列"}.get(d["structure"], d["structure"])
        lines.append("- 数据结构：%s，%s。" % (kind, d.get("structure_reason") or "已自动识别"))
    for note in (d.get("missing_notes") or []):
        lines.append("- %s（未填补，相关分析会对缺失样本做行删除并在此说明）。" % note)
    for note in (d.get("cleaning_notes") or []):
        lines.append("- %s。" % note)
    lines.append("")

    # 二、分析过程
    lines += ["## 二、分析过程", "",
              "按「先验数据、再定方法」的顺序确定方法：**%s**。" % pl.get("method", ""), ""]
    for st in (pl.get("steps") or []):
        if st.get("step") in ("拒绝",):
            continue
        lines.append("- **%s**：%s" % (st.get("step", ""), st.get("detail", "")))
    lines.append("")

    # 三、结果与解读
    lines += ["## 三、结果与解读", ""]
    findings = p.get("findings") or []
    for f in findings:
        lines.append("- %s" % f)
    if not findings:
        lines.append("- 本次没有产生可解读的统计结果。")
    if p.get("charts"):
        lines += ["", "配图：%s。" % "、".join("《%s》" % c["title"] for c in p["charts"])]
    lines.append("")

    # 四、结论与局限
    lines += _limits_section(p, rejected=False)
    return "\n".join(lines).strip() + "\n"


def _labels_map():
    try:
        from .analysis import LABELS
        return dict(LABELS)
    except Exception:                                     # noqa: BLE001
        return {}


def _name(labels, col):
    return labels.get(col, col)


def _conclusion(p):
    """规则生成的"一句话结论"。它只引用 stats 里已有的数字，不引入新数值。"""
    pl = p.get("plan") or {}
    st = p.get("stats") or {}
    labels = p.get("labels") or {}
    m = pl.get("method") or ""

    if st.get("pairs"):
        usable = [r for r in st["pairs"] if r.get("p") is not None]
        if not usable:
            return "有效样本不足，未得到可用的相关系数。"
        sig = [r for r in usable if (r.get("p_adjusted") if r.get("p_adjusted") is not None
                                     else r.get("p")) < 0.05]
        if not sig:
            if len(usable) == 1:
                return ("在 5%% 显著性水平下，%s 与 %s 未呈现显著相关（n=%d），"
                        "不能据此认为两者存在关联。"
                        % (_name(labels, usable[0]["x"]),
                           _name(labels, usable[0]["y"]), usable[0].get("n") or 0))
            return ("在 5%% 显著性水平下，%d 对变量均未呈现显著相关，"
                    "不能据此认为它们之间存在关联；样本量 n=%d。"
                    % (len(usable), usable[0].get("n") or 0))
        best = max(sig, key=lambda r: abs(r.get("r") or 0))
        return ("%s 与 %s 的相关最强（r=%.3f，p=%.4f），达到统计显著；"
                "但相关不等于因果，方向与机制仍需业务或进一步研究确认。"
                % (_name(labels, best["x"]), _name(labels, best["y"]),
                   best["r"], best.get("p_adjusted") or best.get("p")))

    if st.get("panel"):
        pan = st["panel"]
        r2 = pan.get("within_two_way", {}).get("r")
        if r2 is None:
            return "面板分解失败，未得到组内相关。"
        if pan.get("simpson"):
            return ("出现辛普森悖论：混合相关与剔除固定效应后的组内相关方向相反，"
                    "结论必须分「县际差异」与「县内变化」两层陈述。")
        return ("剔除县际差异与年份冲击后，组内相关 r=%.3f，这是可用于「同一县随时间变化」"
                "讨论的系数；混合相关的 p 值不采用。" % r2)

    if st.get("group"):
        g = st["group"]
        if g.get("significant"):
            return ("组间差异显著（p=%.4f，%s=%.3f），可以认为不同分组的表现存在系统性差别。"
                    % (g.get("p") or 0, g.get("effect_name") or "效应量",
                       g.get("effect_size") or 0))
        return ("在 5%% 显著性水平下组间差异不显著（p=%.4f），"
                "不能据此认为不同分组之间存在系统性差别。" % (g.get("p") or 0))

    if st.get("timeseries"):
        ts = st["timeseries"]
        if ts.get("growth_rate") is not None:
            return ("按对数线性趋势外推，年均增速约 %.2f%%，R²=%.3f；"
                    "外推结果只反映历史趋势的延续，不构成对未来的确定性判断。"
                    % (ts["growth_rate"], ts.get("r2") or 0))
        return ""

    if st.get("cluster"):
        c = st["cluster"]
        return ("按轮廓系数选定 k=%d（silhouette=%.3f），分组差异是描述性的，"
                "不代表存在真实的类别边界。" % (c.get("k") or 0, c.get("silhouette") or 0))

    if st.get("outliers"):
        o = st["outliers"]
        if o.get("n_confirmed"):
            return ("双重口径共同确认 %d 个异常样本，建议回查这几条的原始台账；"
                    "标记未删除任何样本。" % o["n_confirmed"])
        return "没有样本同时满足两个异常口径，因此不建议宣布存在异常值。"
    return ""


def _limits_section(p, rejected):
    pl = p.get("plan") or {}
    d = p.get("data") or {}
    lines = ["## 四、结论与局限", ""]
    if rejected:
        lines.append("- 未开展统计推断，因此不对变量间关系作任何判断。")
    else:
        concl = _conclusion(p)
        if concl:
            lines.append("- **综合判断**：%s" % concl)
    lines.append("- 以上所有统计量由 Python（scipy / statsmodels）计算，报告仅负责组织表述，"
                 "未对数值做任何调整。")
    lines.append("- 结论适用于本次查询覆盖的样本范围，不能外推到未包含的地区或年份。")
    lines.append("- 相关关系不等于因果关系；本报告不提供因果推断。")
    if (d.get("structure") == "panel"):
        lines.append("- 面板数据的混合相关 p 值未采用（忽略个体自相关会低估标准误），"
                     "仅组内相关用于解读变化关系。")
    for w in (pl.get("warnings") or []):
        lines.append("- %s。" % w)
    if p.get("error"):
        lines.append("- 分析执行中的问题：%s。" % p["error"])
    lines.append("")
    return lines
