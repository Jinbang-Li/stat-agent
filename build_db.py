# -*- coding: utf-8 -*-
"""
建库脚本：data/raw/*.csv → data/stat_agent.db（SQLite 单文件）

为什么用 SQLite，而不是 MySQL / PostgreSQL
------------------------------------------
1. 一个文件，clone 下来就能跑，评审老师不需要装任何服务；
2. 支持 `set_authorizer`，可以在引擎层面做 SQL 白名单（见 core/datasource/sqlite_source.py），
   这比在字符串层面做黑名单可靠得多；
3. 本项目的数据量（几万行以内）用 SQLite 完全够，换成 MySQL 只增加部署成本。

表结构
------
主表 county_economy（规格要求的那张表）::

    county_id      TEXT     县区代码，如 CN-HB-01
    county_name    TEXT     县区名称
    city           TEXT     所属地级市
    year           INTEGER  年份
    gdp            REAL     地区生产总值（万元）
    income         REAL     居民人均可支配收入（元）
    agri_output    REAL     农业总产值（万元）
    finance_exp    REAL     一般公共预算支出（万元）
    population     REAL     常住人口（万人）

在主表之外多留了三列 / 一张表，都是"演示需要"而不是"设计缺陷"：

    province        省份。跨省比较、按省分组都要用它；不放进主表就得每次 join。
    secondary_ratio 第二产业占比。聚类演示需要至少两个"结构"类指标，
                    只用 gdp / population 聚出来的是"大县 / 小县"，没有分析价值。
    county_info     县维度表（地形、是否贫困县、面积）。"贫困县与非贫困县的收入
                    是否有显著差异"这类分组检验，缺少这个维度就没法演示——
                    而这恰好是最能体现统计严谨性的场景。

运行::

    python build_db.py            # 建库并做数据体检
    python build_db.py --check    # 只体检，不重建

体检不通过（例如出现重复主键、年份缺失、人均 GDP 明显失真）时返回码非 0，
避免"带着脏数据往下跑"。
"""

import argparse
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(HERE, "data", "raw")
DB_PATH = os.path.join(HERE, "data", "stat_agent.db")

ECON_CSV = os.path.join(RAW_DIR, "county_economy.csv")
INFO_CSV = os.path.join(RAW_DIR, "county_info.csv")

ECON_COLUMNS = ["county_id", "county_name", "province", "city", "year", "gdp",
                "income", "agri_output", "finance_exp", "population",
                "secondary_ratio"]
INFO_COLUMNS = ["county_id", "county_name", "province", "city", "terrain",
                "is_poverty_county", "area_km2"]

DDL = """
CREATE TABLE county_economy (
    county_id       TEXT    NOT NULL,
    county_name     TEXT    NOT NULL,
    province        TEXT    NOT NULL,
    city            TEXT,
    year            INTEGER NOT NULL,
    gdp             REAL,
    income          REAL,
    agri_output     REAL,
    finance_exp     REAL,
    population      REAL,
    secondary_ratio REAL,
    PRIMARY KEY (county_id, year)
);

CREATE TABLE county_info (
    county_id         TEXT PRIMARY KEY,
    county_name       TEXT NOT NULL,
    province          TEXT NOT NULL,
    city              TEXT NOT NULL,
    terrain           TEXT NOT NULL,
    is_poverty_county INTEGER NOT NULL,
    area_km2          INTEGER
);

CREATE INDEX idx_econ_county_year ON county_economy(county_id, year);
CREATE INDEX idx_econ_year        ON county_economy(year);
CREATE INDEX idx_econ_city        ON county_economy(city);
CREATE INDEX idx_info_province    ON county_info(province);
"""

INT_COLUMNS = {"year", "is_poverty_county", "area_km2"}


