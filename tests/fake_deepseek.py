# -*- coding: utf-8 -*-
"""
一个假的 DeepSeek 服务，用来在没有真 key 的环境里验证"在线模式"的完整代码路径。

它只实现 POST /chat/completions，按 system prompt 里的特征词决定回什么：
    意图分类器  → 一段 JSON
    生成 SQL    → 一条 SELECT
    分析师/报告 → 一段 markdown

这样能验证的事情很具体：意图确实由模型给出（intent.method == "llm"）、
SQL 确实由模型给出（sql_source == "llm"）、报告确实由模型撰写、
以及报告里的数字对不上时会被 verify_numbers 拦下来。

    python tests/fake_deepseek.py 8899     # 单独起服务，用 curl 调
"""

import json
import re
import sys
import threading

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:                                   # pragma: no cover
    from BaseHTTPServer import BaseHTTPRequestHandler
    from SocketServer import ThreadingHTTPServer

# 报告里的数字是否"正确"，由这个开关控制——用来演示数字白名单校验
BAD_NUMBER = {"on": False}

SQL_OK = ("SELECT county_name, income, agri_output FROM county_economy "
          "WHERE year = 2023 AND income IS NOT NULL AND agri_output IS NOT NULL LIMIT 5000")


def _reply_for(system_prompt, user_prompt):
    sp = system_prompt or ""
    if "意图分类器" in sp:
        return json.dumps({"intent": "corr", "reason": "问两个变量的相关性与显著性"},
                          ensure_ascii=False)
    # 顺序很重要：报告提示词里也会出现 "SQL" 字样（它要讲 sql 字段），
    # 所以必须先判"报告"，再判"生成 SQL"，否则报告请求会拿到一条 SELECT。
    if "分析师" in sp or "报告" in sp:
        if BAD_NUMBER["on"]:
            body = ("## 一、数据概览\n\n共 48 个观测。\n\n"
                    "## 二、分析过程\n\n采用 Spearman 相关分析。\n\n"
                    "## 三、结果与解读\n\n相关系数为 0.987，说明两者高度正相关。\n\n"
                    "## 四、结论与局限\n\n综合判断：相关关系很强。\n")
        else:
            body = ("## 一、数据概览\n\n共 48 个观测，为 2023 年截面数据。\n\n"
                    "## 二、分析过程\n\n先做正态性检验，再按数据特征选择 Spearman 相关。\n\n"
                    "## 三、结果与解读\n\n在 5% 显著性水平下，两者未呈现显著相关。"
                    "需要注意，相关不等于因果。\n\n"
                    "## 四、结论与局限\n\n综合判断：就现有样本而言，没有足够证据支持两者存在线性关联。\n")
        return body
    if "SQL" in sp or "sql" in sp:
        return SQL_OK
    return "OK"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):        # 静音，别把测试输出刷满
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {}
        msgs = payload.get("messages") or []
        system = next((m.get("content", "") for m in msgs if m.get("role") == "system"), "")
        user = next((m.get("content", "") for m in msgs if m.get("role") == "user"), "")
        content = _reply_for(system, user)

        body = json.dumps({
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "model": payload.get("model", "deepseek-chat"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
        }, ensure_ascii=False).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeDeepSeek(object):
    def __init__(self, port=8899):
        self.port = port
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever)
        self.thread.daemon = True

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        return False


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    print("fake deepseek listening on http://127.0.0.1:%d" % port)
    FakeDeepSeek(port).httpd.serve_forever()
