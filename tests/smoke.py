# -*- coding: utf-8 -*-
"""冒烟测试：数据源 / schema 校验 / SQL 四层防御。
跑法：cd stat_agent && python tests/smoke.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import schema as schema_mod            # noqa: E402
from core.datasource import create_source, DataSourceError   # noqa: E402
from core.nlsql import validate_and_execute, build_template_sql, NL2SQL  # noqa: E402
from core.llm_client import LLMClient             # noqa: E402
from core.logging_setup import new_trace_id      # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  ok   %s %s" % (name, detail))
    else:
        FAIL.append(name)
        print("  FAIL %s %s" % (name, detail))


def main():
    new_trace_id()
    print("== 1. 数据源与结构发现 ==")
    src = create_source()
    tables = src.introspect()
    check("introspect 返回 2 张表", len(tables) == 2,
          [t.name for t in tables])
    econ = [t for t in tables if t.name == "county_economy"][0]
    check("county_economy 有 11 列", len(econ.columns) == 11, len(econ.columns))
    check("行数 432", econ.row_count == 432, econ.row_count)
    check("gdp 类型为 REAL", any(c.name == "gdp" and "REAL" in c.type for c in econ.columns))

    print("== 2. 字典一致性校验 ==")
    snap = schema_mod.snapshot(src)
    errors = [i for i in snap["issues"] if i["level"] == "ERROR"]
    warns = [i for i in snap["issues"] if i["level"] == "WARN"]
    check("无 ERROR 级问题", len(errors) == 0, errors)
    print("       WARN %d 条：%s" % (len(warns), [w["column"] for w in warns]))
    check("提示词已生成", snap["prompt_chars"] > 1500, snap["prompt_chars"])

    print("== 3. 正常查询 ==")
    for q, intent in [("2023年各县农业总产值的平均值是多少", "query"),
                      ("2023年GDP最高的五个县", "query"),
                      ("各县居民收入和农业总产值是否显著相关", "corr"),
                      ("不同地形类型的县2023年居民平均收入", "group")]:
        sql = build_template_sql(q, intent)
        ok, cols, rows, chk, note = validate_and_execute(sql, src)
        check("模板可执行: %s" % q[:14], ok, "%s -> %d 行 %s" % (
            note, len(rows or []), cols))

    print("== 4. 安全防御 ==")
    bad = [
        ("DROP TABLE county_economy", "L1"),
        ("SELECT 1; DROP TABLE county_economy", "L1"),
        ("DELETE FROM county_economy WHERE 1=1", "L1"),
        ("SELECT * FROM sqlite_master", "L2"),
        ("SELECT county_id, name FROM county_economy", "col"),
        ("PRAGMA table_info(county_economy)", "L1"),
    ]
    for sql, tag in bad:
        ok, cols, rows, chk, note = validate_and_execute(sql, src)
        check("拒绝[%s]: %s" % (tag, sql[:34]), not ok, note[:70])

    print("== 5. 合法但需要留意 ==")
    ok, cols, rows, chk, note = validate_and_execute(
        "SELECT county_name, income FROM county_economy LIMIT 999999", src)
    check("超大 LIMIT 被压到 5000", ok, note)

    print("== 6. NL2SQL 离线模式 ==")
    # 显式指定 offline：本机可能配了 .env 里的 key，冒烟测试不该依赖"有没有配"。
    eng = NL2SQL(llm=LLMClient(provider="offline", quiet=True), schema_prompt="x")
    res = eng.generate("2023年各县农业总产值的平均值是多少", "query")
    check("离线仍能出 SQL", res.ok, res.source)
    check("降级标记为 llm:offline", res.degraded == "llm:offline", res.degraded)

    print("\n结果：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
