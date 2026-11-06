# -*- coding: utf-8 -*-
"""
数据源抽象层。

目的：让"分析逻辑"和"数据库"彻底解耦。上层（路由、统计、报告）只认
`DataSource` 这一个接口，换 SQLite / MySQL / PostgreSQL 不需要改任何业务代码。

接口只有四个方法：
    introspect()  读表结构（schema 自动发现靠它，不靠人手写提示词）
    execute()     执行一条只读 SQL
    check()       不执行，只做 SQL 安全校验（供"先校验后展示"的场景用）
    dialect       方言名，用于提示词与 SQL 生成

安全责任放在数据源这一层，而不是调用方：谁掌握连接，谁负责只读。
"""

import time


class DataSourceError(Exception):
    """数据源层面的错误（连接失败、方言不支持、执行超时）。"""


class ColumnInfo(object):
    __slots__ = ("name", "type", "nullable", "primary_key")

    def __init__(self, name, type="", nullable=True, primary_key=False):
        self.name = name
        self.type = (type or "").upper()
        self.nullable = bool(nullable)
        self.primary_key = bool(primary_key)

    def to_dict(self):
        return {"name": self.name, "type": self.type,
                "nullable": self.nullable, "primary_key": self.primary_key}

    def __repr__(self):
        return "<Column %s:%s>" % (self.name, self.type)


class TableInfo(object):
    __slots__ = ("name", "columns", "row_count", "comment")

    def __init__(self, name, columns=None, row_count=None, comment=""):
        self.name = name
        self.columns = columns or []
        self.row_count = row_count
        self.comment = comment

    @property
    def column_names(self):
        return [c.name for c in self.columns]

    def to_dict(self):
        return {"name": self.name,
                "row_count": self.row_count,
                "comment": self.comment,
                "columns": [c.to_dict() for c in self.columns]}


class ExecResult(object):
    """执行结果。带耗时与截断标记，便于上层做"数据可能不全"的提示。"""

    def __init__(self, columns, rows, truncated=False, elapsed_ms=0.0, sql=""):
        self.columns = columns or []
        self.rows = rows or []
        self.truncated = bool(truncated)
        self.elapsed_ms = elapsed_ms
        self.sql = sql

    @property
    def row_count(self):
        return len(self.rows)

    def to_dict(self):
        return {"columns": self.columns, "row_count": self.row_count,
                "truncated": self.truncated, "elapsed_ms": self.elapsed_ms}


class CheckResult(object):
    """SQL 安全校验结果，逐层可解释。"""

    def __init__(self):
        self.layers = []          # [{layer, passed, note}]

    def add(self, layer, ok, note):
        self.layers.append({"layer": layer, "passed": bool(ok), "note": note})
        return self

    @property
    def passed(self):
        return all(l["passed"] for l in self.layers) and bool(self.layers)

    @property
    def failed_layer(self):
        for l in self.layers:
            if not l["passed"]:
                return l
        return None

    def to_list(self):
        return list(self.layers)

    def summary(self):
        return "%d/%d 项通过" % (sum(1 for l in self.layers if l["passed"]),
                                 len(self.layers))


class DataSource(object):
    dialect = "generic"

    def introspect(self):
        """返回 [TableInfo]。只读，不修改库。"""
        raise NotImplementedError

    def execute(self, sql, max_rows=5000):
        """执行只读 SQL，返回 ExecResult。不安全或失败时抛 DataSourceError。"""
        raise NotImplementedError

    def check(self, sql, max_rows=5000):
        """只校验不执行，返回 CheckResult。"""
        raise NotImplementedError

    def close(self):
        pass

    # ---- 方言相关的 SQL 拼装，供生成层使用
    def quote(self, ident):
        return '"%s"' % ident

    def limit_clause(self, n):
        return "LIMIT %d" % int(n)

    @property
    def description(self):
        return self.dialect


class Timer(object):
    def __init__(self):
        self.ms = 0.0
        self._t0 = None

    def __enter__(self):
        self._t0 = time.time()
        return self

    def __exit__(self, *exc):
        self.ms = round((time.time() - self._t0) * 1000, 1)
        return False
