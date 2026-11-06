# -*- coding: utf-8 -*-
"""
统计方法库：只算数，不下结论。

这一层是整个项目的立身之本，也是和"调 LLM 的壳"最本质的区别：
**所有统计量都由 scipy / statsmodels 算出，模型的输入是算好的数字，不是原始数据。**
模型没法在计算环节动手脚，它的自由度只剩下"怎么措辞"。

每条结果都返回一个 dict，里面必带三样东西：
    n          样本量（没有样本量的结论一律无效）
    method     具体用的哪个检验（不是"相关分析"这种笼统说法）
    assumptions 前提检验结果（正态性、方差齐性、平稳性）

效应量统一给出，因为"p<0.05"只说"不太可能是巧合"，不说"关系有多强"。
"""

import warnings

import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings("ignore")

try:
    from statsmodels.tsa.stattools import adfuller
    HAS_ADF = True
except Exception:                                         # noqa: BLE001
    HAS_ADF = False

try:
    from statsmodels.tsa.arima_model import ARIMA
    HAS_ARIMA = True
except Exception:                                         # noqa: BLE001
    HAS_ARIMA = False


# ------------------------------------------------------------------ 工具
def _r(v, nd=4):
    try:
        if v is None:
            return None
        f = float(v)
        if np.isnan(f) or np.isinf(f):
            return None
        return round(f, nd)
    except (TypeError, ValueError):
        return None


def effect_label(r):
    """相关系数的强弱口径，和 prompt/stats_guide.txt 里的定义保持一致。"""
    a = abs(r)
    if a < 0.20:
        return "极弱"
    if a < 0.40:
        return "弱"
    if a < 0.60:
        return "中等"
    if a < 0.80:
        return "较强"
    return "强"


def significance_text(p, alpha=0.05):
    if p is None:
        return "无法判断（样本不足）"
    if p < alpha:
        return "在 %.0f%% 显著性水平下显著（p=%.4g）" % (alpha * 100, p)
    return "在 %.0f%% 显著性水平下不显著（p=%.4g）" % (alpha * 100, p)


def fisher_ci(r, n, alpha=0.05):
    """相关系数的置信区间（Fisher z 变换）。n<4 时无意义。"""
    if n < 4 or r is None or abs(r) >= 1:
        return None, None
    z = np.arctanh(r)
    se = 1.0 / np.sqrt(n - 3)
    zcrit = stats.norm.ppf(1 - alpha / 2.0)
    return _r(np.tanh(z - zcrit * se)), _r(np.tanh(z + zcrit * se))


def _clean_pair(df, x, y):
    sub = df[[x, y]].apply(pd.to_numeric, errors="coerce").dropna()
    return sub


# ------------------------------------------------------------------ 描述统计
def describe(series, name=""):
    s = pd.to_numeric(series, errors="coerce").dropna()
    n = int(len(s))
    if n == 0:
        return {"name": name, "n": 0, "error": "该变量没有有效数值"}
    q1, q3 = np.percentile(s, [25, 75])
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    out = {
        "name": name,
        "n": n,
        "mean": _r(s.mean()),
        "std": _r(s.std()),
        "cv": _r(s.std() / s.mean()) if s.mean() else None,
        "min": _r(s.min()),
        "q1": _r(q1),
        "median": _r(s.median()),
        "q3": _r(q3),
        "max": _r(s.max()),
        "iqr": _r(iqr),
        "skew": _r(s.skew()) if n > 2 else None,
        "kurtosis": _r(s.kurtosis()) if n > 3 else None,
        "outlier_count": int(((s < lo) | (s > hi)).sum()),
        "outlier_rule": "IQR 1.5 倍",
    }
    # 偏度提示：|skew|>1 说明均值代表性差，报告里应提醒用中位数
    out["skew_note"] = ("分布明显偏斜，均值代表性较弱，建议同时看中位数"
                        if out["skew"] is not None and abs(out["skew"]) > 1 else "")
    return out


