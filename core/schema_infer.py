# -*- coding: utf-8 -*-
"""表格结构与指标的自动识别。

Excel 通道没有表结构字典可用：列名是用户随手写的，「地区生产总值(万元)」、
「income」、「居民人均可支配收入(元)」都可能出现，列顺序也不保证。
这一层做三件事：

1. 认出列的**角色**——时间维度、分组维度、数值指标、个体编号；
2. 把问题里的指标词（「居民收入」）对到具体列（「居民人均可支配收入(元)」）；
3. 根据结构给出**默认分析方法**，让用户不必先学这套系统怎么说话。

刻意不做的事：不改数据、不填补、不删行，也**不因为识别不出来就瞎猜**——
认不出指标就明说，让用户补一句限定词，比算一个错的数好。
"""

import re

import pandas as pd

from . import config


# ---------------------------------------------------------------- 列名关键词

# 时间线索。注意「年产值」「年龄」不是时间，「年限」是时长。
TIME_HINTS = ("年份", "年度", "季度", "月份", "日期", "时间", "周期", "期间",
              "学年", "year", "date", "quarter", "month", "period", "time", "yr")
TIME_EXCLUDE = ("年龄", "工龄", "年限", "年产", "年产值", "年产量", "年均")

# 分组线索
GROUP_HINTS = ("县", "区", "地区", "城市", "地市", "省", "州", "区域", "类型",
               "类别", "分类", "地形", "行业", "部门", "性别", "分组", "组别",
               "等级", "档次", "品种", "品种", "渠道", "region", "city", "county",
               "province", "type", "category", "group", "sector", "industry")

# 编号线索：这些列只是主键/序号，拿去做相关分析毫无意义
ID_HINTS = ("id", "编号", "代码", "序号", "code")

# 指标词表：左边是用户会怎么说，右边是列名可能怎么写。
# 顺序有意义——「人均GDP」要排在「GDP」前面，否则会被更宽的词抢走。
METRIC_GROUPS = (
    ("gdp_pc", ("人均gdp", "人均生产总值", "人均地区生产总值"),
     ("人均", "percapita", "per_capita")),
    ("income", ("居民收入", "人均可支配", "可支配收入", "收入", "工资", "薪酬"),
     ("收入", "income", "可支配", "工资", "薪酬", "wage")),
    ("gdp", ("地区生产总值", "生产总值", "国内生产总值", "gdp", "经济总量"),
     ("gdp", "生产总值")),
    ("agri", ("农业总产值", "农业", "第一产业", "种植业", "农林牧渔"),
     ("农业", "第一产业", "种植", "农林牧渔", "agri", "farm")),
    ("finance", ("财政支出", "财政收入", "财政", "一般公共预算"),
     ("财政", "预算", "finance")),
    ("population", ("常住人口", "户籍人口", "人口"),
     ("人口", "population", "常住", "户籍")),
    ("industry", ("第二产业", "工业", "制造业"),
     ("第二产业", "工业", "制造", "secondary", "manufactur")),
    ("consumption", ("社会消费品零售", "消费", "零售"),
     ("消费", "零售", "consumption", "retail")),
    ("investment", ("固定资产投资", "投资"),
     ("投资", "investment", "固定资产")),
    ("output", ("产值", "产量", "产出"), ("产值", "产量", "产出", "output")),
    ("area", ("面积",), ("面积", "area")),
)


def norm(text):
    """统一大小写、去掉空格与括号，让「地区生产总值(万元)」和「地区生产总值」可比。"""
    s = str(text).lower()
    s = re.sub(r"[\s_\-—·]+", "", s)
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    return s


def _short(text):
    """展示用：去掉单位括号，保留原意。"""
    s = re.sub(r"[（(][^）)]*[）)]", "", str(text)).strip()
    return s or str(text)


def is_time_name(name):
    low = str(name).lower()
    if any(x in low for x in TIME_EXCLUDE):
        return False
    return any(x in low for x in TIME_HINTS)


def is_group_name(name):
    low = str(name).lower()
    return any(x in low for x in GROUP_HINTS)


def is_id_name(name):
    low = str(name).lower()
    if low in ("id", "year_id"):
        return False                      # year_id 是时间，不是编号
    return any(low.endswith(h) or low == h or ("_" + h) in low for h in ID_HINTS)


# ---------------------------------------------------------------- 取值判断

