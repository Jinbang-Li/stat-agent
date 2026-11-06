# -*- coding: utf-8 -*-
"""
统计方法路由：**按数据特征决策，不按问题措辞决策**。

和常见 NL2SQL 项目最大的区别就在这里。多数实现是：

    if intent == "corr": 算相关系数
    elif intent == "trend": 算时间序列

这是"意图分派"，不是"方法选择"。真正决定能不能做、该做什么的，是数据的形态：
    几个变量？什么类型？多少样本？是截面还是面板？序列平稳吗？组内样本够不够？

所以这里的顺序是：**先验数据，再定方法，验不过就拒绝**。
拒绝不是失败，是本系统最值钱的功能——一个会拒绝的系统才敢被用来做决策。

决策过程全程留痕（`steps`），界面上能逐步展开"为什么选了 Spearman 而不是 Pearson"。
"""

import numpy as np
import pandas as pd

from . import config
from .data_loader import (categorical_columns, detect_structure, numeric_columns)

# 不作为分析变量的技术列
ID_LIKE = ("county_id", "id", "unit_id", "year", "year_id")


class Plan(object):
    """路由决策结果。"""

    def __init__(self):
        self.intent = ""
        self.method = ""          # 内部方法键
        self.label = ""           # 展示名
        self.params = {}
        self.reject = False
        self.reject_code = ""
        self.reason = ""
        self.suggestion = ""
        self.steps = []           # 决策轨迹 [{"step","detail","ok"}]
        self.warnings = []
        self.structure = {}
        self.columns = {}
        self.notes = []

    def step(self, name, detail, ok=True):
        self.steps.append({"step": name, "detail": detail, "ok": bool(ok)})
        return self

    def reject_with(self, code, reason, suggestion=""):
        self.reject = True
        self.reject_code = code
        self.reason = reason
        self.suggestion = suggestion
        self.step("拒绝", reason, ok=False)
        return self

    def to_dict(self):
        return {
            "intent": self.intent, "method": self.method, "label": self.label,
            "params": self.params, "reject": self.reject,
            "reject_code": self.reject_code, "reason": self.reason,
            "suggestion": self.suggestion, "steps": self.steps,
            "warnings": self.warnings, "structure": self.structure,
            "columns": self.columns, "notes": self.notes,
        }


def classify_columns(df):
    """把列分成：时间、个体、数值指标、分类维度。

    这一步做错，后面全错。例如把 year 当数值变量去算"年份与GDP的相关"，
    得到 0.9 的相关系数，然后郑重宣布"年份显著影响 GDP"——这不是发现，是废话。
    """
    time_col = None
    unit_col = None
    for c in df.columns:
        low = c.lower()
        if low in ("year", "year_id") and df[c].dtype.kind in "if":
            time_col = c
            break
    for c in df.columns:
        low = c.lower()
        if low in ("county_name", "county_id", "name", "region", "province"):
            unit_col = c
            break

    num, cat = [], []
    for c in df.columns:
        if c == time_col:
            continue
        s = df[c]
        if s.dtype.kind in "if":
            uniq = s.nunique(dropna=True)
            # 二值/少取值整数列按分类处理
            if uniq <= 2 and float(s.dropna().isin([0, 1]).mean() if s.notnull().any() else 0) == 1.0:
                cat.append(c)
            elif uniq <= 1:
                continue
            else:
                num.append(c)
        else:
            if s.nunique(dropna=True) <= config.MAX_CATEGORIES:
                cat.append(c)

    return {"time": time_col, "unit": unit_col, "numeric": num, "categorical": cat}


def _missing_ok(df, col):
    if col not in df.columns or len(df) == 0:
        return False, "列不存在"
    rate = float(df[col].isnull().mean())
    if rate > config.MAX_MISSING_RATE:
        return False, "缺失率 %.0f%% 超过上限 %.0f%%" % (rate * 100,
                                                        config.MAX_MISSING_RATE * 100)
    return True, ""