# ------------------------------------------------------------------ 相关
def correlation(df, x, y, method="auto"):
    """两变量相关。method: auto | pearson | spearman"""
    sub = _clean_pair(df, x, y)
    n = int(len(sub))
    result = {"x": x, "y": y, "n": n, "caveats": []}

    if n < 3:
        result.update({"error": "有效样本少于 3，无法计算相关系数", "n": n})
        return result

    xv = sub[x].values
    yv = sub[y].values
    if np.std(xv) == 0 or np.std(yv) == 0:
        result.update({"error": "存在取值恒定的变量，相关系数无定义"})
        return result

    normality = {}
    if n <= 5000:
        for nm, v in ((x, xv), (y, yv)):
            if len(np.unique(v)) > 2:
                try:
                    normality[nm] = _r(stats.shapiro(v[:5000])[1])
                except Exception:                          # noqa: BLE001
                    normality[nm] = None
    normal = all(v is not None and v > 0.05 for v in normality.values()) if normality else None

    if method == "auto":
        method = "pearson" if normal else "spearman"
    if method == "pearson":
        r, p = stats.pearsonr(xv, yv)
    else:
        r, p = stats.spearmanr(xv, yv)

    ci_lo, ci_hi = fisher_ci(r, n) if method == "pearson" else (None, None)
    result.update({
        "method": "Pearson 积矩相关" if method == "pearson" else "Spearman 秩相关",
        "method_key": method,
        "r": _r(r),
        "p": _r(p, 6),
        "abs_r": _r(abs(r)),
        "effect": effect_label(r),
        "direction": "正" if r > 0 else "负",
        "ci_low": ci_lo, "ci_high": ci_hi,
        "shapiro_p": normality,
        "normal": normal,
        "auto_reason": ("两变量均通过 Shapiro 正态性检验，用 Pearson"
                        if method == "pearson" else
                        "存在非正态变量（或样本过小），改用 Spearman 秩相关"),
    })
    if n < 30:
        result["caveats"].append("样本量 n=%d 偏小，相关系数估计不稳定，p 值效力不足" % n)
    if method == "spearman":
        result["caveats"].append("Spearman 度量的是单调关系强度，不是线性关系")
    return result


def correlation_pairs(df, pairs, alpha=0.05, correction="fdr_bh"):
    """多对相关 + 多重比较校正。

    为什么必须校正：检验 6 对变量、每对用 0.05，至少出现一个假阳性的概率是
    1-0.95^6 ≈ 26%。不校正就把"碰巧显著"当成发现，是分析报告里最常见的错误。
    """
    out = []
    for x, y in pairs:
        res = correlation(df, x, y)
        out.append(res)

    valid = [r for r in out if r.get("p") is not None]
    if len(valid) > 1:
        ps = [r["p"] for r in valid]
        if correction == "bonferroni":
            adj = [min(1.0, p * len(ps)) for p in ps]
        else:
            adj = _benjamini_hochberg(ps)
        for r, a in zip(valid, adj):
            r["p_adjusted"] = _r(a, 6)
            r["significant_after_correction"] = bool(a < alpha)
        for r in valid:
            r["n_comparisons"] = len(ps)
            r["correction"] = "Bonferroni" if correction == "bonferroni" else "Benjamini-Hochberg FDR"
    return out


def _benjamini_hochberg(ps):
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    adj = [0.0] * m
    prev = 1.0
    for rank in range(m - 1, -1, -1):
        i = order[rank]
        val = ps[i] * m / float(rank + 1)
        prev = min(prev, val)
        adj[i] = min(1.0, prev)
    return adj


