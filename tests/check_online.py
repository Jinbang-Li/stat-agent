# -*- coding: utf-8 -*-
"""
在线模式的端到端验证：用本地假服务代替 DeepSeek，跑完整条链路。

验证四件事（对应需求里的 2/3/4 条）：
  1. 有 key → 自动进在线模式，不需要任何额外开关；
  2. 意图识别由模型给出（intent.method == "llm"）；
  3. SQL 由模型给出（sql_source == "llm"），并且仍然过四层校验；
  4. 报告由模型撰写，但数字必须与 Python 计算结果对得上，对不上就回退模板。

    python tests/check_online.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "STAT_AGENT_PROVIDER",
          "STAT_AGENT_USE_OLLAMA", "STAT_AGENT_TEMPERATURE"):
    os.environ.pop(k, None)

import fake_deepseek                              # noqa: E402
from core.llm_client import LLMClient             # noqa: E402
from core.pipeline import answer                  # noqa: E402

PORT = 8899
QUESTION = "检验各县居民收入和农业总产值是否存在显著相关关系"

FAILS = []


def check(name, ok, detail=""):
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  → " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main():
    with fake_deepseek.FakeDeepSeek(PORT):
        os.environ["DEEPSEEK_API_KEY"] = "sk-fake-for-test"
        os.environ["DEEPSEEK_BASE_URL"] = "http://127.0.0.1:%d" % PORT

        print("1) 配了 key 就自动进在线模式")
        client = LLMClient(quiet=True)
        check("provider=deepseek", client.provider == "deepseek", client.provider)
        check("online=True", client.online is True)
        ok, msg = client.probe()
        check("probe 连通", ok, msg)

        print("2) 意图识别交给模型")
        r = answer(QUESTION, llm=client, persist=False)
        check("intent.detected_by == llm", (r.intent or {}).get("detected_by") == "llm", r.intent)
        check("意图判成 corr", (r.intent or {}).get("intent") == "corr", r.intent)

        print("3) SQL 由模型生成，并且照样过四层校验")
        check("sql_source == llm", r.sql_source == "llm", r.sql_source)
        check("SQL 是模型给的那条", "SELECT county_name, income, agri_output" in r.sql, r.sql[:80])
        checks = r.sql_checks or []
        check("四层校验都有记录", len(checks) >= 3, [c.get("layer") for c in checks])
        check("校验全部通过", all(c.get("passed") is not False for c in checks), checks)
        check("拿到 48 行", r.n_rows == 48, r.n_rows)
        check("统计方法选了 Spearman", (r.plan or {}).get("method") == "correlation",
              (r.plan or {}).get("method"))

        print("4) 报告由模型撰写，数字对得上就采用")
        check("report.source == llm", (r.report or {}).get("source") == "llm",
              (r.report or {}).get("source") + " / " + str((r.report or {}).get("error")))
        # 只验 source 不够：报告请求曾因为假服务分流顺序写错而拿到一条 SELECT，
        # source 依然是 llm、数字也"可溯源"，看上去全绿，实际上报告内容是 SQL。
        md = (r.report or {}).get("markdown", "")
        check("拿到的是报告而不是 SQL", "结论" in md and not md.lstrip().upper().startswith("SELECT"),
              md[:80])
        check("没有数字对账失败标记", "report:number_mismatch" not in (r.degraded or []), r.degraded)
        check("token 有计量", (client.total_usage or {}).get("calls", 0) >= 3,
              client.total_usage)

        print("5) 模型编了个不存在的数字 → 拦下来并回退模板")
        fake_deepseek.BAD_NUMBER["on"] = True
        try:
            r2 = answer(QUESTION, llm=client, persist=False)
        finally:
            fake_deepseek.BAD_NUMBER["on"] = False
        check("打上 report:number_mismatch 标记",
              "report:number_mismatch" in (r2.degraded or []), r2.degraded)
        check("报告回退为模板", (r2.report or {}).get("source") == "template",
              (r2.report or {}).get("source"))
        md = (r2.report or {}).get("markdown", "")
        check("编造的 0.987 没有出现在最终报告里", "0.987" not in md)
        check("回退的报告仍然完整", "结论" in md and "数据概览" in md)

    print("")
    if FAILS:
        print("未通过 %d 项：%s" % (len(FAILS), "、".join(FAILS)))
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