def route(df, intent, question=""):
    """主入口。返回 Plan。"""
    plan = Plan()
    plan.intent = getattr(intent, "name", str(intent))
    plan.intent = plan.intent or "query"

    plan.step("读取结果集", "返回 %d 行 × %d 列" % (len(df), len(df.columns)))
    if len(df) == 0:
        return plan.reject_with("empty", "查询没有返回任何数据，无法分析",
                                "请放宽筛选条件（例如去掉年份或省份限制）后重试")

    cols = classify_columns(df)
    plan.columns = cols
    plan.structure = detect_structure(df, cols["unit"], cols["time"])
    plan.step("识别数据结构",
              "%s（%s）" % (plan.structure["kind"], plan.structure["reason"]))

    plan.notes = _missing_notes(df)

    handler = {
        "query": _route_query,
        "describe": _route_describe,
        "corr": _route_corr,
        "group": _route_group,
        "timeseries": _route_timeseries,
        "cluster": _route_cluster,
        "outlier": _route_outlier,
    }.get(plan.intent, _route_describe)
    return handler(df, plan, question)


# ------------------------------------------------------------------ 各意图
def _route_query(df, plan, question):
    plan.method = "none"
    plan.label = "数据查询"
    plan.step("方法选择", "查询类问题只返回结果集，不做统计推断")
    return plan


def _route_describe(df, plan, question):
    num = [c for c in plan.columns["numeric"] if _missing_ok(df, c)[0]]
    if not num:
        return plan.reject_with("no_numeric", "结果集中没有可分析的数值列",
                                "请指定一个具体指标（如收入、GDP、农业总产值）")
    plan.method = "describe"
    plan.label = "描述统计 + 分布诊断"
    plan.params = {"columns": num[:4]}
    plan.step("方法选择", "单变量/多变量描述统计，不做推断，不需要样本量门槛")
    if len(df) < config.MIN_N_INFER:
        plan.warnings.append("样本量 n=%d，仅做描述统计，不进行显著性检验" % len(df))
    return plan


def _route_corr(df, plan, question):
    num = [c for c in plan.columns["numeric"] if _missing_ok(df, c)[0]]
    plan.step("可用数值列", "、".join(num) or "无")

    if len(num) < 2:
        return plan.reject_with(
            "need_two_numeric",
            "相关分析至少需要两个数值变量，当前结果集只有 %d 个（%s）"
            % (len(num), "、".join(num) or "无"),
            "请在问题中同时指定两个指标，例如「居民收入与农业总产值」")

    # 样本量门槛：低于 30 的相关系数不可用于推断
    chosen = _prefer(num, question)
    sub = df[chosen[:2]].apply(pd.to_numeric, errors="coerce").dropna()
    n = len(sub)
    plan.step("样本量检查", "n=%d（推断门槛 %d）" % (n, config.MIN_N_INFER),
              ok=(n >= config.MIN_N_INFER))
    if n < config.MIN_N_INFER:
        return plan.reject_with(
            "sample_too_small",
            "有效样本 n=%d，少于 %d，不做显著性检验（此时 p 值不可靠，"
            "相关系数置信区间会宽到没有信息量）" % (n, config.MIN_N_INFER),
            "请扩大筛选范围（如不限定单个年份），或仅查看描述统计")

    if plan.structure["kind"] == "panel":
        plan.method = "panel_correlation"
        plan.label = "面板相关分解（混合 / 组间 / 组内）"
        plan.params = {"x": chosen[0], "y": chosen[1],
                       "unit": plan.structure["unit_col"],
                       "time": plan.structure["time_col"]}
        plan.step("方法选择",
                  "数据是面板（%d 县 × %d 年），不能只报一个混合相关系数："
                  "县际差异与县内变化是两类不同的变异，混算会低估标准误"
                  % (plan.structure.get("n_units") or 0, plan.structure["n_periods"]))
        plan.warnings.append("混合相关的 p 值不可直接采用（忽略了个体自相关，假阳性风险高）；"
                             "结论请以双向去均值后的组内相关为准")
        return plan

    if plan.structure["kind"] == "timeseries":
        plan.method = "timeseries_corr"
        plan.label = "时间序列相关（含趋势分离）"
        plan.params = {"x": chosen[0], "y": chosen[1], "time": plan.structure["time_col"]}
        plan.step("方法选择", "单对象多期数据，两条上升序列天然高度相关，"
                              "需同时报告去趋势后的相关")
        plan.warnings.append("时间序列的原始相关系数主要反映共同趋势，"
                             "不能作为「两者联动」的证据")
        return plan

    plan.method = "correlation"
    plan.label = "相关分析（按正态性自动选 Pearson / Spearman）"
    plan.params = {"pairs": _pair_list(chosen)}
    plan.step("方法选择", "截面数据，%d 个数值变量，可检验 %d 对相关关系"
              % (len(chosen), len(plan.params["pairs"])))
    if len(plan.params["pairs"]) > 1:
        plan.warnings.append("同时检验 %d 对相关，已对 p 值做 FDR 校正；"
                             "未经校正的 p 值不能直接当作显著性依据"
                             % len(plan.params["pairs"]))
    return plan


