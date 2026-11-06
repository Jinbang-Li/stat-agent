# -*- coding: utf-8 -*-
"""
把 data/raw/*.csv 建成单文件 SQLite 数据库：data/stat_agent.db

为什么用 SQLite：
  - 单文件，clone 下来即可运行，不需要装 MySQL/PostgreSQL；
  - 支持 set_authorizer，可以在引擎层面做 SQL 语句白名单（见 core/nlsql.py）；
  - 数据量（几十万行以内）完全够用。

运行：python scripts/build_db.py
"""

import csv
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
RAW = os.path.join(ROOT, "data", "raw")
DB = os.path.join(ROOT, "data", "stat_agent.db")

DDL = """
DROP TABLE IF EXISTS county_economy;
CREATE TABLE county_economy (
    county_id       TEXT    NOT NULL,   -- 县编号，如 CN-HB-01
    county_name     TEXT    NOT NULL,   -- 县名
    province        TEXT    NOT NULL,   -- 省
    year            INTEGER NOT NULL,   -- 年份
    gdp             REAL,               -- 地区生产总值，万元
    income          REAL,               -- 居民人均可支配收入，元
    agri_output     REAL,               -- 农业总产值，万元
    finance_exp     REAL,               -- 一般公共预算支出，万元
    population      REAL,               -- 常住人口，万人
    secondary_ratio REAL                -- 第二产业增加值占 GDP 比重，%
);

DROP TABLE IF EXISTS county_info;
CREATE TABLE county_info (
    county_id         TEXT PRIMARY KEY, -- 县编号
    county_name       TEXT NOT NULL,    -- 县名
    province          TEXT NOT NULL,    -- 省
    city              TEXT NOT NULL,    -- 所属地级市
    terrain           TEXT NOT NULL,    -- 地形：平原/丘陵/山地
    is_poverty_county INTEGER NOT NULL, -- 是否原国家级贫困县：1/0
    area_km2          INTEGER           -- 行政区域面积，平方公里
);

CREATE INDEX idx_econ_county_year ON county_economy(county_id, year);
CREATE INDEX idx_econ_year ON county_economy(year);
CREATE INDEX idx_info_province ON county_info(province);
"""


def load_csv(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def to_db_value(v):
    if v is None or v == "":
        return None
    try:
        return float(v) if "." in str(v) else int(v)
    except ValueError:
        return v


def main():
    econ = load_csv(os.path.join(RAW, "county_economy.csv"))
    info = load_csv(os.path.join(RAW, "county_info.csv"))

    if os.path.exists(DB):
        os.remove(DB)

    conn = sqlite3.connect(DB)
    conn.executescript(DDL)

    econ_cols = ["county_id", "county_name", "province", "year", "gdp", "income",
                 "agri_output", "finance_exp", "population", "secondary_ratio"]
    econ_rows = []
    for r in econ:
        row = [to_db_value(r[c]) for c in econ_cols]
        # year 必须为整数
        row[3] = int(row[3])
        econ_rows.append(row)

    conn.executemany(
        "INSERT INTO county_economy (%s) VALUES (%s)"
        % (",".join(econ_cols), ",".join(["?"] * len(econ_cols))),
        econ_rows,
    )

    info_cols = ["county_id", "county_name", "province", "city", "terrain",
                 "is_poverty_county", "area_km2"]
    info_rows = []
    for r in info:
        row = [to_db_value(r[c]) for c in info_cols]
        row[5] = int(row[5])
        info_rows.append(row)

    conn.executemany(
        "INSERT INTO county_info (%s) VALUES (%s)"
        % (",".join(info_cols), ",".join(["?"] * len(info_cols))),
        info_rows,
    )
    conn.commit()

    print("数据库: %s" % DB)
    for t in ("county_economy", "county_info"):
        n = conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        print("  %-16s %d 行" % (t, n))
    yr = conn.execute("SELECT MIN(year), MAX(year) FROM county_economy").fetchone()
    print("  年份范围: %s - %s" % yr)
    conn.close()


if __name__ == "__main__":
    main()
