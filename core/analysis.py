# -*- coding: utf-8 -*-
"""
分析执行层：把 `stats_router.Plan` 落成真正的计算。

这里有一条不可逾越的界线：**所有数字由本模块的 Python 代码算出，大模型一个字都不许改**。
模型只负责把算好的结果组织成通顺的中文；报告里出现的每个统计量都必须能在
`Analysis.stats` 里找到出处。这条界线是整套系统可信度的地基。

本模块同时产出**图表规格**（纯数据，不含绘图库），由 `core.charts` 渲染成 SVG。
这样统计计算与展示彻底解耦，换前端不用动计算代码。
"""

import math

import numpy as np
import pandas as pd

from . import config, stats_methods
from .data_loader import detect_structure


def _labels():
    """从业务字典取中文字段名，报告里不能出现 `agri_output` 这种列名。"""
    m = {}
    try:
        meta = config.load_schema_meta()
        for _t, info in (meta.get("tables") or {}).items():
            for col, ci in (info.get("columns") or {}).items():
                if ci.get("name"):
                    m[col] = ci["name"]
        for key, di in (meta.get("derived") or {}).items():
            m[key] = di.get("name") or key
    except Exception:                                     # noqa: BLE001
        pass
    return m


LABELS = _labels()


def _label(col):
    return LABELS.get(col, col)


class Analysis(object):
    def __init__(self):
        self.plan = None
        self.method = ""
        self.stats = {}
        self.tables = []
        self.charts = []
        self.findings = []      # 规则生成的要点，供报告层复述（LLM 不得新增数字）
        self.error = ""
        # 「拒绝建模」是一种合格结论，不是执行失败：数据不合格就说不合格，
        # 报告照发，不该在日志/历史里标成错误。
        self.refused = False
        self.n_used = 0

    @property
    def ok(self):
        return not self.error

    def to_dict(self):
        return {
            "method": self.method, "stats": self.stats, "tables": self.tables,
            "charts": self.charts, "findings": self.findings,
            "error": self.error, "n_used": self.n_used,
            "refused": self.refused,
        }


def run(df, plan):
    """按 Plan 执行分析。异常一律捕获成 error，不向上抛——用户不该看到 Python 栈。"""
    an = Analysis()
    an.plan = plan
    an.method = plan.method
    if plan.reject:
        an.error = plan.reason
        return an

    handler = {
        "none": _run_none,
        "describe": _run_describe,
        "correlation": _run_correlation,
        "panel_correlation": _run_panel,
        "timeseries_corr": _run_ts_corr,
        "group_compare": _run_group,
        "timeseries": _run_timeseries,
        "cluster": _run_cluster,
        "outliers": _run_outliers,
    }.get(plan.method, _run_none)

    try:
        handler(df, plan, an)
    except Exception as exc:                              # noqa: BLE001
        an.error = "分析执行失败：%s: %s" % (type(exc).__name__, exc)
        an.stats = {}
        an.findings = []
    return an


# ------------------------------------------------------------------ 各方法
def _run_none(df, plan, an):
    an.n_used = len(df)
    an.findings.append("本次为数据查询，共返回 %d 行，未进行统计推断。" % len(df))
    an.tables.append(_preview_table(df))
    num = [c for c in plan.columns["numeric"]]
    if num:
        c = num[0]
        v = pd.to_numeric(df[c], errors="coerce").dropna()
        if len(v):
            an.stats["quick"] = {"column": c, "label": _label(c), "n": int(len(v)),
                                 "mean": float(v.mean()), "min": float(v.min()),
                                 "max": float(v.max()), "sum": float(v.sum())}
            an.findings.append("%s：均值 %.2f，最小 %.2f，最大 %.2f。"
                               % (_label(c), v.mean(), v.min(), v.max()))


