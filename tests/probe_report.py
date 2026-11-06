# -*- coding: utf-8 -*-
"""看模型写的报告为什么没通过数字白名单。

打印：模型原文、被判定为"无法溯源"的数字、最终用的报告来源。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import pipeline                       # noqa: E402
from core.llm_client import LLMClient           # noqa: E402
from core.report_gen import _allowed_numbers, verify_numbers  # noqa: E402


class Recorder(object):
    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def chat(self, system_prompt, user_prompt, **kw):
        text = self.inner.chat(system_prompt, user_prompt, **kw)
        self.calls.append((system_prompt, user_prompt, text))
        return text

    def try_chat(self, system_prompt, user_prompt, **kw):
        try:
            return self.chat(system_prompt, user_prompt, **kw)
        except Exception:                            # noqa: BLE001
            return None


def main():
    q = sys.argv[1] if len(sys.argv) > 1 else "预测未来三年监利市gdp"
    client = Recorder(LLMClient(quiet=True))
    r = pipeline.answer(q, llm=client, persist=False)
    print("问题：%s" % q)
    print("报告来源：%s   降级：%s" % (r.report.get("source"), r.degraded))
    print("错误：%s" % (r.report.get("error") or "-"))
    print("")
    if client.calls:
        system_prompt, user_prompt, text = client.calls[-1]
        print("=== 模型报告原文 ===")
        print(text)
        print("")
        import json
        try:
            payload = json.loads(user_prompt)
        except Exception:                            # noqa: BLE001
            payload = None
        if payload is not None:
            bad = verify_numbers(text, payload)
            print("无法溯源：%s" % (bad or "无"))
            allowed = sorted(_allowed_numbers(payload))
            print("白名单样本（前 40）：%s" % allowed[:40])


if __name__ == "__main__":
    main()