_YEAR_CELL = re.compile(r"^\s*(19|20)\d{2}\s*(年|$|[-/.])")
_QUARTER_CELL = re.compile(r"^\s*(19|20)\d{2}\s*[-/.年]?\s*(q[1-4]|第?[一二三四1-4]\s*季度)", re.I)
_MONTH_CELL = re.compile(r"^\s*(19|20)\d{2}\s*[-/.年]\s*\d{1,2}\s*月?\s*$")


def time_values_ok(series):
    """列名像时间，值也得像时间，否则「季度评分」这种列会被误判成时间轴。"""
    s = series.dropna()
    if len(s) == 0:
        return False
    if s.dtype.kind in "if":
        lo, hi = float(s.min()), float(s.max())
        return 1900 <= lo and hi <= 2100
    if str(s.dtype).startswith("datetime"):
        return True
    sample = [str(v) for v in s.head(50)]
    hit = sum(1 for v in sample if _YEAR_CELL.search(v) or _QUARTER_CELL.search(v))
    return hit >= max(1, int(len(sample) * 0.8))


def period_key(series):
    """把「2018第1季度」「2023年7月」这类文本转成可排序的键。

    返回 (键列表, ok)：ok=False 表示格式不统一，别硬排——排错了趋势就反了。
    """
    keys, seen = [], 0
    for v in list(series.dropna()):
        text = str(v)
        m = re.search(r"(19|20)\d{2}", text)
        if not m:
            keys.append(None)
            continue
        year = int(m.group(0))
        q = re.search(r"q([1-4])|第?([一二三四1-4])\s*季度", text, re.I)
        mo = re.search(r"[-/.年]\s*(\d{1,2})\s*月?\s*$", text)
        if q:
            idx = "一二三四".find(q.group(2)) + 1 if q.group(2) in "一二三四" else int(q.group(2) or q.group(1))
            keys.append(year * 10 + max(1, idx))
        elif mo:
            keys.append(year * 100 + int(mo.group(1)))
        else:
            keys.append(year * 1000)
        seen += 1
    ok = bool(keys) and all(k is not None for k in keys)
    return keys, ok


# ---------------------------------------------------------------- 角色识别