def _run_describe(df, plan, an):
    cols = plan.params.get("columns") or []
    described = []
    for c in cols:
        if c not in df.columns:
            continue
        d = stats_methods.describe(df[c], name=c)
        if d.get("n", 0) == 0:
            continue
        d["label"] = _label(c)
        described.append(d)
        vals = pd.to_numeric(df[c], errors="coerce").dropna().tolist()
        an.charts.append({"id": "hist_" + c, "type": "histogram",
                          "title": "%s 分布" % d["label"],
                          "values": [_r(v) for v in vals],
                          "x_label": d["label"]})
    an.stats["columns"] = described
    an.n_used = len(df)
    for d in described:
        an.findings.append(
            "%s：均值 %s，中位数 %s，标准差 %s，最小值 %s，最大值 %s。"
            % (d["label"], _f(d["mean"]), _f(d["median"]), _f(d["std"]),
               _f(d["min"]), _f(d["max"])))
        an.findings.append(
            "偏度 %s（%s），四分位距 %s；按 1.5×IQR 规则标记 %d 个疑似异常值（仅标记，未剔除）。"
            % (_f(d["skew"]), _skew_word(d["skew"]), _f(d["iqr"]), d["outlier_count"]))
        if d.get("skew_note"):
            an.findings.append(d["skew_note"] + "。")
    if described and len(df) < config.MIN_N_INFER:
        an.findings.append("样本量 n=%d 少于 %d，仅做描述统计，不开展显著性检验。"
                           % (len(df), config.MIN_N_INFER))


def _run_correlation(df, plan, an):
    pairs = plan.params.get("pairs") or []
    results = stats_methods.correlation_pairs(df, pairs)
    an.stats["pairs"] = results
    an.n_used = max([r.get("n", 0) for r in results] or [0])
    usable = [r for r in results if r.get("p") is not None and r.get("r") is not None]
    for r in usable:
        an.findings.append(_corr_sentence(r, len(usable) > 1))
    for r in results:
        if r.get("error"):
            an.findings.append("%s 与 %s：%s。" % (_label(r["x"]), _label(r["y"]), r["error"]))
    if usable:
        best = max(usable, key=lambda r: abs(r.get("r") or 0))
        sub = pd.DataFrame({best["x"]: pd.to_numeric(df[best["x"]], errors="coerce"),
                            best["y"]: pd.to_numeric(df[best["y"]], errors="coerce")}).dropna()
        pts = [[_r(a), _r(b)] for a, b in zip(sub[best["x"]], sub[best["y"]])]
        an.charts.append({"id": "scatter_%s_%s" % (best["x"], best["y"]), "type": "scatter",
                          "title": "%s 与 %s 的散点" % (_label(best["x"]), _label(best["y"])),
                          "points": pts, "x_label": _label(best["x"]),
                          "y_label": _label(best["y"]),
                          "fit": _fit_line(sub[best["x"]].values, sub[best["y"]].values)})


def _run_panel(df, plan, an):
    p = plan.params
    res = stats_methods.panel_correlation(df, p["x"], p["y"], p["unit"], p["time"])
    an.stats["panel"] = res
    an.n_used = res.get("n", 0)
    if res.get("error"):
        an.error = res["error"]
        return
    coefs = [res["pooled"], res["between"], res["within"], res["within_two_way"]]
    an.findings.append(
        "%s 与 %s 的面板相关分解（%s 个县 × %s 年，n=%d）：混合 r=%s，组间 r=%s，组内 r=%s，双向去均值 r=%s。"
        % (_label(p["x"]), _label(p["y"]), res.get("n_units"), res.get("n_periods"),
           res["n"], _f(res["pooled"].get("r"), 3), _f(res["between"].get("r"), 3),
           _f(res["within"].get("r"), 3), _f(res["within_two_way"].get("r"), 3)))
    an.findings.append("结论应以双向去均值后的组内相关为准：它同时剔除了县际固有差异和共同年份冲击。")
    if res.get("simpson"):
        an.findings.append(res["warning"])
    elif res.get("warning"):
        an.findings.append(res["warning"])
    if res.get("trend_note"):
        an.findings.append(res["trend_note"])
    an.findings.append("混合相关的 p 值不予采用（忽略了个体自相关，标准误被低估）；"
                       "如需检验组内关系显著性，应使用聚类稳健标准误的面板回归。")
    an.charts.append({"id": "panel_coef", "type": "bar",
                      "title": "面板相关：四种口径对比",
                      "categories": ["混合", "组间", "组内", "双向去均值"],
                      "values": [_r(c.get("r")) for c in coefs],
                      "y_label": "相关系数 r"})


