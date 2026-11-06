# -*- coding: utf-8 -*-
"""导出一张示例 Excel，给 `python main.py excel data/raw/county_sample.xlsx` 用。

选 2023 年截面 + 中文列名 + 地形/贫困县两个分类列：这样同一张表既能演示
描述统计，也能演示分组比较和相关分析，不用再准备第二张表。
"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import pandas as pd

from core import config

OUT = os.path.join(config.DATA_DIR, "raw", "county_sample.xlsx")

SQL = """
SELECT e.county_name AS 县名,
       e.province    AS 省份,
       e.city        AS 地级市,
       e.year        AS 年份,
       e.gdp         AS 地区生产总值万元,
       e.income      AS 居民人均可支配收入元,
       e.agri_output AS 农业总产值万元,
       e.finance_exp AS 财政支出万元,
       e.population  AS 年末人口万人,
       e.secondary_ratio AS 第二产业占比,
       i.terrain     AS 地形,
       CASE i.is_poverty_county WHEN 1 THEN '贫困县' ELSE '非贫困县' END AS 贫困县
FROM county_economy e
JOIN county_info i ON e.county_id = i.county_id
WHERE e.year = 2023
ORDER BY e.gdp DESC
"""


def main():
    conn = sqlite3.connect(config.DB_PATH)
    try:
        df = pd.read_sql_query(SQL, conn)
    finally:
        conn.close()

    if not os.path.isdir(os.path.dirname(OUT)):
        os.makedirs(os.path.dirname(OUT))
    df.to_excel(OUT, index=False)
    print("已导出 %s：%d 行 × %d 列" % (OUT, len(df), len(df.columns)))
    print("列名：%s" % "、".join(df.columns))
    return 0


if __name__ == "__main__":
    sys.exit(main())