# ------------------------------------------------------------------ 组间比较
def group_compare(df, value_col, group_col, alpha=0.05, min_group=5):
    """分类变量 × 连续变量：先验前提，再选 ANOVA 还是 Kruskal-Wallis。"""
    sub = df[[group_col, value_col]].copy()
    sub[value_col] = pd.to_numeric(sub[value_col], errors="coerce")
    sub = sub.dropna()
    result = {"value": value_col, "group": group_col, "n": int(len(sub)),
              "caveats": []}
    if len(sub) == 0:
        result["error"] = "没有有效数据"
        return result

    groups, names = [], []
    for g, part in sub.groupby(group_col):
        vals = part[value_col].values
        if len(vals) >= min_group:
            groups.append(vals)
            names.append(str(g))

    result["groups"] = []
    for nm, vals in zip(names, groups):
        result["groups"].append({"name": nm, "n": int(len(vals)),
                                 "mean": _r(np.mean(vals)),
                                 "median": _r(np.median(vals)),
                                 "std": _r(np.std(vals, ddof=1)) if len(vals) > 1 else None})
    result["n_groups"] = len(groups)

    if len(groups) < 2:
        result["error"] = ("有效分组少于 2 个（每组至少 %d 个样本），无法做组间比较"
                           % min_group)
        return result

    normality = {}
    for nm, vals in zip(names, groups):
        if len(vals) <= 5000 and len(np.unique(vals)) > 2:
            try:
                normality[nm] = _r(stats.shapiro(vals)[1])
            except Exception:                              # noqa: BLE001
                normality[nm] = None
    normal_ok = bool(normality) and all(v is not None and v > 0.05
                                        for v in normality.values())
    try:
        lev_p = _r(stats.levene(*groups)[1])
    except Exception:                                      # noqa: BLE001
        lev_p = None
    equal_var = lev_p is not None and lev_p > 0.05

    result["assumptions"] = {"shapiro_p": normality, "levene_p": lev_p,
                             "normal": normal_ok, "equal_variance": equal_var}
    result["group_means"] = dict((g["name"], g["mean"]) for g in result["groups"])

    if normal_ok and equal_var:
        f, p = stats.f_oneway(*groups)
        grand = np.concatenate(groups)
        ss_total = np.sum((grand - grand.mean()) ** 2)
        ss_between = sum(len(g) * (g.mean() - grand.mean()) ** 2 for g in groups)
        eta2 = ss_between / ss_total if ss_total else None
        result.update({"method": "单因素方差分析（One-way ANOVA）",
                       "statistic_name": "F", "statistic": _r(f), "p": _r(p, 6),
                       "effect_name": "eta²", "effect_size": _r(eta2),
                       "effect_note": _eta2_label(eta2)})
    else:
        h, p = stats.kruskal(*groups)
        k = len(groups)
        n_all = len(sub)
        eps2 = (h - k + 1) / float(n_all - k) if n_all > k else None
        result.update({"method": "Kruskal-Wallis 秩和检验",
                       "statistic_name": "H", "statistic": _r(h), "p": _r(p, 6),
                       "effect_name": "epsilon²", "effect_size": _r(eps2),
                       "effect_note": _eta2_label(eps2),
                       "why_nonparametric": ("正态性不满足" if not normal_ok
                                             else "方差不齐（Levene p=%.3g）" % (lev_p or 0))})

    result["significant"] = bool(result["p"] is not None and result["p"] < alpha)
    result["conclusion"] = significance_text(result["p"], alpha)

    # 显著才做两两比较，否则"哪个组更高"没有统计依据
    if result["significant"] and 2 <= len(groups) <= 5:
        pairs = []
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                try:
                    u, pij = stats.mannwhitneyu(groups[i], groups[j], alternative="two-sided")
                except Exception:                          # noqa: BLE001
                    continue
                pairs.append({"a": names[i], "b": names[j],
                              "p_raw": _r(pij, 6),
                              "p_bonferroni": _r(min(1.0, pij * len(groups) * (len(groups) - 1) / 2), 6)})
        result["posthoc"] = pairs
        result["caveats"].append("两两比较采用 Mann-Whitney U，并做了 Bonferroni 校正")
    if not result["significant"]:
        result["caveats"].append("整体检验不显著，不应解读「哪个组更高」，组间均值差异可能只是抽样波动")
    return result


def _eta2_label(v):
    if v is None:
        return ""
    a = abs(v)
    if a < 0.01:
        return "极弱"
    if a < 0.06:
        return "弱"
    if a < 0.14:
        return "中等"
    return "强"