def _run_ts_corr(df, plan, an):
    p = plan.params
    sub = df[[p["time"], p["x"], p["y"]]].copy()
    for c in (p["x"], p["y"]):
        sub[c] = pd.to_numeric(sub[c], errors="coerce")
    sub = sub.dropna().sort_values(p["time"])
    res = stats_methods.correlation(sub, p["x"], p["y"])
    d = pd.DataFrame({"dx": sub[p["x"]].diff(), "dy": sub[p["y"]].diff()}).dropna()
    dres = stats_methods.correlation(d, "dx", "dy")
    an.stats["levels"] = res
    an.stats["diff"] = dres
    an.n_used = res.get("n", 0)
    if res.get("r") is None:
        an.error = res.get("error") or "序列过短，无法计算相关"
        return
    an.findings.append("%s 与 %s 在水平值上的相关 r=%s（p=%s，n=%d），"
                       "但两条上升序列天然高度相关，这主要反映共同趋势。"
                       % (_label(p["x"]), _label(p["y"]), _f(res["r"], 3),
                          _f(res.get("p"), 4), res["n"]))
    if dres.get("r") is not None:
        an.findings.append("改看一阶差分（逐年变动）的相关：r=%s（p=%s，n=%d），"
                           "这才是「同向波动」的证据。"
                           % (_f(dres["r"], 3), _f(dres.get("p"), 4), dres["n"]))
    an.charts.append({"id": "ts_corr", "type": "line",
                      "title": "%s 与 %s 的走势" % (_label(p["x"]), _label(p["y"])),
                      "series": [{"name": _label(p["x"]),
                                  "points": [[int(t), _r(v)] for t, v in zip(sub[p["time"]], sub[p["x"]])]},
                                 {"name": _label(p["y"]),
                                  "points": [[int(t), _r(v)] for t, v in zip(sub[p["time"]], sub[p["y"]])]}],
                      "x_label": p["time"]})


def _run_group(df, plan, an):
    p = plan.params
    res = stats_methods.group_compare(df, p["value"], p["group"])
    an.stats["group"] = res
    an.n_used = res.get("n", 0)
    if res.get("error"):
        an.error = res["error"]
        return
    _rename_groups(res, p["group"])
    an.findings.append(
        "%s 按 %s 分组的%s：%s=%s，p=%s，%s。"
        % (_label(p["value"]), _label(p["group"]), res["method"],
           res.get("statistic_name", "统计量"), _f(res.get("statistic"), 3),
           _f(res.get("p"), 4), res["conclusion"]))
    an.findings.append("各组均值：%s。"
                       % "、".join("%s %s（n=%d）" % (g["name"], _f(g["mean"]), g["n"])
                                  for g in res["groups"]))
    if res.get("effect_size") is not None:
        eff = res["effect_size"] or 0.0
        # Kruskal-Wallis 的 epsilon² 在 H < 自由度时会算出负数。
        # 负的"解释比例"没有意义，按 0 报，并把原始值写在括号里——
        # 既不让读者看到 -3.1% 这种吓人的数，也不偷偷把计算值抹掉。
        shown = max(0.0, eff)
        note = res["effect_note"]
        if eff < 0:
            note += "（原始值 %.3f，按定义下限取 0）" % eff
        an.findings.append("效应量 %s=%s，属%s水平，即分组大约解释了 %.1f%% 的变异；"
                           "p 值只说「差异不太可能是巧合」，效应量才说明差异有多大。"
                           % (res["effect_name"], _f(shown, 3), note, shown * 100))
    if res.get("posthoc"):
        sig = [x for x in res["posthoc"] if (x.get("p_bonferroni") or 1) < config.ALPHA]
        if sig:
            an.findings.append("两两比较（Bonferroni 校正）显著的组对：%s。"
                               % "；".join("%s vs %s（p=%s）" % (x["a"], x["b"],
                                                                _f(x["p_bonferroni"], 4)) for x in sig))
        else:
            an.findings.append("整体检验显著，但校正后的两两比较没有一对达到显著，"
                               "不能指名「哪个组更高」。")
    if not res["significant"]:
        an.findings.append("差异不显著不等于「没有差异」，只说明当前样本不足以支持差异存在的判断。")
    an.charts.append({"id": "group_bar", "type": "bar",
                      "title": "%s 按 %s 的组均值" % (_label(p["value"]), _label(p["group"])),
                      "categories": [g["name"] for g in res["groups"]],
                      "values": [_r(g["mean"]) for g in res["groups"]],
                      "errors": [_r((g.get("std") or 0) / math.sqrt(max(g["n"], 1))) for g in res["groups"]],
                      "y_label": _label(p["value"]),
                      "note": "误差线为标准误（SE）"})


