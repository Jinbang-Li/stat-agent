# -*- coding: utf-8 -*-
"""
NL2SQL 转换层。

分开三件事，避免互相污染：
    SQL 生成    LLM 优先，失败回退模板，再失败用最保守的兜底查询
    SQL 校验    交给 DataSource（四层防御在数据源里实现，见 datasource/sqlite_source.py）
    自我修复    只修一次。修两次以上说明模型根本没理解 schema，继续修只会烧钱

为什么"只修一次"是硬规则：LLM 在错误信息上迭代时，经常把一个语法错误改成
另一个语义错误（比如为了绕开 "no such column" 去猜一个相近的字段名）。
给它第二次机会的成本远大于收益，还不如退回模板——模板至少语义是对的。
"""

import re

from . import config, schema as schema_mod
from .datasource import DataSourceError, default_source
from .datasource.base import CheckResult
from .llm_client import LLMClient

OUT_OF_SCOPE = "OUT_OF_SCOPE"

# 兼容旧接口：白名单仍然导出，但真正的来源是 schema_meta.json 的 scope
_SRC = None


def _source(source=None):
    global _SRC
    if source is not None:
        return source
    if _SRC is None:
        _SRC = default_source()
    return _SRC


# ------------------------------------------------------------------ 文本清洗
def clean_sql_text(text):
    """模型常见的输出噪声：```sql 围栏、前缀说明、结尾分号、注释。

    模型经常把 SQL 包在解释里（"好的，SQL 如下：\\n```sql\\nSELECT …\\n```"）。
    这类输出逻辑是对的，只是格式不标准——应该把语句抽出来继续校验，
    而不是整段判失败再回退模板。回退模板意味着用另一个问题的答案冒充答案。
    """
    if not text:
        return ""
    t = text.strip()
    # 围栏可能在整段的任何位置，不只开头；取第一个完整的 fenced block
    fence = re.search(r"```(?:sql|SQL)?\s*\n?(.*?)```", t, re.S)
    if fence and fence.group(1).strip():
        t = fence.group(1).strip()
    t = re.sub(r"^```(?:sql|SQL)?\s*", "", t)
    t = re.sub(r"\s*```\s*$", "", t)
    t = re.sub(r"^(?:sql|SQL|答案|语句)\s*[:：]\s*", "", t)
    # 注释里可能藏关键字，也是注入常用手法，直接抹掉
    t = re.sub(r"/\*.*?\*/", " ", t, flags=re.S)
    t = re.sub(r"--[^\n]*", " ", t)
    t = t.strip()
    # 前面还有说明文字：从第一条 SELECT/WITH 开始截取。
    # 只往"前"截，不往"后"砍——后面的中文可能属于字符串字面量（'监利市'）。
    if not re.match(r"(?is)^\s*(select|with)\b", t):
        m = re.search(r"(?is)\b(select|with)\b", t)
        if m and m.start() > 0:
            t = t[m.start():]
    return t.strip().rstrip(";").strip()


# ------------------------------------------------------------------ 校验执行
def validate_and_execute(sql, source=None):
    """四层校验 + 执行。

    返回 (ok, columns, rows, check, note)。check 是 CheckResult，逐层可解释。
    """
    source = _source(source)
    sql = clean_sql_text(sql)
    check = source.check(sql)
    if not check.passed:
        layer = check.failed_layer
        return False, None, None, check, layer["note"] if layer else "校验未通过"
    try:
        result = source.execute(sql)
    except DataSourceError as e:
        # 执行期才失败的，补一层说明：文本层过了，但引擎或数据库拒了
        check.add("执行", False, str(e))
        return False, None, None, check, str(e)
    check.add("执行", True, "返回 %d 行，耗时 %.0f ms"
              % (result.row_count, result.elapsed_ms))
    return True, result.columns, result.rows, check, "OK"