# ------------------------------------------------------------------ 面板
def panel_correlation(df, x, y, unit_col, time_col):
    """面板数据的三种相关：混合、组间、组内。

    这是本系统最"专业"的一段代码。混合相关（pooled）把两类变异混在一起：
        - 组间变异：发达县收入高、农业占比低  -> 负相关
        - 组内变异：同一个县某年农业丰收，收入也涨 -> 正相关
    两者方向常常相反，混算出来的系数既不是前者也不是后者，
    而且因为忽略了组内自相关，标准误被低估，p 值假性变小。

    正确做法是分开报：组间（between）看结构差异，组内（within，即固定效应
    去均值后）看变化关系。如果两者符号相反，必须明确告诉用户"这是辛普森悖论"。
    """
    sub = df[[unit_col, time_col, x, y]].copy()
    for c in (x, y):
        sub[c] = pd.to_numeric(sub[c], errors="coerce")
    sub = sub.dropna()

    out = {"x": x, "y": y, "unit": unit_col, "time": time_col,
           "n": int(len(sub)), "caveats": []}
    if len(sub) < 10:
        out["error"] = "样本不足，无法做面板分解"
        return out

    pooled = correlation(sub, x, y)

    means = sub.groupby(unit_col)[[x, y]].mean()
    between = correlation(means.reset_index(), x, y)

    sub["_dx"] = sub[x] - sub.groupby(unit_col)[x].transform("mean")
    sub["_dy"] = sub[y] - sub.groupby(unit_col)[y].transform("mean")
    within = correlation(sub, "_dx", "_dy")
    within["x"], within["y"] = x, y

    # 双向去均值：再减掉年份均值，剔除"共同时间趋势"。
    # 只做个体去均值时，只要两个变量都在随时间上涨，组内相关就会虚高——
    # 那不是"同向联动"，只是"都在涨"。
    dx, dy = _two_way_demean(sub, x, y, unit_col, time_col)
    sub["_wx"], sub["_wy"] = dx, dy
    within2 = correlation(sub, "_wx", "_wy")
    within2["x"], within2["y"] = x, y
    within2["label"] = "双向去均值（个体 + 年份）"

    out.update({"pooled": pooled, "between": between, "within": within,
                "within_two_way": within2,
                "n_units": int(sub[unit_col].nunique()),
                "n_periods": int(sub[time_col].nunique())})

    pr, wr, w2 = pooled.get("r"), within.get("r"), within2.get("r")
    if pr is not None and w2 is not None and pr * w2 < 0:
        out["simpson"] = True
        out["warning"] = ("辛普森悖论：混合样本的相关系数为 %.3f，剔除个体与年份固定效应后为 %.3f，"
                          "方向相反。混合样本把「县际结构差异」和「县内时间变化」混在了一起，"
                          "不能用来回答「某县产值上升时收入会怎样」这类问题。" % (pr, w2))
    else:
        out["simpson"] = False
        if pr is not None and w2 is not None and abs(w2) - abs(pr) > 0.2:
            out["warning"] = ("混合相关（%.3f）与组内相关（%.3f）差距较大："
                              "县际差距和时间变化讲的是两件事，报告里应分别陈述。" % (pr, w2))
    if wr is not None and w2 is not None and abs(wr - w2) > 0.15:
        out["trend_note"] = ("只做个体去均值时相关为 %.3f，再剔除年份效应后降到 %.3f，"
                             "说明原相关主要来自共同时间趋势，而非两者的真实联动。" % (wr, w2))
    out["caveats"].append("面板数据不做混合 OLS 推断；组内相关按个体去均值，"
                          "双向去均值进一步剔除了共同时间趋势")
    return out


def _two_way_demean(sub, x, y, unit_col, time_col, iters=6):
    """双向固定效应去均值（不平衡面板用迭代法，平衡面板两次即收敛）。"""
    dx = sub[x].astype(float).copy()
    dy = sub[y].astype(float).copy()
    for _ in range(iters):
        dx = dx - dx.groupby(sub[unit_col]).transform("mean")
        dy = dy - dy.groupby(sub[unit_col]).transform("mean")
        dx = dx - dx.groupby(sub[time_col]).transform("mean")
        dy = dy - dy.groupby(sub[time_col]).transform("mean")
    return dx, dy


