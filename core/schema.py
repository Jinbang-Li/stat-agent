# -*- coding: utf-8 -*-
"""
Schema 自动发现与一致性校验。

商业系统里最容易腐坏的东西不是代码，是**提示词和真实表结构的漂移**：
别人给表加了一列、改了个字段名，手写的 schema 提示词就过时了，
于是模型开始编造字段，报错，然后你以为是模型不行。

所以这里做两件事：
1. 启动时从数据库 introspect() 出真实结构，**运行时拼提示词**，不依赖手写文件；
2. 把它和业务字典（data/schema_meta.json）对账，不一致就报出来：
   - 库里有、字典没写 -> 该列对模型不可见（保守但安全），提示补字典
   - 字典写了、库里没有 -> 提示词会写进一个不存在的列，必须阻断启动
   - 类型不符（字典说 int，库里是 text）-> 告警，通常是建表时就错了

校验失败的处理策略是分级的：
    ERROR   阻断启动（否则模型必然生成错 SQL，越早失败越好）
    WARN    继续跑，但写进健康检查结果，界面上能看到
"""

import json
import os

from . import config

_NUMERIC_HINTS = ("INT", "REAL", "FLOA", "DOUB", "DEC", "NUM", "BIGINT", "SMALLINT",
                  "TINYINT", "NUMERIC", "SERIAL")
_TEXT_HINTS = ("CHAR", "TEXT", "CLOB", "VARCHAR", "STRING")
_TIME_HINTS = ("DATE", "TIME")


def _type_family(sql_type):
    t = (sql_type or "").upper()
    for h in _TIME_HINTS:
        if h in t:
            return "time"
    for h in _NUMERIC_HINTS:
        if h in t:
            return "numeric"
    for h in _TEXT_HINTS:
        if h in t:
            return "text"
    return "other"


class SchemaIssue(object):
    def __init__(self, level, table, column, message):
        self.level = level
        self.table = table
        self.column = column
        self.message = message

    def to_dict(self):
        return {"level": self.level, "table": self.table,
                "column": self.column, "message": self.message}

    def __repr__(self):
        return "[%s] %s.%s %s" % (self.level, self.table, self.column, self.message)


def validate(tables, meta=None):
    """把真实结构与业务字典对账。返回 [SchemaIssue]。"""
    meta = config.load_schema_meta() if meta is None else meta
    issues = []
    dictionary = (meta or {}).get("tables") or {}
    scope = (meta or {}).get("scope") or {}

    db_tables = dict((t.name.lower(), t) for t in tables)

    for tname in scope:
        if tname.lower() not in db_tables:
            issues.append(SchemaIssue("ERROR", tname, "*",
                                      "字典里声明了这张表，但数据库里不存在"))
            continue
        tinfo = db_tables[tname.lower()]
        declared = dictionary.get(tname, {}).get("columns") or {}
        db_cols = dict((c.name.lower(), c) for c in tinfo.columns)

        for cname in declared:
            if cname.lower() not in db_cols:
                issues.append(SchemaIssue("ERROR", tname, cname,
                                          "字典里声明了这一列，但数据库里没有"))
        for cname, cinfo in db_cols.items():
            if cname not in [d.lower() for d in declared]:
                issues.append(SchemaIssue("WARN", tname, cname,
                                          "数据库里有这列，但业务字典没写含义，模型不会看到它"))
        # 类型粗校验：字典里带 unit=万元/元/% 的，库里应该是数值型
        for cname, cinfo in db_cols.items():
            spec = declared.get(cname) or {}
            unit = (spec.get("unit") or "").strip()
            fam = _type_family(cinfo.type)
            if unit in ("万元", "元", "%", "万人", "平方公里") and fam != "numeric":
                issues.append(SchemaIssue("WARN", tname, cname,
                                          "字段有单位「%s」但库里类型是 %s，可能是文本存储"
                                          % (unit, cinfo.type)))
    return issues


def has_blocking_issues(issues):
    return any(i.level == "ERROR" for i in issues)