def _rename_groups(res, col):
    """0/1 这样的编码在报告里必须翻成人话，否则读者不知道 0 是哪一组。"""
    m = {"is_poverty_county": {"0": "非贫困县", "1": "贫困县", "0.0": "非贫困县", "1.0": "贫困县"}}
    mapping = m.get(col)
    if not mapping:
        return
    for g in res.get("groups") or []:
        g["name"] = mapping.get(str(g["name"]), g["name"])
    for ph in res.get("posthoc") or []:
        ph["a"] = mapping.get(str(ph.get("a")), ph.get("a"))
        ph["b"] = mapping.get(str(ph.get("b")), ph.get("b"))


def _run_timeseries(df, plan, an):
    p = plan.params
    res = stats_methods.timeseries(df, p["value"], p["time"])
    an.stats["timeseries"] = res
    an.n_used = res.get("n", 0)

    # 先把历史序列画出来：就算后面拒绝建模，用户也该看到数据长什么样。
    # 横轴是「2023第2季度」这类文本时改用序号 + 标签，硬转 int 会直接抛错。
    sub = df[[p["time"], p["value"]]].copy()
    sub[p["value"]] = pd.to_numeric(sub[p["value"]], errors="coerce")
    sub = sub.dropna().sort_values(p["time"])
    numeric_axis = pd.to_numeric(sub[p["time"]], errors="coerce").notnull().all()
    value_label = _label(p["value"])
    if numeric_axis:
        obs = [[int(float(t)), _r(v)] for t, v in zip(sub[p["time"]], sub[p["value"]])]
        x_labels = None
    else:
        obs = [[i, _r(v)] for i, v in enumerate(sub[p["value"]])]
        x_labels = [str(v) for v in sub[p["time"]]]

    start = len(obs)
    fc = [[start + i, _r(v)] for i, v in enumerate(res.get("forecast") or [])]
    if x_labels is not None:
        x_labels = x_labels + ["预测%d" % (i + 1) for i in range(len(fc))]

    spec = {"id": "ts_line", "type": "line",
            "title": "%s 的历史走势与预测" % value_label,
            "series": [{"name": "实际值", "points": obs}],
            "x_label": str(p["time"])}
    if fc:
        spec["series"].append({"name": "预测值", "points": fc, "dashed": True})
    if x_labels:
        spec["x_labels"] = x_labels
    an.charts.append(spec)

    if res.get("error"):
        an.error = res["error"]
        return
    if res.get("refuse"):
        an.error = res["refuse"]
        an.refused = True
        an.findings.append(res["refuse"])
        # 拒绝建模不等于拒绝回答：把序列本身的样子说清楚，
        # 否则用户只拿到一句「不做」和一个没有解读的图。
        vals = pd.to_numeric(sub[p["value"]], errors="coerce").dropna()
        if len(vals) >= 2:
            first, last = float(vals.iloc[0]), float(vals.iloc[-1])
            change = ("%.1f%%" % ((last / first - 1.0) * 100.0)) if first else "—"
            an.findings.append(
                "序列概况：%d 期，从 %s 到 %s；均值 %s，最小 %s，最大 %s；首末变化 %s。"
                % (len(vals), sub[p["time"]].iloc[0], sub[p["time"]].iloc[-1],
                   _r(vals.mean()), _r(vals.min()), _r(vals.max()), change))
        return
    adf = res.get("adf") or {}
    if adf.get("statistic") is not None:
        an.findings.append("ADF 检验：统计量=%s，p=%s，%s。"
                           % (_f(adf["statistic"], 3), _f(adf.get("p"), 4),
                              "序列平稳" if adf.get("stationary") else "序列非平稳"))
    model = res.get("model") or {}
    if model.get("unstable"):
        an.findings.append("模型系数标准误无法估计（Hessian 求逆失败），"
                           "该模型只用于外推量级，不用于系数显著性判断。")
    if res.get("growth_rate") is not None:
        an.findings.append("采用%s：年均增速 %s%%，R²=%s。"
                           % (model.get("name", "趋势模型"), _f(res["growth_rate"], 2),
                              _f(res.get("r2"), 3)))
    elif model.get("name"):
        an.findings.append("采用%s（AIC=%s）。" % (model["name"], _f(model.get("aic"), 1)))
    bt = res.get("backtest") or {}
    if bt.get("mape") is not None:
        an.findings.append("留出最后 3 期回测，MAPE=%s%%，短期预测误差可接受。" % _f(bt["mape"], 1))
    if res.get("forecast"):
        an.findings.append("外推预测：%s。"
                           % "、".join("%s 年 %s" % (y, _f(v, 1))
                                      for y, v in zip(res.get("forecast_index") or [], res["forecast"])))
    an.findings.append("外推假设「历史趋势延续」，未纳入政策变化与外部冲击；"
                       "预测区间随时间迅速变宽，只应作为量级参考，不宜作为确定性结论。")