# ------------------------------------------------------------------ 时间序列
def timeseries(df, value_col, time_col, alpha=0.05):
    sub = df[[time_col, value_col]].copy()
    sub[value_col] = pd.to_numeric(sub[value_col], errors="coerce")
    sub = sub.dropna().sort_values(time_col)
    n = int(len(sub))
    out = {"value": value_col, "time": time_col, "n": n, "caveats": []}
    if n < 8:
        out["error"] = "时间点少于 8 个，不做时序建模（样本量不足以识别趋势）"
        return out

    series = pd.Series(sub[value_col].values, index=sub[time_col].values)

    if HAS_ADF:
        try:
            adf_stat, adf_p, used_lag, nobs, crit, _ = adfuller(series.values, autolag="AIC")
            out["adf"] = {"statistic": _r(adf_stat), "p": _r(adf_p, 6),
                          "used_lag": int(used_lag),
                          "critical": dict((k, _r(v)) for k, v in crit.items()),
                          "stationary": bool(adf_p < alpha)}
        except Exception as e:                             # noqa: BLE001
            out["adf"] = {"error": str(e)}
    else:
        out["adf"] = {"error": "statsmodels 不可用"}

    adf = out.get("adf", {})
    d = 0
    work = series.values.astype(float)
    if not adf.get("stationary", False):
        d = 1
        work = np.diff(work)
        if HAS_ADF and len(work) >= 8:
            try:
                p2 = adfuller(work, autolag="AIC")[1]
                out["adf_after_diff"] = {"p": _r(p2, 6), "stationary": bool(p2 < alpha)}
                if p2 >= alpha:
                    out["refuse"] = ("一阶差分后仍不平稳（ADF p=%.3f），"
                                     "不满足 ARIMA 的平稳性前提，拒绝建模。"
                                     "建议先检查数据口径是否变化，或改用更长的序列。" % p2)
                    return out
            except Exception:                              # noqa: BLE001
                pass

    if n < 15:
        # 9 个年度点去估 ARIMA(p,d,q)，参数根本不可识别：拟合能跑完，
        # 但系数标准误巨大、外推会指数级发散。这种情况下改用对数线性趋势，
        # 它只有一个斜率参数，等价于"假设保持历史平均增速"，外推可控且可解释。
        out["caveats"].append("时间点仅 %d 个，ARIMA 参数不可靠，改用对数线性趋势外推" % n)
        trend = _loglinear_trend(series)
        out["model"] = trend.get("model")
        if trend.get("error"):
            out["refuse"] = trend["error"]
            return out
        out["forecast"] = trend["values"]
        out["forecast_index"] = trend["index"]
        out["growth_rate"] = trend["growth_rate"]
        out["r2"] = trend["r2"]
        out["backtest"] = {"skipped": "样本不足 15 期，回测无统计意义"}
        out["caveats"].append("趋势外推假设「增速不变」；政策变化、口径调整都会让它失效，"
                              "预测只应作为量级参考")
        return out

    forecast, model_info = _fit_arima(series, d)
    out["model"] = model_info
    if forecast is None:
        out["refuse"] = "ARIMA 拟合失败（样本过短或序列退化），不输出预测"
        return out

    out["forecast"] = forecast["values"]
    out["forecast_index"] = forecast["index"]
    out["backtest"] = _backtest(series, d)
    out["caveats"].append("ARIMA 只刻画序列自身的惯性，不包含任何外生变量；"
                          "预测区间随步长迅速变宽，超过 3 期的点预测不应直接用于决策")
    return out


def _loglinear_trend(series):
    """log(y) = a + b*t，b 即年均复合增速。返回 3 期外推与拟合优度。"""
    values = series.values.astype(float)
    if np.any(values <= 0):
        return {"error": "序列含非正值，无法取对数做趋势外推"}
    t = np.arange(len(values), dtype=float)
    y = np.log(values)
    try:
        slope, intercept, r_value, p_value, std_err = stats.linregress(t, y)
    except Exception as e:                                 # noqa: BLE001
        return {"error": "趋势拟合失败：%s" % e}

    resid = y - (intercept + slope * t)
    s = float(np.std(resid, ddof=2)) if len(values) > 3 else float(np.std(resid))
    steps = 3
    fut_t = np.arange(len(values), len(values) + steps, dtype=float)
    pred_log = intercept + slope * fut_t
    pred = np.exp(pred_log)
    # 95% 预测区间：对数尺度上 ±1.96*s，再指数还原（对数正态）
    lo = np.exp(pred_log - 1.96 * s)
    hi = np.exp(pred_log + 1.96 * s)
    last = series.index[-1]
    try:
        last_t = int(float(last))
        idx = [last_t + i + 1 for i in range(steps)]
    except (TypeError, ValueError):
        idx = [len(values) + i + 1 for i in range(steps)]

    return {
        "values": [_r(v) for v in pred],
        "index": idx,
        "conf": [[_r(a), _r(b)] for a, b in zip(lo, hi)],
        "growth_rate": _r((np.exp(slope) - 1) * 100, 2),
        "r2": _r(r_value ** 2),
        "p": _r(p_value, 6),
        "model": {"name": "对数线性趋势（等价于固定年增速）",
                  "log_slope": _r(slope, 6),
                  "resid_sd": _r(s, 6),
                  "note": "年增速 %.2f%%，R²=%.3f" % ((np.exp(slope) - 1) * 100, r_value ** 2)},
    }