# ---------------------------------------------------------------- 读取
def _read_csv(path, required=True):
    if not os.path.isfile(path):
        if required:
            raise SystemExit("缺少数据文件：%s\n（先跑 scripts/generate_data.py 生成样例数据）" % path)
        return []
    import csv
    with open(path, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _value(col, raw):
    """空字符串一律转 None（NULL），不要写成 0——0 和"没测到"是两件事。"""
    if raw is None:
        return None
    raw = str(raw).strip()
    if raw == "" or raw.lower() in ("nan", "null", "none"):
        return None
    if col in INT_COLUMNS:
        try:
            return int(float(raw))
        except ValueError:
            return None
    try:
        return float(raw)
    except ValueError:
        return raw


def build(db_path=DB_PATH):
    econ = _read_csv(ECON_CSV)
    info = _read_csv(INFO_CSV, required=False)
    if not econ:
        raise SystemExit("county_economy.csv 是空的")

    # 地级市从维度表补进主表：主表自己不带 city 列时也能建出规格要求的字段。
    city_of = {}
    for r in info:
        cid = (r.get("county_id") or "").strip()
        if cid:
            city_of[cid] = (r.get("city") or "").strip() or None

    rows = []
    for r in econ:
        cid = (r.get("county_id") or "").strip()
        row = []
        for col in ECON_COLUMNS:
            if col == "city" and not (r.get("city") or "").strip():
                row.append(city_of.get(cid))
            else:
                row.append(_value(col, r.get(col)))
        rows.append(row)

    info_rows = []
    for r in info:
        info_rows.append([_value(c, r.get(c)) for c in INFO_COLUMNS])

    # 先写临时文件再原子替换：中途出错不会留下半个库，
    # 也不会出现"旧库删了、新库没建完"的空窗。
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db", dir=os.path.dirname(db_path))
    os.close(tmp_fd)
    try:
        conn = sqlite3.connect(tmp_path)
        conn.executescript(DDL)
        conn.executemany(
            "INSERT INTO county_economy (%s) VALUES (%s)"
            % (",".join(ECON_COLUMNS), ",".join(["?"] * len(ECON_COLUMNS))), rows)
        if info_rows:
            conn.executemany(
                "INSERT INTO county_info (%s) VALUES (%s)"
                % (",".join(INFO_COLUMNS), ",".join(["?"] * len(INFO_COLUMNS))), info_rows)
        conn.commit()
        conn.close()
        os.replace(tmp_path, db_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    print("数据库：%s" % db_path)
    print("  主表 county_economy  %d 行 × %d 列" % (len(rows), len(ECON_COLUMNS)))
    if info_rows:
        print("  维度表 county_info   %d 行" % len(info_rows))
    return db_path


# ---------------------------------------------------------------- 体检
def check(db_path=DB_PATH):
    """建库后立刻做的数据体检。

    这些检查对应的是"会安静地把结论带偏"的问题：重复行会让 n 虚高、p 值被压低；
    缺失率过高会让均值失去代表性；人均 GDP 出现 17 万元这种值则说明单位换算错了。
    """
    if not os.path.isfile(db_path):
        raise SystemExit("数据库不存在：%s" % db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    problems = []
    warnings = []

    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    print("\n表：%s" % ", ".join(tables))

    n = conn.execute("SELECT COUNT(*) FROM county_economy").fetchone()[0]
    n_county = conn.execute("SELECT COUNT(DISTINCT county_id) FROM county_economy").fetchone()[0]
    yr = conn.execute("SELECT MIN(year), MAX(year), COUNT(DISTINCT year) FROM county_economy").fetchone()
    print("  county_economy: %d 行, %d 个县, 年份 %s-%s（%d 个年度）"
          % (n, n_county, yr[0], yr[1], yr[2]))

    # 1. 主键唯一
    dup = conn.execute(
        "SELECT COUNT(*) FROM (SELECT county_id, year FROM county_economy "
        "GROUP BY county_id, year HAVING COUNT(*) > 1)").fetchone()[0]
    if dup:
        problems.append("主键重复：%d 组 (county_id, year)" % dup)

    # 2. 每县年份覆盖是否完整（缺年份会让时序方法和同比计算失真）
    gaps = conn.execute(
        "SELECT COUNT(*) FROM (SELECT county_id, COUNT(DISTINCT year) c FROM county_economy "
        "GROUP BY county_id HAVING c < ?)", (yr[2],)).fetchone()[0]
    if gaps:
        warnings.append("%d 个县的年份不完整（少于 %d 个年度），时序分析会受影响" % (gaps, yr[2]))

    # 3. 缺失率
    cols = [r[1] for r in conn.execute("PRAGMA table_info(county_economy)")]
    num_cols = [c for c in cols if c not in ("county_id", "county_name", "province", "city")]
    print("\n  缺失率：")
    for c in num_cols:
        miss = conn.execute("SELECT COUNT(*) FROM county_economy WHERE %s IS NULL" % c).fetchone()[0]
        rate = miss / float(n) if n else 0
        flag = ""
        if rate > 0.30:
            flag = "  ← 超过 30%，该列不宜单独支撑结论"
            warnings.append("%s 缺失率 %.1f%%" % (c, rate * 100))
        print("    %-16s %5.1f%%%s" % (c, rate * 100, flag))

    # 4. 数值合理性（单位换算错误最典型的症状就是这一步挂掉）
    print("\n  取值区间：")
    for c in ("gdp", "income", "agri_output", "finance_exp", "population", "secondary_ratio"):
        if c not in num_cols:
            continue
        row = conn.execute(
            "SELECT MIN(%s), AVG(%s), MAX(%s) FROM county_economy WHERE %s IS NOT NULL" % (c, c, c, c)
        ).fetchone()
        print("    %-16s min=%-14s mean=%-14s max=%s"
              % (c, _fmt(row[0]), _fmt(row[1]), _fmt(row[2])))

    bad_pc = conn.execute(
        "SELECT COUNT(*) FROM county_economy WHERE population > 0 "
        "AND (gdp / population < 5000 OR gdp / population > 200000)").fetchone()[0]
    if bad_pc:
        problems.append("人均 GDP 超出 [0.5 万, 20 万] 的样本：%d 行（单位或口径可能错）" % bad_pc)

    bad_ratio = conn.execute(
        "SELECT COUNT(*) FROM county_economy WHERE secondary_ratio < 0 OR secondary_ratio > 100"
    ).fetchone()[0]
    if bad_ratio:
        problems.append("secondary_ratio 不在 [0,100] 的样本：%d 行" % bad_ratio)

    # 5. 维度表外键
    if "county_info" in tables:
        orphan = conn.execute(
            "SELECT COUNT(DISTINCT e.county_id) FROM county_economy e "
            "LEFT JOIN county_info i ON e.county_id = i.county_id WHERE i.county_id IS NULL"
        ).fetchone()[0]
        if orphan:
            problems.append("%d 个县在主表里存在、维度表里没有（join 后会丢样本）" % orphan)
        nc = conn.execute("SELECT COUNT(*) FROM county_info").fetchone()[0]
        print("\n  county_info: %d 行" % nc)

    conn.close()

    print("")
    for w in warnings:
        print("  [warn] %s" % w)
    for p in problems:
        print("  [FAIL] %s" % p)
    if not problems and not warnings:
        print("  体检通过：无阻断问题。")
    elif not problems:
        print("  体检通过（有告警，但可继续）。")
    else:
        print("  体检未通过：先修数据，再往下做分析。")
    return 1 if problems else 0


def _fmt(v):
    if v is None:
        return "NULL"
    if isinstance(v, float):
        return "%.2f" % v
    return str(v)


def main(argv=None):
    ap = argparse.ArgumentParser(description="建库并做数据体检")
    ap.add_argument("--check", action="store_true", help="只体检，不重建")
    ap.add_argument("--db", default=DB_PATH, help="数据库路径")
    args = ap.parse_args(argv)

    if not args.check:
        build(args.db)
    return check(args.db)


if __name__ == "__main__":
    sys.exit(main())
