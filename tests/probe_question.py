# -*- coding: utf-8 -*-
"""按一个问题跑完整链路，并把"模型到底回了什么"全部打出来。

用法：
    python tests/probe_question.py "预测未来三年监利市gdp"

这不是单元测试，是给人看的排查工具：模型给的原始文本、清洗后的 SQL、
四层校验逐层结果、最终结果行数，一次全摊开。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config                      # noqa: E402
from core.llm_client import LLMClient        # noqa: E402
from core.nlsql import NL2SQL, clean_sql_text  # noqa: E402
from core.intent import classify_by_rules    # noqa: E402
from core.pipeline import schema_prompt      # noqa: E402
from core.datasource import default_source   # noqa: E402


class Recorder(object):
    """包一层，把每次 chat 的入参和模型原文都记下来。"""

    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def chat(self, system_prompt, user_prompt, **kw):
        text = self.inner.chat(system_prompt, user_prompt, **kw)
        self.calls.append({"system": system_prompt, "user": user_prompt, "text": text})
        return text

    def try_chat(self, system_prompt, user_prompt, **kw):
        try:
            return self.chat(system_prompt, user_prompt, **kw)
        except Exception as exc:                    # noqa: BLE001
            self.calls.append({"system": system_prompt, "user": user_prompt,
                               "text": "<ERROR %s: %s>" % (type(exc).__name__, exc)})
            return None


def show(exc_text, limit=1600):
    t = exc_text or ""
    print(t[:limit] + ("\n...（截断，共 %d 字符）" % len(t) if len(t) > limit else ""))


def main():
    question = sys.argv[1] if len(sys.argv) > 1 else "预测未来三年监利市gdp"
    print("问题：%s" % question)
    print("模型：%s" % LLMClient().describe())
    print("")

    intent = classify_by_rules(question)
    print("[1] 规则意图：%s（causal=%s）" % (intent.name, intent.causal_request))

    client = Recorder(LLMClient())
    intent2 = __import__("core.intent", fromlist=["classify"]).classify(question, llm=client)
    print("[2] 最终意图：%s  来源=%s" % (intent2.name, intent2.method))
    print("")

    src = default_source()
    eng = NL2SQL(llm=client, schema_prompt=schema_prompt(src), source=src)
    res = eng.generate(question, intent2)

    print("[3] 模型原始回复（%d 次调用）：" % len(client.calls))
    for i, c in enumerate(client.calls):
        print("---- 调用 %d ----" % (i + 1))
        print("system 片段：%s" % (c["system"][:100].replace("\n", " ")))
        show(c["text"])
        print("")

    print("[4] 清洗后的 SQL（sql_source=%s，degraded=%s）" % (res.source, res.degraded))
    print(res.sql or "（空）")
    print("")

    print("[5] 逐层校验：")
    if res.guard is not None:
        for item in res.guard.to_list():
            print("    %-6s %s  %s" % (item.get("layer"), "PASS" if item.get("passed")
                                       else "FAIL", item.get("note") or ""))
    print("    备注：%s" % (res.note or "-"))
    print("")

    print("[6] 结果：%d 行" % len(res.rows or []))
    print("    列：%s" % (res.columns or []))
    for row in (res.rows or [])[:5]:
        print("    %s" % (row,))

    if len(client.calls) and res.sql:
        cleaned = clean_sql_text(client.calls[0]["text"] or "")
        print("")
        print("[7] 单独重放第一条回复的清洗结果：")
        print(cleaned)


if __name__ == "__main__":
    main()