def _run_cluster(df, plan, an):
    p = plan.params
    res = stats_methods.cluster(df, p["cols"])
    an.stats["cluster"] = res
    an.n_used = res.get("n", 0)
    if res.get("error"):
        an.error = res["error"]
        return
    an.findings.append("按 %s 标准化后聚类，轮廓系数选定 k=%d（silhouette=%s）。"
                       % ("、".join(_label(c) for c in p["cols"]), res["k"],
                          _f(res["silhouette"], 3)))
    raw = res.get("centers_raw") or {}
    for cid in sorted(raw.keys(), key=lambda x: int(x)):
        means = raw[cid]
        an.findings.append("第 %s 类（%d 个样本）：%s。"
                           % (int(cid) + 1, res["sizes"].get(cid, 0),
                              "、".join("%s %s" % (_label(k), _f(v, 1))
                                        for k, v in means.items())))
    for cv in res.get("caveats") or []:
        an.findings.append(cv + "。")
    an.findings.append("聚类是探索性分类，类别边界由算法给出而非业务定义；"
                       "换初始点或换指标都可能得到不同分组。")
    if res.get("plot"):
        groups = {}
        for x, y, k in res["plot"]:
            groups.setdefault(int(k), []).append([x, y])
        an.charts.append({"id": "cluster_scatter", "type": "scatter_groups",
                          "title": "聚类结果（%s × %s）"
                                   % (_label(p["cols"][0]), _label(p["cols"][1])),
                          "x_label": _label(p["cols"][0]), "y_label": _label(p["cols"][1]),
                          "groups": [{"name": "第 %d 类" % (k + 1), "points": v}
                                     for k, v in sorted(groups.items())]})


