# -*- coding: utf-8 -*-
"""
黄金用例回归。

为什么需要它：这个系统里"看起来对"和"真的对"差别很大——SQL 可能取错年份、
相关可能把面板当截面算、p 值可能因为重复计数被压小。这些错误不会抛异常，
只会安静地给出一个像模像样的错误结论。所以每条用例断言的是一条**行为契约**：
意图是什么、方法是不是按数据特征选的、该不该拒绝、关键统计量在不在容差内。

用法：
    python main.py eval                  # 跑全部
    python main.py eval --quiet          # 只输出汇总
    STAT_AGENT_PROVIDER=offline python main.py eval   # 固定在离线模式跑（推荐，可复现）
"""

import json
import os
import sys
import time

from core import config
from core.pipeline import answer

CASES_PATH = os.path.join(config.CASE_DIR, "cases.json")
REPORT_PATH = os.path.join(config.OUTPUT_DIR, "eval_report.md")


# ---------------------------------------------------------------- 取值
def _dig(obj, path):
    """按 'a.b.0.c' 取值，兼容 dict 与 list。"""
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _cmp(actual, op, expected, tol):
    if actual is None:
        return False, "取不到值"
    try:
        if op == "~":
            ok = abs(float(actual) - float(expected)) <= float(tol or 0.01)
            return ok, "实际 %s，期望 %s±%s" % (actual, expected, tol)
        if op == "==":
            return actual == expected, "实际 %s，期望 %s" % (actual, expected)
        if op == ">":
            return float(actual) > float(expected), "实际 %s，期望 >%s" % (actual, expected)
        if op == "<":
            return float(actual) < float(expected), "实际 %s，期望 <%s" % (actual, expected)
        if op == ">=":
            return float(actual) >= float(expected), "实际 %s，期望 >=%s" % (actual, expected)
        if op == "<=":
            return float(actual) <= float(expected), "实际 %s，期望 <=%s" % (actual, expected)
        if op == "in":
            return expected in actual, "实际 %r，期望包含 %r" % (actual, expected)
    except (TypeError, ValueError) as e:
        return False, "比较失败：%s" % e
    return False, "未知比较符 %s" % op


