# -*- coding: utf-8 -*-
"""
数据获取与清洗层。

这一层**完全由代码规则控制，不经模型**。原因很直接：
让模型决定"删掉哪些样本"，等于让它在没有任何统计训练的情况下做样本选择，
删错了不会报错，只会让后面的检验悄悄失去代表性。

三条硬规则：
1. **不静默丢样本**。任何一次 drop / 截断都要进 cleaning_notes，报告里会写明。
2. 缺失只做统计和提示，不擅自填补。填补会让方差失真。
3. 异常值只**标记**不删除。IQR 标记出来的点，交给统计层用稳健方法处理，
   或者交给用户判断——而不是在这里悄悄抹掉。
"""

import numpy as np
import pandas as pd

NUMERIC_DTYPES = ("int", "float")


def to_frame(columns, rows):
    """把 ExecResult 的列/行转成 DataFrame，并做保守的类型推断。

    SQLite 的 REAL 列里可能混进 None；这里统一转成 NaN 让 pandas 处理，
    但**不做任何 int/float 强转**——强转会把 None 变成 0，那是灾难。
    """
    df = pd.DataFrame(rows, columns=columns)
    for c in df.columns:
        if df[c].dtype == object:
            converted = pd.to_numeric(df[c], errors="coerce")
            # 只有当"能转的比例足够高"才认为是数值列，否则保持原样（可能是县名）
            non_null = df[c].notnull().sum()
            if non_null > 0 and converted.notnull().sum() >= non_null * 0.9:
                df[c] = converted
    return df


def numeric_columns(df):
    return [c for c in df.columns if df[c].dtype.kind in "if"]


def categorical_columns(df):
    return [c for c in df.columns if df[c].dtype.kind not in "if"]


def describe_column(series):
    """单列画像。数值列给分布统计，文本列给取值概况。"""
    n = int(series.notnull().sum())
    total = int(len(series))
    out = {
        "missing": total - n,
        "missing_rate": round((total - n) / float(total), 4) if total else 0.0,
        "n_unique": int(series.nunique(dropna=True)),
    }
    if series.dtype.kind in "if" and n > 0:
        s = series.dropna()
        q1, q3 = np.percentile(s, [25, 75])
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        out.update({
            "type": "numeric",
            "min": _round(s.min()), "max": _round(s.max()),
            "mean": _round(s.mean()), "std": _round(s.std()),
            "median": _round(s.median()),
            "q1": _round(q1), "q3": _round(q3),
            "skew": _round(s.skew()) if n > 2 else None,
            "iqr_outliers": int(((s < lo) | (s > hi)).sum()),
        })
    else:
        top = series.dropna().value_counts().head(5)
        out.update({
            "type": "categorical",
            "top_values": [[str(k), int(v)] for k, v in top.items()],
        })
    return out


def _round(v, nd=4):
    try:
        if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
            return None
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def profile(df):
    """整体画像。报告里的"数据概览"直接由它渲染，不让模型自由发挥。"""
    notes = []
    total = int(len(df))
    cols = {}
    for c in df.columns:
        cols[c] = describe_column(df[c])

    # 重复行：只提示，不删除——重复可能是合法的（同一县同一年两条记录才是问题）
    dup = int(df.duplicated().sum()) if total else 0
    if dup and dup < total:
        notes.append("存在 %d 行完全重复的记录，未自动删除，请确认是否为录入问题" % dup)

    high_missing = [c for c, v in cols.items() if v["missing_rate"] > 0.3]
    if high_missing:
        notes.append("以下列缺失率超过 30%，不参与统计推断：%s" % "、".join(high_missing))

    constant = [c for c in numeric_columns(df) if df[c].nunique(dropna=True) <= 1]
    if constant:
        notes.append("以下数值列取值恒定，无法参与相关/检验：%s" % "、".join(constant))

    return {
        "n_rows": total,
        "n_cols": int(len(df.columns)),
        "duplicated_rows": dup,
        "columns": cols,
        "notes": notes,
    }


