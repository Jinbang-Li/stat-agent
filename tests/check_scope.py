# -*- coding: utf-8 -*-
"""两条"降级但不能给错答案"的路径。

1) 模型说 OUT_OF_SCOPE 时：不能回退到模板拼一条回答了别的问题的 SQL，
   而要明确说"答不了"；
2) 模型不可用时：模板必须把"监利市"限定到该县，不能拿全县均值顶上。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import pipeline          # noqa: E402
from core.llm_client import LLMClient  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s %s" % ("ok  " if ok else "FAIL", name, detail))


class ScopeLLM(object):
    """永远回答 OUT_OF_SCOPE 的假模型。"""

    online = True
    provider = "stub"

    def chat(self, system_prompt, user_prompt, **kw):
        if "意图分类器" in system_prompt:
            return '{"intent": "query", "reason": "取数"}'
        return "OUT_OF_SCOPE"

    def try_chat(self, system_prompt, user_prompt, **kw):
        return self.chat(system_prompt, user_prompt, **kw)

    def describe(self):
        return "stub / 永远 OUT_OF_SCOPE"


def main():
    print("1) 模型拒绝回答 → 明确说答不了，不回退模板")
    r = pipeline.answer("这个县的营商环境排名第几", llm=ScopeLLM(), persist=False)
    md = r.report.get("markdown") or ""
    check("报告来源是 scope", r.report.get("source") == "scope", r.report.get("source"))
    check("降级标记含 sql:out_of_scope", "sql:out_of_scope" in r.degraded, r.degraded)
    check("没有执行 SQL", not r.sql, repr(r.sql))
    check("报告说明了原因", "无法用现有数据回答" in md, md.splitlines()[0] if md else "")
    check("报告没有编造结果", "平均" not in md or "可以这样问" in md)

    print("2) 离线模板：点名某个县就必须只取该县")
    off = LLMClient(provider="offline", quiet=True)
    r2 = pipeline.answer("预测未来三年监利市gdp", llm=off, persist=False)
    sql = (r2.sql or "").replace("\n", " ")
    check("SQL 带县名条件", "county_name = '监利市'" in sql, sql[:120])
    check("不是全县平均口径", "GROUP BY year" in sql and "AVG" in sql, sql[:120])
    check("拿到的是该县自己的序列", r2.n_rows == 9, r2.n_rows)
    check("方法走时序", (r2.plan or {}).get("method") == "timeseries",
          (r2.plan or {}).get("method"))

    print("\n结果：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