def _prefer(cols, question):
    """问题里点名的指标优先。

    用户问「收入和农业产值相关吗」，就不该把 GDP 也拉进来做第三对检验——
    多检一对，就多一次假阳性机会，还会把用户的注意力带偏。
    点名了几个就用几个；一个都没点名，才退回按列顺序取。
    """
    if not question:
        return list(cols)
    try:
        from .nlsql import detect_fields
        named = [f for f in detect_fields(question) if f in cols]
    except Exception:                                     # noqa: BLE001
        named = []
    return named or list(cols)


def _pair_list(num, limit=None):
    limit = limit or config.MAX_CORR_PAIRS
    pairs = []
    for i in range(len(num)):
        for j in range(i + 1, len(num)):
            pairs.append((num[i], num[j]))
            if len(pairs) >= limit:
                return pairs
    return pairs


def _route_group(df, plan, question):
    cat = [c for c in plan.columns["categorical"] if _missing_ok(df, c)[0]]
    num = [c for c in plan.columns["numeric"] if _missing_ok(df, c)[0]]
    plan.step("可用列", "分类：%s；数值：%s" % ("、".join(cat) or "无", "、".join(num) or "无"))
    if not cat:
        return plan.reject_with("no_category", "结果集中没有可用的分组变量",
                                "请指定分组维度，例如「按地形」「按省份」「按是否贫困县」")
    if not num:
        return plan.reject_with("no_numeric", "结果集中没有可分析的数值指标",
                                "请指定要比较的指标，例如「居民收入」")

    group_col = _pick_group_col(df, cat, question)
    value_col = num[0]

    counts = df[group_col].value_counts()
    big_enough = counts[counts >= config.MIN_GROUP_SIZE]
    plan.step("分组检查", "%d 个类别，其中 %d 个样本量≥%d（%s）"
              % (len(counts), len(big_enough), config.MIN_GROUP_SIZE,
                 "、".join("%s:%d" % (k, v) for k, v in counts.head(6).items())))
    if len(big_enough) < 2:
        return plan.reject_with(
            "groups_too_small",
            "有效分组不足 2 个：每组至少需要 %d 个样本，当前只有 %d 组达标"
            % (config.MIN_GROUP_SIZE, len(big_enough)),
            "请放宽筛选条件或换一个分组维度")
    if len(counts) > config.MAX_CATEGORIES:
        return plan.reject_with("too_many_groups",
                                "分组数 %d 超过 %d，不适合做组间比较"
                                % (len(counts), config.MAX_CATEGORIES),
                                "请先按业务规则合并类别")

    plan.method = "group_compare"
    plan.label = "组间差异检验（ANOVA / Kruskal-Wallis，按前提自动选择）"
    plan.params = {"value": value_col, "group": group_col}
    plan.step("方法选择", "先做 Shapiro 正态性与 Levene 方差齐性检验，"
                          "满足前提用 ANOVA，否则用 Kruskal-Wallis")
    if plan.structure["kind"] == "panel":
        plan.warnings.append("数据包含多个年份，同一年份内各值并非独立观测；"
                             "若结论用于政策判断，建议固定到单一截面年")
    return plan


def _pick_group_col(df, cat, question):
    """优先选问题里提到的维度，其次选类别数适中的。"""
    for c in cat:
        if c and c in question:
            return c
    for c in cat:
        k = df[c].nunique(dropna=True)
        if 2 <= k <= 8:
            return c
    return cat[0]