def build_schema_block(tables, meta=None):
    """生成提示词里的"表结构"部分。只写 scope 内、字典里有含义的列。"""
    meta = config.load_schema_meta() if meta is None else meta
    dictionary = (meta or {}).get("tables") or {}
    lines = []
    by_name = dict((t.name.lower(), t) for t in tables)

    for tname, tspec in dictionary.items():
        tinfo = by_name.get(tname.lower())
        if tinfo is None:
            continue
        lines.append("表名：%s   —— %s" % (tname, tspec.get("comment", "")))
        if tspec.get("grain"):
            lines.append("数据粒度：%s" % tspec["grain"])
        if tinfo.row_count is not None:
            lines.append("行数：约 %d" % tinfo.row_count)
        lines.append("字段：")
        db_cols = dict((c.name.lower(), c) for c in tinfo.columns)
        for cname, cspec in (tspec.get("columns") or {}).items():
            real = db_cols.get(cname.lower())
            if real is None:
                continue
            unit = cspec.get("unit") or ""
            note = cspec.get("note") or ""
            line = "  %-18s %-10s %-8s" % (cname, real.type or "?", cspec.get("name", ""))
            if unit:
                line += " 单位:%s" % unit
            if note:
                line += " （%s）" % note
            lines.append(line)
        lines.append("")

    derived = (meta or {}).get("derived") or {}
    if derived:
        lines.append("可计算的派生指标（用 SQL 表达式算，不要新增字段）：")
        for key, spec in derived.items():
            lines.append("  %s = %s   单位:%s  %s"
                         % (spec.get("name", key), spec.get("formula", ""),
                            spec.get("unit", ""), spec.get("note", "")))
        lines.append("")

    lines.append("可用的分类维度：")
    if by_name.get("county_economy"):
        lines.append("  province（省份）、county_name（县名）、year（年份）")
    if by_name.get("county_info"):
        lines.append("  county_info.terrain（平原/丘陵/山地）、"
                     "county_info.is_poverty_county（1/0）、county_info.city（地级市）")
    return "\n".join(lines)


def build_prompt(tables, meta=None, extra_rules=""):
    """组装完整的 NL2SQL 系统提示词：结构 + 规则 + few-shot。"""
    meta = config.load_schema_meta() if meta is None else meta
    header = ("你是 SQL 生成助手。你只能基于下面的数据表结构生成 SQL。\n"
              "禁止创建、删除、修改任何表；禁止查询未列出的表或字段。\n")
    return header + "\n" + build_schema_block(tables, meta) + "\n" + (extra_rules or "")


def dump_prompt(text, path=None):
    """把实际用的提示词落盘，便于事后复盘"模型当时看到的是什么"。"""
    path = path or os.path.join(config.PROMPT_DIR, "schema_prompt.generated.txt")
    try:
        config.ensure_dirs()
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:                                     # noqa: BLE001
        pass
    return path


def snapshot(source, meta=None):
    """一次拿齐：结构、问题清单、提示词。供启动自检和 /api/schema 用。"""
    tables = source.introspect()
    meta = config.load_schema_meta() if meta is None else meta
    issues = validate(tables, meta)
    extra = load_rules()
    prompt = build_prompt(tables, meta, extra)
    return {
        "datasource": source.description,
        "tables": [t.to_dict() for t in tables],
        "issues": [i.to_dict() for i in issues],
        "blocking": has_blocking_issues(issues),
        "prompt_chars": len(prompt),
        "prompt": prompt,
    }


def load_rules():
    path = os.path.join(config.PROMPT_DIR, "sql_rules.txt")
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def check_startup(source=None):
    """启动自检：返回 (ok, snapshot)。ERROR 级问题直接判定不可用。"""
    from .datasource import default_source
    source = source or default_source()
    try:
        snap = snapshot(source)
    except Exception as e:                                # noqa: BLE001
        return False, {"error": str(e), "datasource": getattr(source, "description", "?")}
    return (not snap["blocking"]), snap


def table_fingerprint(source=None):
    """结构指纹：列名+类型拼成的短哈希。用来判断"库变了但服务没重启"。"""
    from .datasource import default_source
    import hashlib
    source = source or default_source()
    parts = []
    for t in source.introspect():
        parts.append(t.name + "(" + ",".join("%s:%s" % (c.name, c.type) for c in t.columns) + ")")
    raw = "|".join(sorted(parts))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16], json.dumps(
        {"tables": len(parts), "chars": len(raw)}, ensure_ascii=False)
