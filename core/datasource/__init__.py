# -*- coding: utf-8 -*-
"""
数据源工厂。

调用方只说"我要一个数据源"，不关心背后是 SQLite 还是 MySQL。
白名单（哪些表、哪些列对外可见）来自 data/schema_meta.json 的 scope 段，
**不是硬编码在代码里**——换一套库只要换一份字典文件。
"""

from .. import config
from .base import (CheckResult, ColumnInfo, DataSource, DataSourceError,
                   ExecResult, TableInfo)
from .dbapi_source import DBAPISource
from .sqlite_source import SQLiteSource

__all__ = ["DataSource", "DataSourceError", "ExecResult", "TableInfo",
           "ColumnInfo", "CheckResult", "SQLiteSource", "DBAPISource",
           "create_source", "scope_from_meta"]


def scope_from_meta(meta=None):
    """从业务字典里取 scope，返回 (tables:set, columns:dict)。"""
    meta = config.load_schema_meta() if meta is None else meta
    scope = (meta or {}).get("scope") or {}
    tables = set(scope.keys())
    columns = dict((t, set(cols)) for t, cols in scope.items())
    return tables, columns


def create_source(cfg=None, meta=None):
    cfg = cfg or config.DataSourceConfig()
    tables, columns = scope_from_meta(meta)
    if cfg.kind == "sqlite":
        return SQLiteSource(cfg.dsn, tables, columns,
                            timeout=config.SQL_TIMEOUT, max_rows=config.SQL_MAX_ROWS)
    if cfg.kind in ("mysql", "postgresql", "postgres", "pg"):
        kind = "postgresql" if cfg.kind in ("postgres", "pg") else cfg.kind
        return DBAPISource(kind, cfg.dsn, tables, columns,
                           timeout=config.SQL_TIMEOUT, max_rows=config.SQL_MAX_ROWS)
    raise DataSourceError("不支持的数据源类型：%s（可选 sqlite / mysql / postgresql）"
                          % cfg.kind)


_DEFAULT = None


def default_source():
    """进程内单例。测试里可以 create_source() 另建一个。"""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = create_source()
    return _DEFAULT


def reset_default():
    global _DEFAULT
    _DEFAULT = None
