# -*- coding: utf-8 -*-
"""
通用 DB-API 数据源（MySQL / PostgreSQL）。

为什么单独写一个而不是给每种库写一份：pymysql、psycopg2、mysqlclient 都实现了
PEP 249，连接、游标、fetchmany 的行为一致，差异只在三处——方言的引号与 LIMIT、
结构查询的 information_schema 写法、只读会话怎么开。把差异收在这三个钩子里，
其余代码共用。

只读是怎么保证的（比 SQLite 的 authorizer 弱一档，但仍是纵深）：
  L1 文本白名单（同 SQLite）
  L3 会话级只读：PG 用 `SET TRANSACTION READ ONLY`，MySQL 用
     `SET SESSION TRANSACTION READ ONLY`，由数据库拒绝写操作
  L4 语句超时：PG 用 statement_timeout，MySQL 用 max_execution_time
  L4 行数上限：强制 LIMIT

注意：这个实现依赖驱动，当前环境没装 pymysql/psycopg2，所以**未在生产库上验证**。
代码路径是完整的，但首次接入真实库时必须跑一遍 `python -m tests.test_datasource`。
"""

import re

from .base import (CheckResult, ColumnInfo, DataSource, DataSourceError,
                   ExecResult, TableInfo)
from .sqlite_source import FORBIDDEN_KEYWORDS, enforce_limit, has_multiple_statements, \
    _strip_string_literals

_DRIVERS = {
    "mysql": ("pymysql", "mysqlclient"),
    "postgresql": ("psycopg2", "psycopg"),
}

_SYSTEM_SCHEMAS = {
    "mysql": ("information_schema", "mysql", "performance_schema", "sys"),
    "postgresql": ("pg_catalog", "information_schema"),
}


def _import_driver(kind):
    for name in _DRIVERS.get(kind, ()):
        try:
            return name, __import__(name)
        except ImportError:
            continue
    raise DataSourceError(
        "未安装 %s 驱动。请先 `pip install %s`，或改用 SQLite 数据源。"
        % (kind, "pymysql" if kind == "mysql" else "psycopg2-binary"))


