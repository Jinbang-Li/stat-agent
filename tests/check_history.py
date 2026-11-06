# -*- coding: utf-8 -*-
"""历史查询记录：写入、列表、回看、清理。

重点验三件事：
  1. 列表页要的字段（结论摘要、方法、耗时、是否降级）都在；
  2. 回看时能拿到**当时那份**报告与图表，而不是重新跑一遍；
  3. 历史写失败不能影响主流程（历史是旁路，不是链路的一部分）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import history, pipeline          # noqa: E402
from core.llm_client import LLMClient       # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s %s" % ("ok  " if ok else "FAIL", name, detail))


def main():
    llm = LLMClient(provider="offline", quiet=True)

    print("1) 一次问答后自动落一条历史")
    history.clear()
    before = history.count()
    # 用描述统计而不是"平均值"——单行聚合结果没有图表，
    # 这里要验的是"图表也随记录存下来"，得挑一个会出图的问题。
    r = pipeline.answer("看看各县居民收入的分布和极值", llm=llm, persist=True)
    after = history.count()
    check("历史条数 +1", after == before + 1, "%s → %s" % (before, after))

    print("2) 列表字段齐全")
    rows = history.recent(limit=5)
    check("有记录", len(rows) >= 1, len(rows))
    it = rows[0]
    check("问题已保存", it["question"].startswith("看看各县居民收入"), it["question"])
    check("意图已保存", bool(it["intent"]), it["intent"])
    check("方法已保存", bool(it["method"]), it["method"])
    check("结论摘要非空", bool((it["summary"] or "").strip()), it["summary"])
    check("耗时已记录", int(it["latency_ms"] or 0) >= 0, it["latency_ms"])
    check("降级字段存在", "degraded" in it, it["degraded"])

    print("3) 回看拿到完整报告与图表")
    full = history.get(it["id"])
    md = (full.get("report") or {}).get("markdown") or ""
    check("记录存在", full is not None, it["id"])
    check("报告正文已保存", "数据概览" in md, len(md))
    check("报告来源已保存", bool((full.get("report") or {}).get("source")),
          (full.get("report") or {}).get("source"))
    check("SQL 已保存", bool(full.get("sql")), (full.get("sql") or "")[:40])
    check("图表随记录保存", len(full.get("charts") or []) >= 1,
          len(full.get("charts") or []))
    svg = (full.get("charts") or [{}])[0].get("svg") or ""
    check("图表是 SVG", "<svg" in svg, svg[:20])

    print("4) 拒绝建模的记录也要能回看")
    r2 = pipeline.answer("把2023年GDP最高的三个县做相关分析", llm=llm, persist=True)
    rows2 = history.recent(limit=1)
    check("拒绝也写历史", rows2[0]["question"].startswith("把2023年GDP最高"), rows2[0]["question"])
    check("摘要说明了原因", "未建模" in (rows2[0]["summary"] or ""), rows2[0]["summary"])

    print("5) 历史写失败不能拖垮主流程")
    ok = True
    try:
        out = history.record(object())     # 缺字段的对象
    except Exception as exc:               # noqa: BLE001
        ok = False
        out = exc
    check("record() 不抛异常", ok, out)

    print("6) 清理")
    left = history.clear()
    check("清空后为 0", left == 0, left)
    check("get 不存在的 id 返回 None", history.get(999999) is None)

    print("")
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：%s" % "、".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