# ------------------------------------------------------------------ 生成结果
class SQLResult(object):
    def __init__(self, sql, source, guard=None, note="", question="", attempts=0,
                 degraded=None, out_of_scope=False):
        self.sql = sql
        self.source = source          # 'llm' | 'template' | 'fallback' | 'none'
        self.guard = guard
        self.note = note
        self.question = question
        self.attempts = attempts
        self.degraded = degraded
        # 模型明确说"这个问题用现有字段答不了"。这跟"SQL 写错了"是两回事：
        # 前者不该回退模板——模板只能拼出一条回答了别的问题的 SQL。
        self.out_of_scope = out_of_scope
        self.columns = []
        self.rows = []

    @property
    def ok(self):
        return self.guard is not None and self.guard.passed

    def to_dict(self):
        return {
            "sql": self.sql,
            "source": self.source,
            "note": self.note,
            "attempts": self.attempts,
            "degraded": self.degraded,
            "checks": self.guard.to_list() if self.guard else [],
        }


# ---- 离线模板：没有模型时的兜底，也是"规则 + LLM 双控"中"规则"的一半

FIELD_ALIASES = {
    "gdp": ["gdp", "生产总值", "地区生产总值", "经济总量", "经济体量"],
    "income": ["收入", "可支配收入", "居民收入", "人均收入"],
    "agri_output": ["农业总产值", "农林牧渔", "农产值", "农业", "第一产业"],
    "finance_exp": ["财政支出", "预算支出", "财政"],
    "population": ["人口", "常住人口"],
    "secondary_ratio": ["第二产业", "工业化率", "产业占比"],
}


def detect_fields(question):
    """从问题里识别用户提到了哪些指标，按出现顺序返回。
    注意统一转小写：用户会写 "GDP"，别名表里是小写。"""
    text = (question or "").lower()
    found = []
    for field, alias in FIELD_ALIASES.items():
        for a in alias:
            if a in text:
                if field not in found:
                    found.append(field)
                break
    return found


def detect_year(question):
    m = re.search(r"(20\d{2})", question)
    return int(m.group(1)) if m else None


def _where(parts):
    return (" WHERE " + " AND ".join(parts)) if parts else ""


_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _top_n(question, default=5):
    """解析"前五 / 最高的三个县 / top10"里的 N。"""
    m = re.search(r"(?:前|最高的|最多的|最大的|最好的|top)\s*([一二三四五六七八九十]+|\d+)",
                  question or "", re.I)
    if not m:
        return default
    s = m.group(1)
    if s.isdigit():
        return max(1, min(int(s), 200))
    return _CN_NUM.get(s, default)


def _is_panel_question(q):
    """判断用户要的是"跨年份的面板"，而不是"某一年的截面"。

    这个区分不是措辞游戏：同一批县在 9 年里的县际差异与县内变化是两类变异，
    混在一起算相关会低估标准误（见 stats_methods.panel_correlation）。
    """
    return bool(re.search(r"20\d{2}\s*年?\s*[-~到至—]\s*年?\s*20\d{2}|面板|历年|各年|逐年|每一年|全时段",
                          q or ""))


# ------------------------------------------------------------------ 县名识别
_COUNTIES = {"names": [], "source_id": None}

# 出现这些词就是在问"一批县"，此时不该按某个县过滤
_PLURAL = ("各县", "各市", "每个县", "所有县", "全部县", "各county", "排名", "哪些县", "哪个县",
           "对比", "分布", "平均", "均值", "总和", "合计", "全部")


def county_names(source=None):
    """县名清单，按数据源缓存一次。

    用途有二：模板要把"监利市的 GDP"限定到该县（否则出来的全县均值会被当成
    监利市的数据）；以及用户提到一个库里没有的县名时，能明确说"没有这个县"，
    而不是悄悄回退到一个回答了别的问题的查询。
    """
    src = _source(source)
    if _COUNTIES["source_id"] == id(src) and _COUNTIES["names"]:
        return _COUNTIES["names"]
    names = []
    try:
        result = src.execute("SELECT DISTINCT county_name FROM county_economy "
                             "WHERE county_name IS NOT NULL ORDER BY county_name")
        names = [r[0] for r in result.rows if r and r[0]]
    except Exception:                                     # noqa: BLE001
        names = []
    _COUNTIES["names"] = names
    _COUNTIES["source_id"] = id(src)
    return names