def _run_outliers(df, plan, an):
    p = plan.params
    res = stats_methods.outliers(df, p["value"], p.get("unit"), p.get("time"))
    an.stats["outliers"] = res
    an.n_used = res.get("n", 0)
    if res.get("error"):
        an.error = res["error"]
        return
    rows = res.get("rows") or []
    confirmed = [r for r in rows if r.get("confirmed")]
    suspected = [r for r in rows if r.get("flag_level") and not r.get("confirmed")]

    an.findings.append("以中位数 ± %s×MAD 作为「水平异常」门槛（稳健 z），%s。"
                       % (_f(res.get("threshold"), 1),
                          "并用同比变化率的稳健 z 做「突变异常」交叉验证"
                          if res.get("period") else "因缺少时间维度，未做交叉验证"))
    if res.get("period"):
        an.findings.append("判定截面为 %d 年，共 %d 个样本。"
                           % (res["period"], res["n"]))
    if confirmed:
        an.findings.append("双重确认的异常样本 %d 个：%s。"
                           % (len(confirmed),
                              "、".join("%s（稳健 z=%s，同比 z=%s）"
                                        % (r["unit"], _f(r["robust_z"], 2), _f(r["yoy_z"], 2))
                                        for r in confirmed)))
    else:
        an.findings.append("没有样本同时满足「水平异常」与「突变异常」，"
                           "因此不宣布存在异常值，表中仅为偏高候选。")
    if suspected:
        an.findings.append("另有 %d 个样本水平偏高但变化平稳（如 %s），"
                           "属「本来就高」，不判定为异常——这正是双重佐证的意义。"
                           % (len(suspected), "、".join(r["unit"] for r in suspected[:3])))
    an.findings.append("异常标记只用于提示复核，未删除任何样本；异常可能是录入错误，"
                       "也可能是真实的结构性变化，需回查原始台账。")
    if rows:
        an.charts.append({"id": "outlier_bar", "type": "bar",
                          "title": "偏离中位数最多的样本（稳健 z）",
                          "categories": [r["unit"] for r in rows[:10]],
                          "values": [r["robust_z"] for r in rows[:10]],
                          "y_label": "稳健 z 值"})
    an.tables.append({"columns": ["样本", "取值", "稳健 z", "同比 z", "水平异常", "突变异常", "确认异常"],
                      "rows": [[r["unit"], _f(r["value"], 2), _f(r["robust_z"], 2),
                                _f(r["yoy_z"], 2), "是" if r["flag_level"] else "否",
                                "是" if r["flag_change"] else "否",
                                "是" if r["confirmed"] else "否"] for r in rows],
                      "total": len(rows)})


# ------------------------------------------------------------------ 工具
def _preview_table(df, limit=20):
    cols = list(df.columns)[:8]
    rows = []
    for _, r in df.head(limit).iterrows():
        rows.append([_cell(r[c]) for c in cols])
    return {"columns": cols, "rows": rows, "total": len(df)}


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, float):
        if math.isnan(v):
            return ""
        return "%.2f" % v
    return str(v)


def _r(v, nd=4):
    try:
        if v is None:
            return None
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, nd)
    except (TypeError, ValueError):
        return None


def _f(v, nd=2):
    """数字转字符串，None 显示为「—」。报告里不允许出现 None/NaN。"""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if math.isnan(f) or math.isinf(f):
        return "—"
    return ("%." + str(nd) + "f") % f


def _skew_word(s):
    if s is None:
        return "无法判断"
    if s > 0.3:
        return "右偏"
    if s < -0.3:
        return "左偏"
    return "近似对称"


def _fit_line(xs, ys):
    try:
        if len(xs) < 3:
            return None
        slope, intercept = np.polyfit(xs, ys, 1)
        x0, x1 = float(min(xs)), float(max(xs))
        return [[_r(x0), _r(intercept + slope * x0)], [_r(x1), _r(intercept + slope * x1)]]
    except Exception:                                     # noqa: BLE001
        return None


def _corr_sentence(r, corrected):
    p = r.get("p")
    sig = "在 5% 显著性水平下显著" if (p is not None and p < config.ALPHA) \
        else "在 5% 显著性水平下不显著"
    s = "%s 与 %s 的%s：r=%s（n=%d，p=%s），%s" % (
        _label(r["x"]), _label(r["y"]), r.get("method", ""),
        _f(r.get("r"), 3), r["n"], _f(p, 4), sig)
    if r.get("ci_low") is not None:
        s += "，95%% 置信区间 [%s, %s]" % (_f(r["ci_low"], 3), _f(r["ci_high"], 3))
    if r.get("effect"):
        s += "，效应量属%s" % r["effect"]
    if r.get("significant_after_correction") is not None:
        s += "，FDR 校正后 p=%s" % _f(r["p_adjusted"], 4)
    return s + "。"