def _fit_arima(series, d):
    if not HAS_ARIMA:
        return None, {"error": "statsmodels ARIMA 不可用"}
    best = None
    last_err = ""
    for order in [(1, d, 0), (0, d, 1), (1, d, 1), (2, d, 0), (0, d, 0)]:
        try:
            model = ARIMA(series.values, order=order)
            res = model.fit(disp=0, trend="c" if d == 0 else "nc")
            if best is None or res.aic < best[0]:
                best = (res.aic, order, res)
        except Exception as e:                             # noqa: BLE001
            last_err = str(e)
            continue
    if best is None:
        return None, {"error": last_err or "所有候选阶数都拟合失败"}

    aic, order, res = best
    steps = 3
    values, index = [], []
    try:
        fc = res.forecast(steps)
        values = [_r(v) for v in np.asarray(fc[0]).ravel()]
        last_t = int(series.index[-1]) if str(series.index[-1]).isdigit() else len(series)
        index = [last_t + i + 1 for i in range(steps)]
        conf = None
        try:
            conf = [[_r(a), _r(b)] for a, b in np.asarray(fc[2]).reshape(-1, 2)]
        except Exception:                                  # noqa: BLE001
            conf = None
    except Exception as e:                                 # noqa: BLE001
        return None, {"error": "预测失败：%s" % e}

    return {"values": values, "index": index, "conf": conf}, {
        "order": list(order), "aic": _r(aic),
        "name": "ARIMA(%d,%d,%d)" % order,
        "params": len(getattr(res, "params", []) or []),
    }


def _backtest(series, d):
    """留出最后 3 个点做回测。样本太短就跳过——回测本身也需要样本。"""
    n = len(series)
    if n < 14:
        return {"skipped": "样本少于 14 个时间点，回测无统计意义"}
    try:
        train = series.values[:-3]
        truth = series.values[-3:]
        model = ARIMA(train, order=(1, d, 0))
        res = model.fit(disp=0, trend="c" if d == 0 else "nc")
        pred = np.asarray(res.forecast(3)[0]).ravel()
        mape = np.mean(np.abs((truth - pred) / truth)) * 100 if np.all(truth != 0) else None
        return {"mape": _r(mape, 2), "points": 3,
                "note": "留出最后 3 期做外推，MAPE 越小说明短期预测越可信"}
    except Exception as e:                                 # noqa: BLE001
        return {"error": str(e)}


# ------------------------------------------------------------------ 聚类
def cluster(df, cols, k_range=(2, 3, 4, 5, 6)):
    try:
        from sklearn.cluster import KMeans
        from sklearn.metrics import silhouette_score
        from sklearn.preprocessing import StandardScaler
    except Exception as e:                                 # noqa: BLE001
        return {"error": "scikit-learn 不可用：%s" % e}

    sub = df[list(cols)].apply(pd.to_numeric, errors="coerce").dropna()
    n = len(sub)
    out = {"cols": list(cols), "n": n, "caveats": []}
    if n < 20:
        out["error"] = "样本少于 20，聚类结果不稳定，拒绝建模"
        return out

    X = StandardScaler().fit_transform(sub.values)
    best = None
    scores = []
    for k in k_range:
        if k >= n:
            continue
        try:
            km = KMeans(n_clusters=k, n_init=10, random_state=42).fit(X)
            if len(set(km.labels_)) < 2:
                continue
            s = silhouette_score(X, km.labels_)
            scores.append({"k": k, "silhouette": _r(s)})
            if best is None or s > best[0]:
                best = (s, k, km)
        except Exception:                                  # noqa: BLE001
            continue

    if best is None:
        out["error"] = "所有候选 k 都聚类失败"
        return out

    s, k, km = best
    out.update({"k": k, "silhouette": _r(s), "candidate_scores": scores,
                "method": "KMeans（标准化后）+ 轮廓系数选 k"})
    if s < 0.25:
        out["caveats"].append("最佳轮廓系数仅 %.2f，说明样本本身没有清晰的分群结构，"
                              "分出来的类不宜当作真实类型使用" % s)

    centers = pd.DataFrame(km.cluster_centers_, columns=cols)
    out["centers_z"] = dict((str(i), dict((c, _r(centers.loc[i, c])) for c in cols))
                            for i in range(k))
    labels = km.labels_
    out["sizes"] = dict((str(i), int((labels == i).sum())) for i in range(k))
    means = sub.groupby(labels).mean()
    out["centers_raw"] = dict((str(i), dict((c, _r(means.loc[i, c])) for c in cols))
                              for i in range(k))
    # 前两个指标作为散点图坐标，供图表层直接使用，避免图表层再跑一次聚类
    if len(cols) >= 2:
        out["plot"] = [[_r(sub.iloc[i][cols[0]], 2), _r(sub.iloc[i][cols[1]], 2), int(labels[i])]
                       for i in range(len(sub))]
    return out