def detect_county(question, source=None):
    """找出问题里点名的那个县。返回 (规范县名, 问题里出现的写法) 或 (None, None)。

    用"包含"而不是相等：用户常写"监利""枣阳"，库里的名字是"监利市""枣阳市"。
    取最长匹配，避免"监利"被"监利市"以外的短名误伤。
    """
    q = question or ""
    best = None
    for name in county_names(source):
        stem = name[:-1] if name.endswith(("县", "市", "区")) else name
        if stem and stem in q:
            if best is None or len(name) > len(best):
                best = name
    if not best:
        return None, None
    return best, best


def _county_cond(question, source=None):
    """问题只涉及一个县时，返回该县的过滤条件；否则返回空列表。"""
    q = question or ""
    if any(w in q for w in _PLURAL):
        return []
    name, _ = detect_county(q, source)
    if not name:
        return []
    return ["county_name = '%s'" % name.replace("'", "''")]


def build_template_sql(question, intent, source=None):
    """基于规则的 SQL 模板。用于无模型场景，也用于模型失败后的兜底。"""
    q = question or ""
    year = detect_year(q)
    fields = detect_fields(q)
    tnum = len(fields)
    panel = _is_panel_question(q)
    conds = []
    if year and not panel:
        conds.append("year = %d" % year)
    elif not year and not panel and intent not in ("timeseries", "outlier"):
        # 面板数据默认取最新一年：把 9 年混在一起算"平均/分布/相关"，
        # 等于把增长趋势当成了样本差异，n 也会虚高到 400+。
        conds.append("year = (SELECT MAX(year) FROM county_economy)")

    # 问题点名了某个县（"监利市"）就只取这个县。不加这条，"预测监利市 GDP"
    # 会退化成全县均值——一条回答了别的问题的 SQL，比报错更糟。
    # 只对"看单个对象的指标"生效；相关/分组/聚类/异常本来就是要跨县比较。
    if intent in ("query", "describe", "timeseries"):
        conds.extend(_county_cond(q, source))

    if intent in ("corr", "cluster"):
        if intent == "cluster" and tnum < 2:
            # 聚类默认按"经济结构"取列：产值、人口、工业化率。
            # 用收入+农业产值做聚类也能跑，但那更像在给"农业县/工业县"分类，
            # 而用户说"按经济特征"时想要的通常是规模与结构。
            cols = ["gdp", "population", "secondary_ratio"]
        else:
            cols = fields if tnum >= 2 else ["income", "agri_output"]
        keep = ["county_name"] + (["year"] if panel else []) + cols[:3]
        notnull = ["%s IS NOT NULL" % c for c in cols[:3]]
        # "GDP 最高的三个县做相关分析"这类问题：按用户说的样本范围取数交给下游判断。
        # 只取 3 行时统计路由会拒绝建模——这是设计意图，不是失败。
        top = _top_n(q, 0) if re.search(r"前\s*[一二三四五六七八九十\d]|最高|最大|top", q, re.I) else 0
        if top and intent == "corr":
            return ("SELECT %s FROM county_economy%s ORDER BY %s DESC LIMIT %d"
                    % (", ".join(keep), _where(conds + notnull), cols[0], top))
        return ("SELECT %s FROM county_economy%s LIMIT %d"
                % (", ".join(keep), _where(conds + notnull), config.SQL_MAX_ROWS))

    if intent == "group":
        metric = (fields or ["income"])[0]
        gcond = ["e.%s IS NOT NULL" % metric,
                 # 分组比较必须是同一年，否则 n 虚高、观测不独立，p 值会被压小
                 "e.year = %d" % year if year
                 else "e.year = (SELECT MAX(year) FROM county_economy)"]
        if "地形" in q or "山地" in q or "平原" in q or "丘陵" in q:
            return ("SELECT i.terrain, e.%s FROM county_economy e "
                    "JOIN county_info i ON e.county_id = i.county_id"
                    "%s LIMIT %d"
                    % (metric, _where(gcond), config.SQL_MAX_ROWS))
        if "贫困" in q or "脱贫" in q:
            return ("SELECT i.is_poverty_county, e.%s FROM county_economy e "
                    "JOIN county_info i ON e.county_id = i.county_id"
                    "%s LIMIT %d"
                    % (metric, _where(gcond), config.SQL_MAX_ROWS))
        return ("SELECT province, %s FROM county_economy%s LIMIT %d"
                % (metric, _where(conds + ["%s IS NOT NULL" % metric]),
                   config.SQL_MAX_ROWS))

    if intent == "timeseries":
        metric = (fields or ["gdp"])[0]
        # 指名了某个县就取该县序列；否则才是"全省/合计"意义下的加总
        county = _county_cond(q, source)
        agg = "SUM" if (not county and ("全省" in q or "合计" in q)) else "AVG"
        return ("SELECT year, ROUND(%s(%s), 2) AS value FROM county_economy%s "
                "GROUP BY year ORDER BY year LIMIT %d"
                % (agg, metric, _where(conds + ["%s IS NOT NULL" % metric]),
                   config.SQL_MAX_ROWS))

    if intent == "outlier":
        metric = (fields or ["gdp"])[0]
        # 这里刻意**不加** year = MAX(year) 的过滤：同比跳变要逐县按年算，
        # 只取一年就没有变化率可比了。异常诊断必须拿到时间序列本身。
        # 必须带上 year：没有时间维度就只能比"谁高"，抓不到"忽然变高"，
        # 而"本来就高"和"异常"是两回事。异常诊断的整个方法都依赖这一列。
        return ("SELECT county_name, year, %s FROM county_economy%s "
                "ORDER BY %s DESC LIMIT %d"
                % (metric, _where(conds + ["%s IS NOT NULL" % metric]),
                   metric, config.SQL_MAX_ROWS))

    if intent == "describe":
        metric = (fields or ["income"])[0]
        # 分布问题看最新一年的截面（conds 里已加），否则四分位数会被通胀与增长污染
        return ("SELECT county_name, %s FROM county_economy%s LIMIT %d"
                % (metric, _where(conds + ["%s IS NOT NULL" % metric]),
                   config.SQL_MAX_ROWS))

    # ---- query：判断顺序从"最具体"到"最笼统"，
    #      否则"各县农业总产值的平均值"会被"各县"误吞成明细查询。
    if tnum >= 1:
        metric = fields[0]
        # 人均 GDP 是派生指标：gdp 单位万元、population 单位万人，相除正好是元/人。
        # 必须排在"最高/最低"之前判断，否则"人均GDP最高的县"会被当成 GDP 排名。
        expr, order_col = metric, metric
        if metric == "gdp" and re.search(r"人均", q):
            expr, order_col = "ROUND(gdp / population, 2)", "gdp_per_capita"
        extra = ["%s IS NOT NULL" % metric]
        if order_col != metric:
            extra.append("population > 0")
        if re.search(r"最高|最多|最大|排名|前\s*[一二三四五六七八九十\d]|top", q, re.I):
            n = _top_n(q)
            return ("SELECT county_name, %s AS %s FROM county_economy%s "
                    "ORDER BY %s DESC LIMIT %d"
                    % (expr, order_col, _where(conds + extra), order_col, n))
        if re.search(r"最低|最少|最小", q):
            return ("SELECT county_name, %s AS %s FROM county_economy%s "
                    "ORDER BY %s ASC LIMIT %d"
                    % (expr, order_col, _where(conds + extra), order_col, _top_n(q)))
        if re.search(r"平均|均值|平均水平", q):
            return ("SELECT ROUND(AVG(%s), 2) AS avg_%s FROM county_economy%s LIMIT %d"
                    % (expr, order_col, _where(conds + extra),
                       config.SQL_MAX_ROWS))
        if re.search(r"合计|总(?!产值)|加总|总量", q):
            return ("SELECT ROUND(SUM(%s), 2) AS total FROM county_economy%s LIMIT %d"
                    % (metric, _where(conds + extra), config.SQL_MAX_ROWS))
        if re.search(r"各县|每个县|分县", q):
            return ("SELECT county_name, %s AS %s FROM county_economy%s "
                    "ORDER BY %s DESC LIMIT %d"
                    % (expr, order_col, _where(conds + extra), order_col,
                       config.SQL_MAX_ROWS))
        return ("SELECT ROUND(AVG(%s), 2) AS avg_%s FROM county_economy%s LIMIT %d"
                % (expr, order_col, _where(conds + extra), config.SQL_MAX_ROWS))

    # 什么都没识别出来：给一个安全的概览查询，让上层去判断
    return ("SELECT year, ROUND(AVG(gdp), 2) AS avg_gdp, COUNT(*) AS n "
            "FROM county_economy GROUP BY year ORDER BY year LIMIT %d"
            % config.SQL_MAX_ROWS)