def _route_timeseries(df, plan, question):
    tcol = plan.columns["time"]
    num = [c for c in plan.columns["numeric"] if _missing_ok(df, c)[0]]
    if tcol is None:
        return plan.reject_with("no_time", "结果集中没有年份/时间列，无法做时序分析",
                                "请在问题中说明按年汇总，例如「各年 GDP 合计」")
    if not num:
        return plan.reject_with("no_numeric", "没有可建模的数值指标")
    periods = df[tcol].nunique(dropna=True)
    plan.step("时间点检查", "%d 个时间点（建模门槛 %d）" % (periods, config.MIN_N_INFER // 4),
              ok=periods >= 8)
    if periods < 8:
        return plan.reject_with("too_few_periods",
                                "只有 %d 个时间点，少于 8，无法识别趋势与自相关结构"
                                % periods,
                                "请改用描述统计，或扩大到更长的年度区间")
    if len(df) < config.MIN_N_MODEL:
        plan.warnings.append("总样本 %d 少于 %d，模型参数估计不稳定"
                             % (len(df), config.MIN_N_MODEL))

    plan.method = "timeseries"
    plan.label = "ADF 平稳性检验 + 趋势外推 / ARIMA"
    plan.params = {"value": num[0], "time": tcol, "periods": periods}
    plan.step("方法选择", "先做 ADF 检验；时间点 <15 用对数线性趋势（参数少、外推可控），"
                          "≥15 才用 ARIMA")
    return plan


def _route_cluster(df, plan, question):
    num = [c for c in plan.columns["numeric"] if _missing_ok(df, c)[0]]
    if len(num) < 2:
        return plan.reject_with("need_two_numeric", "聚类至少需要两个数值指标",
                                "例如「GDP、人口、第二产业占比」")
    sub = df[num].apply(pd.to_numeric, errors="coerce").dropna()
    plan.step("样本量检查", "n=%d（聚类门槛 20）" % len(sub), ok=len(sub) >= 20)
    if len(sub) < 20:
        return plan.reject_with("sample_too_small",
                                "有效样本 %d 少于 20，聚类结果随初始点变化剧烈，"
                                "不具有可解释性" % len(sub),
                                "请扩大样本范围（如不限定年份）")
    plan.method = "cluster"
    plan.label = "KMeans 聚类（标准化 + 轮廓系数选 k）"
    plan.params = {"cols": num[:4]}
    plan.step("方法选择", "对 %d 个指标标准化后聚类，k 在 2~6 中按轮廓系数挑选"
              % min(len(num), 4))
    plan.warnings.append("聚类结果是描述性的分类，不是总体的真实分组；"
                         "k 的选择带主观性，应结合业务可解释性")
    return plan


def _route_outlier(df, plan, question):
    num = [c for c in plan.columns["numeric"] if _missing_ok(df, c)[0]]
    if not num:
        return plan.reject_with("no_numeric", "没有可做异常诊断的数值指标")
    n = int(df[num[0]].notna().sum())
    plan.step("样本量检查", "n=%d（异常诊断门槛 8）" % n, ok=n >= 8)
    if n < 8:
        return plan.reject_with("sample_too_small",
                                "有效样本 %d 少于 8，中位数与 MAD 都不稳定，无法判断异常" % n,
                                "请扩大样本范围")
    plan.method = "outliers"
    plan.label = "双重佐证异常诊断（稳健 z + 同比跳变）"
    use_time = plan.structure["kind"] in ("panel", "timeseries") and plan.columns["time"]
    plan.params = {"value": num[0],
                   "unit": plan.columns["unit"],
                   "time": plan.columns["time"] if use_time else None}
    plan.step("方法选择",
              "截面稳健 z（MAD）判断「水平异常」，同比变化 z 判断「突变异常」，"
              "两者同时命中才判定为异常" if use_time else
              "仅有截面数据，只做稳健 z 判断，结论强度较弱")
    if not use_time:
        plan.warnings.append("缺少时间维度，无法区分「本来就高」和「突然变高」，"
                             "异常判定只能作为线索")
    return plan


def _missing_notes(df):
    notes = []
    for c in df.columns:
        rate = float(df[c].isnull().mean())
        if rate > 0:
            notes.append("%s 缺失 %.1f%%" % (c, rate * 100))
    return notes