# ------------------------------------------------------------------ 异常
def outliers(df, value_col, unit_col=None, time_col=None, z_thresh=3.5):
    """双重佐证：截面稳健 z + 时序同比跳变。

    只用截面 z 会把"本来就富的县"全标成异常；
    只用同比会把"一次性波动"和"水平异常"混在一起。
    两个都命中，才叫异常。
    """
    sub = df.copy()
    sub[value_col] = pd.to_numeric(sub[value_col], errors="coerce")
    sub = sub.dropna(subset=[value_col])
    out = {"value": value_col, "n": int(len(sub)), "method": "稳健 z（MAD）+ 同比跳变",
           "caveats": []}
    if len(sub) < 8:
        out["error"] = "样本少于 8，无法做异常诊断"
        return out

    vals = sub[value_col].values.astype(float)
    med = np.median(vals)
    mad = np.median(np.abs(vals - med))
    scale = 1.4826 * mad if mad > 0 else np.std(vals)
    if scale == 0:
        out["error"] = "取值恒定，无法判断异常"
        return out
    sub["_rz"] = (vals - med) / scale
    sub["_flag_z"] = sub["_rz"].abs() > z_thresh

    # 时序：按单位算同比变化率的稳健 z
    if time_col and unit_col:
        try:
            sub = sub.sort_values([unit_col, time_col])
            sub["_pct"] = sub.groupby(unit_col)[value_col].pct_change()
            pct = sub["_pct"].dropna()
            if len(pct) >= 8 and pct.std() > 0:
                pm, ps = pct.median(), 1.4826 * np.median(np.abs(pct - pct.median()))
                if ps > 0:
                    sub["_pz"] = (sub["_pct"] - pm) / ps
                    sub["_flag_yoy"] = sub["_pz"].abs() > z_thresh
            sub["_flag_yoy"] = sub.get("_flag_yoy", pd.Series(False, index=sub.index)).fillna(False)
        except Exception:                                  # noqa: BLE001
            sub["_flag_yoy"] = False
    else:
        sub["_flag_yoy"] = False

    sub["_flag_both"] = sub["_flag_z"] & sub["_flag_yoy"]

    latest = None
    if time_col:
        latest = sub[time_col].max()
        view = sub[sub[time_col] == latest]
    else:
        view = sub

    rows = []
    view = view.copy()
    view["_abs_rz"] = view["_rz"].abs()
    for _, r in view.sort_values("_abs_rz", ascending=False).head(10).iterrows():
        rows.append({
            "unit": str(r[unit_col]) if unit_col else "-",
            "value": _r(r[value_col]),
            "robust_z": _r(r["_rz"], 2),
            "yoy_z": _r(r.get("_pz")) if "_pz" in r and r.get("_pz") is not None else None,
            "flag_level": bool(r["_flag_z"]),
            "flag_change": bool(r["_flag_yoy"]),
            "confirmed": bool(r["_flag_both"]),
        })

    out["rows"] = rows
    out["threshold"] = z_thresh
    out["period"] = None if latest is None else int(latest)
    out["n_level_flag"] = int(sub["_flag_z"].sum())
    out["n_change_flag"] = int(sub["_flag_yoy"].sum())
    out["n_confirmed"] = int(sub["_flag_both"].sum())
    if out["n_confirmed"] == 0:
        out["caveats"].append("没有样本同时满足「水平异常」和「变化异常」，"
                              "因此不建议直接宣布存在异常值；表中仅是偏高/偏低的候选")
    out["caveats"].append("异常不等于错误：可能是真实的高增长县，也可能是口径调整或录入错误，需回查原始台账")
    return out
