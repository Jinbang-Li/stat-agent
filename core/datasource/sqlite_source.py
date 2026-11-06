# -*- coding: utf-8 -*-
"""
SQLite 数据源。

四层防御在这里落地（不是散落在业务代码里）：

  L1 文本白名单   必须单条 SELECT/WITH；危险关键字直接拒
  L2 引擎 authorizer  交给 SQLite 解析，只放行 SELECT/READ/FUNCTION/RECURSIVE，
                  且 READ 的表、列必须在白名单内
  L3 只读连接     file:...?mode=ro，物理不可写
  L4 行数与超时   强制 LIMIT；用 progress_handler 实现语句级超时

为什么 L2 不能省：L1 是字符串匹配，`DELETE` 可以写成 `DeLeTe`、可以藏在
`/* */` 里、可以出现在子查询里。只有让引擎在语法树上判断才拦得住。
为什么 L4 的超时要单独做：一条 SELECT 只要不带 LIMIT 就能把内存或 IO 打满，
`LIMIT` 管不住 `ORDER BY`/`GROUP BY` 的中间结果规模，必须靠超时兜底。
"""

import re
import sqlite3
import time

from .base import (CheckResult, ColumnInfo, DataSource, DataSourceError,
                   ExecResult, TableInfo)

FORBIDDEN_KEYWORDS = [
    "insert", "update", "delete", "drop", "alter", "create", "replace",
    "attach", "detach", "pragma", "vacuum", "reindex", "trigger", "view",
    "load_extension", "writefile", "readfile",
]

_ALLOWED_ACTIONS = set([
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", -1),
])


def _strip_string_literals(sql):
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def has_multiple_statements(sql):
    stripped = _strip_string_literals(sql)
    parts = [p for p in stripped.split(";") if p.strip()]
    return len(parts) > 1


def enforce_limit(sql, max_rows):
    if re.search(r"\blimit\s+\d+", sql, re.I):
        return re.sub(r"(\blimit\s+)(\d+)",
                      lambda m: m.group(1) + str(min(int(m.group(2)), max_rows)),
                      sql, flags=re.I)
    return sql + " LIMIT %d" % max_rows