# ---------------------------------------------------------------- 断言
def check_case(case, result):
    """返回 (是否通过, [(断言, 是否通过, 说明)])"""
    exp = case.get("expect") or {}
    d = result.to_dict(include_rows=False)
    plan = d.get("plan") or {}
    intent = d.get("intent") or {}
    report = d.get("report") or {}
    md = report.get("markdown") or ""
    sql = (d.get("sql") or "").strip()
    checks = []

    def add(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    if "intent" in exp:
        add("意图=%s" % exp["intent"], intent.get("intent") == exp["intent"],
            "实际 %s" % intent.get("intent"))

    if "method" in exp:
        want = exp["method"]
        got = plan.get("method")
        if want is None:
            add("不执行统计方法", bool(plan.get("reject")),
                "实际 method=%s reject=%s" % (got, plan.get("reject_code")))
        else:
            add("方法=%s" % want, got == want, "实际 %s" % got)

    if "reject" in exp:
        want = exp["reject"]
        got = plan.get("reject_code")
        if want is None:
            add("不拒绝", not plan.get("reject"), "实际拒绝码 %s" % got)
        else:
            add("拒绝码=%s" % want, got == want, "实际 %s" % got)

    if "rows" in exp:
        lo, hi = exp["rows"]
        n = d.get("n_rows")
        add("行数在 [%s,%s]" % (lo, hi), n is not None and lo <= n <= hi, "实际 %s" % n)

    if exp.get("sql_empty"):
        add("未生成 SQL", not sql, "实际 SQL 长度 %d" % len(sql))

    for s in (exp.get("stats") or []):
        actual = _dig(d, s["path"])
        ok, detail = _cmp(actual, s.get("op", "~"), s.get("value"), s.get("tol"))
        add("%s %s %s" % (s["path"], s.get("op", "~"), s.get("value")), ok, detail)

    for kw in (exp.get("report") or []):
        add("报告含「%s」" % kw, kw in md, "未出现" if kw not in md else "")

    for kw in (exp.get("sql_contains") or []):
        add("SQL 含「%s」" % kw, kw in sql, "未出现" if kw not in sql else "")

    for code in (exp.get("degraded") or []):
        add("降级标记 %s" % code, code in (d.get("degraded") or []),
            "实际 %s" % (d.get("degraded") or []))

    for code in (exp.get("report_flags_absent") or []):
        add("未出现标记 %s" % code, code not in (report.get("flags") or []),
            "实际 %s" % (report.get("flags") or []))

    return all(c[1] for c in checks), checks


# ---------------------------------------------------------------- 执行
def run_all(verbose=True):
    if not os.path.isfile(CASES_PATH):
        print("找不到用例文件：%s" % CASES_PATH)
        return 2
    with open(CASES_PATH, "r", encoding="utf-8") as f:
        spec = json.load(f)
    cases = spec.get("cases") or []

    from core.llm_client import get_default_client
    mode = get_default_client().describe()
    if verbose:
        print("")
        print("  黄金用例回归：%d 条" % len(cases))
        print("  运行模式：%s" % mode)
        print("")

    started = time.time()
    rows = []
    n_pass = n_must_fail = 0
    for case in cases:
        t0 = time.time()
        try:
            r = answer(case["question"], session_id="eval", actor="eval")
            ok, checks = check_case(case, r)
            err = r.error
        except Exception as e:                              # noqa: BLE001
            ok, checks, err = False, [("执行异常", False, str(e))], str(e)
        ms = int((time.time() - t0) * 1000)
        must = case.get("must", True)
        if ok:
            n_pass += 1
        elif must:
            n_must_fail += 1
        rows.append({"case": case, "ok": ok, "checks": checks, "ms": ms, "err": err})

        if verbose:
            mark = "PASS" if ok else ("FAIL" if must else "warn")
            print("  [%s] %-4s %-6s %5dms  %s" % (mark, case["id"], case.get("group", ""),
                                                  ms, case["question"]))
            if not ok:
                for name, cok, detail in checks:
                    if not cok:
                        print("          · %s → %s" % (name, detail))

    total_ms = int((time.time() - started) * 1000)
    rate = (n_pass / float(len(cases)) * 100) if cases else 0.0
    if verbose:
        print("")
        print("  通过 %d / %d（%.1f%%），must 用例失败 %d 条，总耗时 %sms"
              % (n_pass, len(cases), rate, n_must_fail, total_ms))
        print("")

    _write_report(rows, rate, n_pass, len(cases), n_must_fail, total_ms, mode, verbose)
    return 0 if n_must_fail == 0 else 1


def _write_report(rows, rate, n_pass, n_total, n_must_fail, total_ms, mode, verbose):
    config.ensure_dirs()
    lines = ["# 黄金用例回归报告", "",
             "- 运行时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
             "- 运行模式：%s" % mode,
             "- 用例数：%d，通过：%d（%.1f%%），must 用例失败：%d" % (n_total, n_pass, rate, n_must_fail),
             "- 总耗时：%sms" % total_ms, "",
             "| 用例 | 分组 | 问题 | 结果 | 耗时 |", "| --- | --- | --- | --- | --- |"]
    for row in rows:
        c = row["case"]
        lines.append("| %s | %s | %s | %s | %sms |" % (
            c["id"], c.get("group", ""), c["question"],
            "通过" if row["ok"] else ("**失败**" if c.get("must", True) else "待改进"),
            row["ms"]))
    fails = [r for r in rows if not r["ok"]]
    if fails:
        lines += ["", "## 未通过明细", ""]
        for row in fails:
            lines.append("### %s %s" % (row["case"]["id"], row["case"]["question"]))
            for name, ok, detail in row["checks"]:
                if not ok:
                    lines.append("- %s → %s" % (name, detail))
            lines.append("")
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).strip() + "\n")
    if verbose:
        print("  报告已写入：%s" % REPORT_PATH)


if __name__ == "__main__":
    sys.exit(run_all())