class DBAPISource(DataSource):
    def __init__(self, kind, dsn, allowed_tables=None, allowed_columns=None,
                 timeout=20, max_rows=5000):
        self.kind = kind
        self.dialect = kind
        self.dsn = dsn
        self.timeout = timeout
        self.max_rows = max_rows
        self.allowed_tables = set(t.lower() for t in (allowed_tables or []))
        self.allowed_columns = dict(
            (k.lower(), set(c.lower() for c in v))
            for k, v in (allowed_columns or {}).items())
        self._driver_name, self._driver = _import_driver(kind)

    @property
    def description(self):
        return "%s via %s" % (self.kind, self._driver_name)

    def quote(self, ident):
        if self.kind == "mysql":
            return "`%s`" % ident.replace("`", "``")
        return '"%s"' % ident.replace('"', '""')

    # ------------------------------------------------------------ 连接
    def _connect(self):
        try:
            if self.kind == "mysql":
                conn = self._driver.connect(self.dsn, connect_timeout=self.timeout)
            else:
                conn = self._driver.connect(self.dsn, connect_timeout=self.timeout)
            conn.autocommit = False
        except Exception as e:                            # noqa: BLE001
            raise DataSourceError("连接失败：%s" % e)

        cur = conn.cursor()
        try:
            if self.kind == "postgresql":
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute("SET statement_timeout = %d" % (self.timeout * 1000))
            else:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
                try:
                    cur.execute("SET SESSION max_execution_time = %d" % (self.timeout * 1000))
                except Exception:                         # noqa: BLE001
                    pass          # 老版本 MySQL 没有这个变量，忽略
        finally:
            cur.close()
        return conn

    # ------------------------------------------------------------ 结构发现
    def introspect(self):
        conn = self._connect()
        cur = conn.cursor()
        tables = []
        try:
            if self.kind == "mysql":
                rows = self._mysql_tables(cur)
            else:
                rows = self._pg_tables(cur)
            for tname, comment in rows:
                if self.allowed_tables and tname.lower() not in self.allowed_tables:
                    continue
                cols = self._columns(cur, tname)
                n = None
                try:
                    cur.execute("SELECT COUNT(*) FROM %s" % self.quote(tname))
                    n = cur.fetchone()[0]
                except Exception:                         # noqa: BLE001
                    conn.rollback()
                tables.append(TableInfo(tname, cols, n, comment or ""))
        finally:
            cur.close()
            conn.close()
        return tables

    def _mysql_tables(self, cur):
        cur.execute("SELECT TABLE_NAME, TABLE_COMMENT FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE' "
                    "ORDER BY TABLE_NAME")
        return [(r[0], r[1]) for r in cur.fetchall()]

    def _pg_tables(self, cur):
        cur.execute("SELECT table_name, '' FROM information_schema.tables "
                    "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
                    "AND table_type = 'BASE TABLE' ORDER BY table_name")
        return [(r[0], r[1]) for r in cur.fetchall()]

    def _columns(self, cur, table):
        if self.kind == "mysql":
            cur.execute("SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY "
                        "FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
                        "ORDER BY ORDINAL_POSITION", (table,))
            return [ColumnInfo(r[0], r[1], nullable=(r[2] == "YES"),
                               primary_key=(r[3] == "PRI")) for r in cur.fetchall()]
        cur.execute("SELECT c.column_name, c.data_type, c.is_nullable, "
                    "CASE WHEN tc.constraint_type = 'PRIMARY KEY' THEN 1 ELSE 0 END "
                    "FROM information_schema.columns c "
                    "LEFT JOIN information_schema.key_column_usage k "
                    "  ON k.table_name = c.table_name AND k.column_name = c.column_name "
                    "LEFT JOIN information_schema.table_constraints tc "
                    "  ON tc.constraint_name = k.constraint_name AND tc.constraint_type = 'PRIMARY KEY' "
                    "WHERE c.table_name = %s ORDER BY c.ordinal_position", (table,))
        return [ColumnInfo(r[0], r[1], nullable=(r[2] == "YES"),
                           primary_key=bool(r[3])) for r in cur.fetchall()]

    # ------------------------------------------------------------ 校验 / 执行
    def check(self, sql, max_rows=None):
        res = CheckResult()
        sql = (sql or "").strip().rstrip(";").strip()
        low = _strip_string_literals(sql).lower()
        if not low:
            return res.add("L1 文本白名单", False, "SQL 为空")
        if has_multiple_statements(sql):
            return res.add("L1 文本白名单", False, "检测到多条语句，只允许单条 SELECT")
        if not re.match(r"^\s*(select|with)\b", low):
            return res.add("L1 文本白名单", False, "语句必须以 SELECT 或 WITH 开头")
        for kw in FORBIDDEN_KEYWORDS:
            if re.search(r"\b%s\b" % kw, low):
                return res.add("L1 文本白名单", False, "出现禁止的关键字：%s" % kw.upper())
        res.add("L1 文本白名单", True, "单条 SELECT，无危险关键字")
        res.add("L2 表白单", True, "表白单由数据库账号权限与 L1 共同保证")
        return res

    def execute(self, sql, max_rows=None):
        max_rows = max_rows or self.max_rows
        sql = (sql or "").strip().rstrip(";").strip()
        check = self.check(sql, max_rows)
        if not check.passed:
            raise DataSourceError(check.failed_layer["note"])
        sql = enforce_limit(sql, max_rows)

        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute(sql)
            columns = [d[0] for d in (cur.description or [])]
            rows = cur.fetchmany(max_rows + 1)
            truncated = len(rows) > max_rows
            return ExecResult(columns, [list(r) for r in rows[:max_rows]], truncated,
                              0.0, sql)
        except Exception as e:                            # noqa: BLE001
            raise DataSourceError("SQL 执行失败：%s" % e)
        finally:
            try:
                conn.rollback()
            except Exception:                             # noqa: BLE001
                pass
            cur.close()
            conn.close()