class SQLiteSource(DataSource):
    dialect = "sqlite"

    def __init__(self, path, allowed_tables=None, allowed_columns=None,
                 timeout=20, max_rows=5000):
        self.path = path
        self.timeout = timeout
        self.max_rows = max_rows
        self.allowed_tables = set(t.lower() for t in (allowed_tables or []))
        self.allowed_columns = dict(
            (k.lower(), set(c.lower() for c in v))
            for k, v in (allowed_columns or {}).items())

    @property
    def description(self):
        return "sqlite:%s（%d 张表白名单）" % (self.path, len(self.allowed_tables))

    # ------------------------------------------------------------ 结构发现
    def _ro_conn(self, allow_authorizer=False):
        uri = "file:%s?mode=ro" % self.path.replace("\\", "/")
        conn = sqlite3.connect(uri, uri=True, timeout=self.timeout)
        conn.text_factory = str
        conn.row_factory = None
        return conn

    def introspect(self):
        """读表结构。注意：这条路径**不挂 authorizer**，因为它要读 sqlite_master。
        但连接仍是 mode=ro，所以改不了库。"""
        if not self.path or not self._file_exists():
            raise DataSourceError("数据库文件不存在：%s" % self.path)
        conn = self._ro_conn()
        tables = []
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")
            names = [r[0] for r in cur.fetchall()]
            for name in names:
                if self.allowed_tables and name.lower() not in self.allowed_tables:
                    continue
                cols = []
                info = conn.execute("PRAGMA table_info(%s)" % _quote(name))
                for cid, cname, ctype, notnull, dflt, pk in info.fetchall():
                    cols.append(ColumnInfo(cname, ctype,
                                           nullable=not bool(notnull),
                                           primary_key=bool(pk)))
                try:
                    n = conn.execute("SELECT COUNT(*) FROM %s" % _quote(name)).fetchone()[0]
                except sqlite3.Error:
                    n = None
                tables.append(TableInfo(name, cols, n))
        finally:
            conn.close()
        return tables

    def _file_exists(self):
        import os
        return os.path.isfile(self.path)

    # ------------------------------------------------------------ 校验
    def check(self, sql, max_rows=None):
        """只做校验，不执行。L2 只能"预演"——真正的引擎级拒绝发生在 execute。"""
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

        tables = self._referenced_tables(sql)
        bad = [t for t in tables if self.allowed_tables and t.lower() not in self.allowed_tables]
        if bad:
            res.add("L2 表白名单", False, "引用了非白名单表：%s" % ", ".join(bad))
        else:
            res.add("L2 表白名单", True, "涉及表：%s" % (", ".join(tables) or "（无法静态解析）"))
        return res

    def _referenced_tables(self, sql):
        """粗解析 FROM/JOIN 后的表名，仅用于给人看的提示。
        真正的拦截不依赖它，而依赖 authorizer。"""
        found = []
        for m in re.finditer(r"\b(?:from|join)\s+([`\"\[]?)([A-Za-z_][\w]*)",
                             sql, re.I):
            t = m.group(2)
            if t.lower() not in ("select",):
                if t not in found:
                    found.append(t)
        return found

    # ------------------------------------------------------------ 执行
    def execute(self, sql, max_rows=None):
        max_rows = max_rows or self.max_rows
        sql = (sql or "").strip().rstrip(";").strip()
        check = self.check(sql, max_rows)
        if not check.passed:
            raise DataSourceError(check.failed_layer["note"])

        sql = enforce_limit(sql, max_rows)
        denials = []

        def authorizer(action, arg1, arg2, dbname, trigger):
            if action not in _ALLOWED_ACTIONS:
                denials.append("动作被拒 code=%s(%s/%s)" % (action, arg1, arg2))
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ:
                table = (arg1 or "").lower()
                col = (arg2 or "").lower()
                if self.allowed_tables and table not in self.allowed_tables:
                    denials.append("读取了非白名单表：%s" % arg1)
                    return sqlite3.SQLITE_DENY
                allowed_cols = self.allowed_columns.get(table)
                if allowed_cols is not None and col and col not in allowed_cols:
                    denials.append("读取了非白名单列：%s.%s" % (table, col))
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        started = time.time()
        conn = self._ro_conn()
        conn.set_authorizer(authorizer)

        deadline = started + self.timeout

        def progress():
            if time.time() > deadline:
                return 1          # 非 0 -> SQLite 中止执行
            return 0

        conn.set_progress_handler(progress, 20000)
        try:
            cur = conn.execute(sql)
            columns = [d[0] for d in (cur.description or [])]
            rows = cur.fetchmany(max_rows + 1)
            truncated = len(rows) > max_rows
            rows = rows[:max_rows]
            if denials:
                raise DataSourceError("引擎拦截：" + "；".join(denials))
            return ExecResult(columns, [list(r) for r in rows], truncated,
                              round((time.time() - started) * 1000, 1), sql)
        except sqlite3.OperationalError as e:
            if denials:
                raise DataSourceError("引擎拦截：" + "；".join(denials))
            msg = str(e)
            if "interrupted" in msg.lower():
                raise DataSourceError("查询超时（>%d 秒），已中止。请缩小时间范围或加聚合条件。"
                                      % self.timeout)
            raise DataSourceError("SQL 执行失败：%s" % msg)
        except sqlite3.DatabaseError as e:
            if denials:
                raise DataSourceError("引擎拦截：" + "；".join(denials))
            raise DataSourceError("SQL 执行失败：%s" % e)
        finally:
            try:
                conn.set_progress_handler(None, 0)
                conn.set_authorizer(None)
            except Exception:                             # noqa: BLE001
                pass
            conn.close()


def _quote(ident):
    return '"%s"' % ident.replace('"', '""')