def _numeric_like(series):
    if series.dtype.kind in "if":
        return True
    conv = pd.to_numeric(series, errors="coerce")
    return float(conv.notnull().mean()) >= 0.9 and int(conv.notnull().sum()) >= max(3, len(series) // 10)


def _binary(series):
    s = pd.to_numeric(series, errors="coerce").dropna()
    return len(s) > 0 and set(pd.unique(s)).issubset({0, 1})


def usable_group(df, col):
    """能不能拿这一列做组间比较：类别数 2~30，且至少两组有 5 个样本。"""
    if col is None or col not in df.columns:
        return False
    s = df[col]
    k = int(s.nunique(dropna=True))
    if k < 2 or k > config.MAX_CATEGORIES:
        return False
    counts = s.value_counts()
    return int((counts >= config.MIN_GROUP_SIZE).sum()) >= 2


def _scan_year_like(df, skip):
    """列名没救时的兜底：整列是 1900–2100 的整数且取值不多，就当成年份。"""
    for c in df.columns:
        if c in skip:
            continue
        s = df[c]
        if s.dtype.kind not in "if":
            continue
        vals = s.dropna()
        if len(vals) == 0:
            continue
        if vals.min() >= 1900 and vals.max() <= 2100 and int(vals.nunique()) <= 200:
            return c
    return None


def guess_roles(df):
    """识别每个列的角色。返回的 dict 只含普通 Python 类型，能直接塞进 JSON。"""
    roles = {
        "time": None, "time_kind": "", "unit": None, "group": None,
        "groups": [], "metrics": [], "ids": [], "n_units": None, "n_periods": 0,
        "notes": [],
    }
    if df is None or len(df.columns) == 0:
        return roles

    for c in df.columns:
        if is_id_name(str(c)):
            roles["ids"].append(c)

    # ---- 时间
    for c in df.columns:
        if c in roles["ids"]:
            continue
        if is_time_name(str(c)) and time_values_ok(df[c]):
            roles["time"] = c
            roles["time_kind"] = "名字与取值都像时间"
            break
    if roles["time"] is None:
        cand = _scan_year_like(df, set(roles["ids"]))
        if cand is not None:
            roles["time"] = cand
            roles["time_kind"] = "取值落在 1900–2100，判定为年份"
    if roles["time"] is not None:
        roles["n_periods"] = int(df[roles["time"]].nunique(dropna=True))
        roles["notes"].append("时间列「%s」：%d 个时期（%s）"
                              % (_short(roles["time"]), roles["n_periods"],
                                 roles["time_kind"]))

    # ---- 分组 / 个体
    for c in df.columns:
        if c == roles["time"] or c in roles["ids"]:
            continue
        if is_group_name(str(c)) and usable_group(df, c):
            roles["groups"].append(c)
    if roles["groups"]:
        # 优先取类别少的那一列：3 个地形比 20 个城市更适合做组间比较
        roles["group"] = sorted(roles["groups"],
                                key=lambda c: int(df[c].nunique(dropna=True)))[0]
        roles["notes"].append("分组列「%s」：%d 个类别（另有候选：%s）"
                              % (_short(roles["group"]),
                                 int(df[roles["group"]].nunique(dropna=True)),
                                 "、".join(_short(c) for c in roles["groups"] if c != roles["group"]) or "无"))

    # 个体列：类别多到不能当分组，但样本确实按它重复出现（面板数据靠它识别）
    for c in df.columns:
        if c == roles["time"] or c in roles["ids"] or c in roles["groups"]:
            continue
        s = df[c]
        if s.dtype.kind in "if":
            continue
        k = int(s.nunique(dropna=True))
        if is_group_name(str(c)) and config.MAX_CATEGORIES < k <= 500:
            roles["unit"] = c
            roles["n_units"] = k
            roles["notes"].append("个体列「%s」：%d 个单位（不做分组，用于识别面板结构）"
                                  % (_short(c), k))
            break

    # ---- 指标
    for c in df.columns:
        if c in (roles["time"], roles["unit"]) or c in roles["ids"] or c in roles["groups"]:
            continue
        s = df[c]
        if not _numeric_like(s) or _binary(s):
            continue
        if int(s.nunique(dropna=True)) <= 1:
            continue
        if float(s.isnull().mean()) > config.MAX_MISSING_RATE:
            continue
        roles["metrics"].append(c)

    if roles["metrics"]:
        roles["notes"].append("数值指标：%s" % "、".join(_short(c) for c in roles["metrics"][:8]))
    else:
        roles["notes"].append("没有可用的数值指标（全是文本或缺失过多）")
    return roles


# ---------------------------------------------------------------- 指标匹配

def match_metric(question, columns):
    """把问题里的指标词对到列上。返回 (列名, 命中的说法, 得分)，认不出返回 (None, None, 0)。

    两条路：
    - 列名整段出现在问题里（「居民人均可支配收入」），最强证据，直接命中；
    - 否则借助同义词表：问题说「居民收入」，列叫「income(元)」也能对上。
    """
    cols = [c for c in (columns or [])]
    if not cols:
        return None, None, 0
    q = norm(question)
    if not q:
        return None, None, 0

    best, said, score = None, None, 0
    for c in cols:
        n = norm(c)
        if len(n) >= 2 and n in q:
            s = 1000 + len(n)
            if s > score:
                best, said, score = c, str(c), s
    if best is not None:
        return best, said, score

    for _key, q_aliases, col_words in METRIC_GROUPS:
        hit = ""
        for a in q_aliases:
            if norm(a) in q and len(norm(a)) > len(hit):
                hit = norm(a)
        if not hit:
            continue
        for c in cols:
            n = norm(c)
            for w in col_words:
                if norm(w) and norm(w) in n:
                    s = 100 + len(hit) + len(norm(w))
                    if s > score:
                        best, said, score = c, a, s
    return best, said, score


# ---------------------------------------------------------------- 方法推荐

def _usable_numeric(df, col):
    s = df[col]
    if float(s.isnull().mean()) > config.MAX_MISSING_RATE:
        return False
    return int(pd.to_numeric(s, errors="coerce").notnull().sum()) >= 3


# 比率/增速这类列是"派生指标"：它们适合回答"增速多少"，不适合代表"这个指标本身"。
# 表里同时有「生产总值」和「同比增值」时，默认该看前者。
_DERIVED_HINTS = ("率", "占比", "比重", "同比", "环比", "增速", "增长率", "%")


def is_derived(name):
    low = str(name).lower()
    return any(h in low for h in _DERIVED_HINTS)


def order_metrics(metrics):
    """把派生指标（率、占比、同比…）排到后面，水平值排前面。稳定排序，不改相对顺序。"""
    if not metrics:
        return []
    level = [c for c in metrics if not is_derived(c)]
    derived = [c for c in metrics if is_derived(c)]
    return level + derived


def recommend(df, roles=None, question=""):
    """按表结构给出默认分析方法，并说明为什么。

    决策顺序（结构优先，问题不明确时用）：
      有可用分组列      → 分组比较（面板数据自动只用最新一期）
      有 ≥8 期时间列    → 时序趋势
      有 ≥2 个数值指标  → 相关分析
      只有 1 个指标     → 描述统计

    这只是**默认值**：用户一句话说"看分布"或"预测"，就以用户为准。
    """
    roles = roles or guess_roles(df)
    metrics = order_metrics([c for c in roles["metrics"] if _usable_numeric(df, c)])
    out = {"intent": "describe", "reason": "", "roles": roles,
           "alternatives": [], "metrics": metrics}
    if not metrics:
        out["reason"] = "表里没有可分析的数值指标，只能列出数据概况"
        out["alternatives"] = [{"intent": "describe", "why": "先确认列里的数字是不是文本格式"}]
        return out

    first = _short(metrics[0])
    group = roles["group"]
    time_col = roles["time"]
    periods = int(roles["n_periods"] or 0)

    if group:
        k = int(df[group].nunique(dropna=True))
        out["intent"] = "group"
        out["reason"] = ("检测到分组列「%s」（%d 类），默认按它比较「%s」的组间差异"
                         % (_short(group), k, first))
        if time_col and periods >= 8:
            out["reason"] += "；这是面板数据，比较只取最新一期，避免同一单位多年重复计数"
        out["alternatives"].append({"intent": "corr", "why": "如果更关心指标之间的关系，可以问「%s和%s相关吗」" % (first, _short(metrics[1]) if len(metrics) > 1 else "另一个指标")})
        return out

    if time_col and periods >= 8:
        out["intent"] = "timeseries"
        out["reason"] = ("检测到时间列「%s」（%d 期），默认看「%s」随时间的趋势"
                         % (_short(time_col), periods, first))
        out["alternatives"].append({"intent": "describe", "why": "如果只想知道分布形态，可以问「%s的分布」" % first})
        return out

    if len(metrics) >= 2:
        out["intent"] = "corr"
        out["reason"] = ("没有可用的分组/时间维度，但有 %d 个数值指标，默认看「%s」与「%s」的相关性"
                         % (len(metrics), first, _short(metrics[1])))
        out["alternatives"].append({"intent": "describe", "why": "如果只想看分布，可以问「%s的分布」" % first})
        return out

    out["intent"] = "describe"
    out["reason"] = ("只有「%s」一个数值指标、也没有分组或时间维度，默认输出描述统计"
                     % first)
    return out


def auto_question(rec):
    """按结构推荐的结果，生成一句"人话版"的默认问题。

    用户没写问题时，报告标题、历史记录都用这句。它必须和实际做的方法一致：
    明明做了时序，标题却写"分布与基本统计特征"，读的人会以为系统答错了题。
    """
    roles = rec.get("roles") or {}
    metrics = [_short(m) for m in (rec.get("metrics") or [])]
    group = roles.get("group")
    intent = rec.get("intent")
    if intent == "group" and group and metrics:
        return "比较不同「%s」的「%s」差异" % (_short(group), metrics[0])
    if intent == "timeseries" and metrics:
        return "看看「%s」随时间的走势" % metrics[0]
    if intent == "corr" and len(metrics) >= 2:
        return "「%s」和「%s」有没有关系" % (metrics[0], metrics[1])
    if metrics:
        return "看看「%s」的分布和基本统计特征" % metrics[0]
    return "看看这组数据的分布和基本统计特征"


def pad_time_strings(df, col):
    """把「2023年7月」补成「2023年07月」，让字符串排序和真实时间一致。

    只在这一列确实是「年+月」格式时才动，且只改副本，不动用户的原表。
    """
    if col is None or col not in df.columns:
        return df
    s = df[col]
    if s.dtype.kind not in "o":
        return df
    values = [str(v) for v in s.dropna()]
    if not values:
        return df
    if not any(_MONTH_CELL.search(v) for v in values):
        return df
    if not all(_MONTH_CELL.search(v) for v in values):
        return df

    def _fix(v):
        if pd.isnull(v):
            return v
        text = str(v)
        text = re.sub(r"([-/.年]\s*)(\d)\s*月?",
                      lambda m: m.group(1) + ("0" + m.group(2) if len(m.group(2)) == 1 else m.group(2)),
                      text)
        return text

    out = df.copy()
    out[col] = [_fix(v) for v in out[col]]
    return out