_SQL_SYS_SUFFIX = """
另外补充四条本系统的硬性要求：
- 如果用户问的是"相关性 / 分组差异 / 异常 / 趋势"，请把后续统计分析需要的**原始行**都取出来
  （例如相关分析要同时取两列数值，不要只取一列均值；分组比较要取分组列和指标列）。
- 用户点名了某个县（如"监利市"）时，必须加 county_name 过滤，只取该县的数据。
  绝不能用"全县平均"代替某一个县——那是另一个问题的答案。
- 预测类问题（"预测未来三年…"）不需要你外推未来年份，也不要返回 OUT_OF_SCOPE：
  只把该对象的**历史序列**按年取出来，预测由系统后续的统计模块完成。
- 异常检测只取目标指标本身（例如 gdp），不要附加其他列的非空条件——
  多一个 IS NOT NULL 就会把缺了别的字段的异常样本筛掉。
"""


_CROSS_SECTIONAL = ("query", "describe", "group", "corr")

_YEAR_FILTER = "year = (SELECT MAX(year) FROM county_economy)"


def _enforce_latest_year(sql, intent, question):
    """把"默认取最新一年"这条口径落到 SQL 上，而不是靠模型记得住。

    面板数据混算多年度，会把"9 年的增长"当成"48 个县的差异"：n 从 48 变成
    420，p 值被压小，分组比较出现伪显著。模型有时会自己加年份条件、有时不加，
    同一句话两次运行结果不同——口径不能是这种概率性的东西。
    """
    if intent not in _CROSS_SECTIONAL:
        return sql
    if _is_panel_question(question):
        return sql
    if re.search(r"\byear\b\s*(=|>=|<=|>|<|\bin\b|\bbetween\b)", sql, re.I):
        return sql                       # 已经指定了年份
    if re.search(r"\bgroup\s+by\b[^;]*\byear\b", sql, re.I):
        return sql                       # 按年汇总，本来就要多年的

    m = re.search(r"\bwhere\b", sql, re.I)
    if m:
        return "%s %s AND%s" % (sql[:m.end()], _YEAR_FILTER, sql[m.end():])
    m = re.search(r"\b(order\s+by|group\s+by|limit)\b", sql, re.I)
    if m:
        return "%s WHERE %s%s" % (sql[:m.start()], _YEAR_FILTER, sql[m.start():])
    return sql + " WHERE " + _YEAR_FILTER


