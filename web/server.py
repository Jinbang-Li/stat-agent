# -*- coding: utf-8 -*-
"""
内网 Web 服务（标准库 http.server，不依赖 FastAPI/Flask）。

设计取舍：交付环境是内网单机、并发个位数，装包的代价（以及 Python 3.7 下
FastAPI 依赖链的兼容问题）远大于收益。标准库 ThreadingHTTPServer 足够，
且整个服务零外部依赖——拷贝目录即可运行。

接口：
    GET  /                 单页前端
    GET  /api/health       启动自检（数据源、Schema、模型模式）
    GET  /api/schema       当前表结构提示词
    GET  /api/traces       最近审计记录
    GET  /api/metrics      请求量、失败率、平均耗时、token 用量
    POST /api/ask          {"question": "...", "session_id": "..."}
"""

import json
import os
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from core import config, audit
from core.logging_setup import get_logger, log_event, new_trace_id

log = get_logger("web")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

MAX_QUESTION = 500
_rate = {}
_rate_lock = threading.Lock()


def _rate_ok(session_id):
    """简单的按会话滑动窗口限流，防止误触脚本把 API 打满。"""
    now = time.time()
    with _rate_lock:
        q = _rate.setdefault(session_id or "-", deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= config.RATE_LIMIT_PER_MIN:
            return False
        q.append(now)
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = "StatAgent/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 基础工具 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str))

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 200000:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        # 默认实现会往 stderr 打日志，这里改走结构化日志
        log_event(log, "http", method=self.command, path=self.path,
                  status=getattr(self, "_status", None))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html", "text/html; charset=utf-8")
            if path == "/static/app.css":
                return self._static("app.css", "text/css; charset=utf-8")
            if path == "/static/app.js":
                return self._static("app.js", "application/javascript; charset=utf-8")
            if path == "/api/health":
                return self._health()
            if path == "/api/schema":
                from core.datasource import default_source
                from core.schema import snapshot
                snap = snapshot(default_source())
                return self._json({"prompt": snap["prompt"],
                                   "prompt_chars": snap["prompt_chars"],
                                   "issues": snap["issues"],
                                   "blocking": snap["blocking"]})
            if path == "/api/traces":
                qs = parse_qs(parsed.query)
                limit = min(int((qs.get("limit") or ["20"])[0]), 200)
                audit.init()
                rows = audit.recent(limit=limit)
                ok, bad = audit.verify_chain(limit=limit)
                return self._json({"items": rows, "chain_ok": ok, "chain_bad": bad})
            if path == "/api/metrics":
                return self._metrics()
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            return self._json({"error": "not found"}, 404)
        except Exception as e:  # 任何未捕获异常都不能让服务 500 掉线
            log.exception("GET %s 失败: %s", path, e)
            return self._json({"error": "内部错误：%s" % e}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/ask":
            return self._json({"error": "not found"}, 404)
        body = self._read_json()
        question = (body.get("question") or "").strip()
        session = (body.get("session_id") or "web").strip()[:64]
        if not question:
            return self._json({"error": "问题不能为空"}, 400)
        if len(question) > MAX_QUESTION:
            return self._json(
                {"error": "问题过长（%d 字），请控制在 %d 字以内" % (len(question), MAX_QUESTION)},
                400)
        if not _rate_ok(session):
            return self._json(
                {"error": "请求过于频繁，请稍后再试（每分钟最多 %d 次）"
                          % config.RATE_LIMIT_PER_MIN}, 429)
        try:
            from core.pipeline import answer
            r = answer(question, session_id=session, actor=body.get("actor") or "web")
            return self._json(r.to_dict())
        except Exception as e:
            # 兜底：即使用户看到报错，也必须带上 trace_id 供排查
            log.exception("问答失败: %s", e)
            return self._json({"error": "服务内部错误：%s" % e,
                               "trace_id": new_trace_id()}, 500)

    # ---------- 具体实现 ----------
    def _static(self, name, ctype):
        path = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(path):
            return self._json({"error": "静态文件缺失：%s" % name}, 404)
        with open(path, "rb") as f:
            data = f.read()
        self._send(200, data, ctype)

    def _health(self):
        from core.pipeline import warmup
        info = warmup()
        return self._json({
            "ok": not info.get("schema_blocking"),
            "datasource": info.get("datasource"),
            "tables": info.get("tables"),
            "llm": info.get("llm"),
            "llm_online": info.get("llm_online"),
            "model": info.get("model"),
            "temperature": info.get("temperature"),
            "prompt_chars": info.get("prompt_chars"),
            "schema_issues": info.get("schema_issues"),
            "schema_blocking": info.get("schema_blocking"),
            "audit_db": info.get("audit_db"),
            "limits": {
                "max_question": MAX_QUESTION,
                "rate_limit_per_min": config.RATE_LIMIT_PER_MIN,
                "sql_max_rows": config.SQL_MAX_ROWS,
            },
        })

    def _metrics(self):
        audit.init()
        s = audit.summary(days=7)
        from core.llm_client import get_default_client
        u = get_default_client().total_usage
        return self._json({"requests": s, "tokens": u,
                           "monthly_budget": config.MONTHLY_TOKEN_BUDGET})


def serve(host="127.0.0.1", port=8000):
    from core.pipeline import warmup
    import logging
    logging.getLogger("core.pipeline").setLevel(logging.WARNING)

    warmup()  # 预热：Schema 校验、数据源连通性，启动即暴露问题
    audit.init()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    log_event(log, "server_start", host=host, port=port)
    print("")
    print("  统计 Agent 已启动：http://%s:%d" % (host, port))
    print("  按 Ctrl+C 停止")
    print("")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。")
    finally:
        httpd.server_close()