def detect_structure(df, unit_hint=None, time_hint=None):
    """判断这批数据是截面、面板还是时间序列。

    这是一个容易被忽略但非常关键的判断：
    对面板数据直接算 Pearson，会把"县际差异"和"县内变化"混在一起，
    标准误被严重低估，p 值假性变小。必须先识别出结构，再决定算法。
    """
    time_col = time_hint or _guess_time_col(df)
    unit_col = unit_hint or _guess_unit_col(df)
    info = {"time_col": time_col, "unit_col": unit_col}

    if time_col and df[time_col].notnull().any():
        years = sorted(pd.unique(df[time_col].dropna()))
        info["n_periods"] = len(years)
        info["periods"] = [int(y) for y in years if str(y).replace(".0", "").isdigit()][:20]
    else:
        info["n_periods"] = 1
        info["periods"] = []

    if unit_col:
        info["n_units"] = int(df[unit_col].nunique(dropna=True))
    else:
        info["n_units"] = None

    if info["n_periods"] >= 3 and (info["n_units"] or 0) >= 5:
        info["kind"] = "panel"
        info["reason"] = "检测到 %d 个单位 × %d 个时期" % (info["n_units"], info["n_periods"])
    elif info["n_periods"] >= 5 and (info["n_units"] or 1) <= 1:
        info["kind"] = "timeseries"
        info["reason"] = "单个对象、%d 个时期" % info["n_periods"]
    else:
        info["kind"] = "cross_section"
        info["reason"] = "单一截面（%d 个观测）" % len(df)
    return info


def _guess_time_col(df):
    for c in df.columns:
        low = c.lower()
        if low in ("year", "year_id", "yyyy") or low.endswith("_year"):
            return c
    for c in df.columns:
        if df[c].dtype.kind in "if":
            vals = df[c].dropna()
            if len(vals) and vals.min() >= 1900 and vals.max() <= 2100:
                return c
    return None


def _guess_unit_col(df):
    for c in df.columns:
        low = c.lower()
        if low in ("county_id", "county_name", "id", "unit_id", "region", "province", "city"):
            return c
    return None


def drop_missing(df, cols):
    """只对**本次分析真正用到的列**删除缺失，返回 (新 df, 删除行数)。
    绝不对整张表 dropna，那样会因为一个无关列缺失而丢掉大量有效样本。"""
    cols = [c for c in cols if c in df.columns]
    if not cols:
        return df, 0
    before = len(df)
    out = df.dropna(subset=cols)
    return out, before - len(out)


def flag_iqr_outliers(series):
    """返回布尔掩码，标记 IQR 意义上的离群点。只标记，不删除。"""
    s = series.dropna()
    if len(s) < 4:
        return pd.Series(False, index=series.index)
    q1, q3 = np.percentile(s, [25, 75])
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return (series < lo) | (series > hi)


def cleaning_notes(df, used_cols):
    """生成"清洗说明"。报告里必须原样带上，这样读者知道结论建立在多少样本上。"""
    notes = []
    for c in used_cols:
        if c not in df.columns:
            continue
        miss = int(df[c].isnull().sum())
        if miss:
            notes.append("%s 有 %d 个缺失值，在本次分析中按行剔除" % (c, miss))
    return notes


def sample_rows(df, n=8):
    """给报告展示用的前 n 行（转成 JSON 安全的类型）。"""
    head = df.head(n)
    rows = []
    for _, r in head.iterrows():
        row = {}
        for c in df.columns:
            v = r[c]
            if isinstance(v, (np.integer,)):
                v = int(v)
            elif isinstance(v, (np.floating,)):
                v = None if np.isnan(v) else round(float(v), 4)
            elif isinstance(v, float) and np.isnan(v):
                v = None
            else:
                v = str(v)
            row[c] = v
        rows.append(row)
    return rows
