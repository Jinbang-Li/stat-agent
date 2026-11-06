# -*- coding: utf-8 -*-
"""
可观测性：结构化日志 + trace_id 贯穿。

为什么不用 print / 默认 logging：
- 一条日志必须能被机器读（jsonl），否则出问题时只能靠肉眼翻屏；
- 一个用户请求会横跨 意图→SQL→查询→统计→报告 五个阶段，
  没有 trace_id 根本对不上是哪个请求出的错；
- 商业系统里"这条日志属于哪个请求、花了多久、哪一步降级了"是排障的第一现场。
"""

import contextvars
import json
import logging
import os
import sys
import time
import uuid

from . import config

_trace_id = contextvars.ContextVar("stat_agent_trace_id", default="-")
_stage = contextvars.ContextVar("stat_agent_stage", default="-")


def new_trace_id():
    tid = time.strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:12]
    _trace_id.set(tid)
    return tid


def get_trace_id():
    return _trace_id.get()


def set_stage(stage):
    _stage.set(stage)


def get_stage():
    return _stage.get()


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "trace_id": getattr(record, "trace_id", _trace_id.get()),
            "stage": getattr(record, "stage", _stage.get()),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)[:2000]
        return json.dumps(payload, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    def format(self, record):
        tid = getattr(record, "trace_id", _trace_id.get())
        stage = getattr(record, "stage", _stage.get())
        base = "%s [%s][%s] %s" % (
            time.strftime("%H:%M:%S", time.localtime(record.created)),
            tid[-6:] if tid != "-" else "-", stage, record.getMessage())
        extra = getattr(record, "extra_fields", None)
        if extra:
            base += "  " + " ".join("%s=%s" % (k, v) for k, v in sorted(extra.items()))
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)[:2000]
        return base


_CONFIGURED = False


def setup_logging(level=None, quiet=False):
    """幂等初始化。日志同时写 output/logs/app.jsonl 和控制台。"""
    global _CONFIGURED
    if _CONFIGURED:
        return logging.getLogger("stat_agent")
    config.ensure_dirs()

    root = logging.getLogger("stat_agent")
    root.setLevel(getattr(logging, (level or os.environ.get("STAT_AGENT_LOG_LEVEL", "INFO")).upper(),
                        logging.INFO))
    root.propagate = False

    fh = logging.FileHandler(os.path.join(config.LOG_DIR, "app.jsonl"), encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    root.addHandler(fh)

    if not quiet:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(HumanFormatter())
        root.addHandler(ch)

    _CONFIGURED = True
    return root


def get_logger(name="stat_agent"):
    setup_logging()
    return logging.getLogger("stat_agent." + name)


_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
           "warn": logging.WARNING, "error": logging.ERROR, "critical": logging.CRITICAL}


def log_event(logger, msg, level=logging.INFO, **fields):
    """带结构化字段的日志。level 既接受 logging.INFO 这样的整数，也接受 "warning" 字符串。

    接受字符串是为了调用处可读：`log_event(log, "x", level="error")` 比
    `level=logging.ERROR` 少一次 import，也少一次写错的机会。
    """
    if isinstance(level, str):
        level = _LEVELS.get(level.lower(), logging.INFO)
    logger.log(level, msg, extra={"extra_fields": fields})


class Timer(object):
    """上下文管理器，记录阶段耗时。用法：
        with Timer() as t: ...
        t.ms
    """

    def __init__(self):
        self.ms = 0.0
        self._t0 = None

    def __enter__(self):
        self._t0 = time.time()
        return self

    def __exit__(self, *exc):
        self.ms = round((time.time() - self._t0) * 1000, 1)
        return False