class NL2SQL(object):
    def __init__(self, llm=None, schema_prompt=None, source=None):
        self.llm = llm or LLMClient(quiet=True)
        self.source = _source(source)
        if schema_prompt:
            self.schema_prompt = schema_prompt
        else:
            # 运行时生成，而不是读一份可能已经过时的手写文件
            self.schema_prompt = schema_mod.build_prompt(
                self.source.introspect(), extra_rules=schema_mod.load_rules())

    def generate(self, question, intent, max_repair=1):
        """生成 SQL 并校验执行。返回 SQLResult（可能失败）。

        `intent` 既可以是 Intent 对象，也可以是意图名字符串——上游两种调用都出现过，
        这里统一归一化，避免模板分支拿到对象后静默走错分支。
        """
        name = getattr(intent, "name", intent) or "query"
        if self.llm.online:
            res = self._generate_by_llm(question, name, max_repair)
            if res is not None and res.ok:
                return res
            if res is not None and res.out_of_scope:
                # 模型明确说"答不了"：不要回退模板。模板只会拼出一条回答了
                # 另一个问题的 SQL，然后把它的结果当成答案交出去。
                res.degraded = res.degraded or "sql:out_of_scope"
                return res
            if res is not None:
                # 模型给了但没跑通：退回模板，并把模型的失败原因留在 note 里
                fb = self._from_template(question, name)
                fb.note = "模型生成的 SQL 未通过校验（%s），已回退模板" % res.note
                fb.degraded = "sql:llm_failed"
                return fb
        return self._from_template(question, name)

    def _from_template(self, question, intent):
        sql = build_template_sql(question, intent, source=self.source)
        ok, cols, rows, check, note = validate_and_execute(sql, self.source)
        if ok:
            res = SQLResult(sql, "template", check, "离线模板生成", question,
                            degraded=None if self.llm.online else "llm:offline")
            res.columns, res.rows = cols, rows
            return res
        fallback = ("SELECT year, ROUND(AVG(gdp), 2) AS avg_gdp, COUNT(*) AS n "
                    "FROM county_economy GROUP BY year ORDER BY year LIMIT 100")
        ok, cols, rows, check, note = validate_and_execute(fallback, self.source)
        res = SQLResult(fallback, "fallback", check, "兜底查询：" + note, question,
                        degraded="sql:fallback")
        res.columns, res.rows = cols, rows
        return res

    def _generate_by_llm(self, question, intent, max_repair):
        sys_prompt = self.schema_prompt + "\n" + _SQL_SYS_SUFFIX
        user = "用户问题：%s\n分析意图：%s" % (question, intent)
        sql = self.llm.try_chat(sys_prompt, user, temperature=0.0, max_tokens=400)
        if not sql:
            return None

        sql = clean_sql_text(sql)
        if OUT_OF_SCOPE in sql.upper():
            # 模型有时会把"预测/外推"误当成 SQL 不该干的事而直接拒答。
            # 给它一次机会说明白：只取历史序列，预测交给下游统计层。
            retry_user = (user + "\n\n注意：本系统的预测由后续统计模块完成，"
                          "你只需要把该对象的历史序列按年取出来。"
                          "请只输出一条 SELECT 语句；确实缺少字段时才输出 OUT_OF_SCOPE。")
            second = self.llm.try_chat(sys_prompt, retry_user, temperature=0.0,
                                       max_tokens=400)
            sql = clean_sql_text(second or "")
            if not sql or OUT_OF_SCOPE in sql.upper():
                check = CheckResult().add("范围校验", False, "模型判定为超出数据范围")
                return SQLResult("", "llm", check, "模型判定为超出数据范围",
                                 question, out_of_scope=True)

        for attempt in range(max_repair + 1):
            sql = _enforce_latest_year(sql, intent, question)
            ok, cols, rows, check, note = validate_and_execute(sql, self.source)
            if ok:
                res = SQLResult(sql, "llm", check, "OK", question, attempt)
                res.columns, res.rows = cols, rows
                return res
            if attempt >= max_repair:
                return SQLResult(sql, "llm", check, note, question, attempt)
            fix_user = ("你上一条 SQL 执行失败了，错误信息：%s\n原 SQL：\n%s\n"
                        "请修正后只输出一条新的 SELECT 语句。" % (note, sql))
            fixed = self.llm.try_chat(sys_prompt, fix_user, temperature=0.0,
                                      max_tokens=400)
            if not fixed:
                return SQLResult(sql, "llm", check, note, question, attempt)
            sql = clean_sql_text(fixed)
        return None
