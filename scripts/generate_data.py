# -*- coding: utf-8 -*-
"""
生成县域经济模拟面板数据。

数据来源声明（README 中同样会写明）：
  - 县名、所属省市取自公开行政区划信息，本身不含统计数据；
  - 各项经济指标（gdp / income / agri_output / finance_exp / population）
    由带真实统计量级的生成模型模拟，**不是真实统计年鉴数值**；
  - 随机种子固定，任何人 clone 后运行结果完全一致，可复现。

之所以不直接给"真实"数据：县域统计年鉴的完整面板不公开、口径年年变化，
强行拼凑会得到一个无法复现也无法解释的数据集——那反而是研究者的大忌。

运行：python scripts/generate_data.py
输出：data/raw/county_economy.csv
      data/raw/county_info.csv
"""

import csv
import math
import os
import random

import numpy as np

SEED = 20240601
N_YEARS = 9          # 2015 - 2023
YEAR_START = 2015

# ---------------------------------------------------------------- 行政区划
# (县名, 省, 地市)  取公开行政区划，不含任何统计数据
COUNTIES = [
    ("枣阳市", "湖北", "襄阳"), ("宜城市", "湖北", "襄阳"), ("老河口市", "湖北", "襄阳"),
    ("南漳县", "湖北", "襄阳"), ("谷城县", "湖北", "襄阳"), ("保康县", "湖北", "襄阳"),
    ("监利市", "湖北", "荆州"), ("松滋市", "湖北", "荆州"), ("石首市", "湖北", "荆州"),
    ("洪湖市", "湖北", "荆州"), ("公安县", "湖北", "荆州"), ("江陵县", "湖北", "荆州"),
    ("京山市", "湖北", "荆门"), ("沙洋县", "湖北", "荆门"), ("钟祥市", "湖北", "荆门"),
    ("云梦县", "湖北", "孝感"), ("应城市", "湖北", "孝感"), ("安陆市", "湖北", "孝感"),
    ("汉川市", "湖北", "孝感"), ("大悟县", "湖北", "孝感"), ("孝昌县", "湖北", "孝感"),
    ("黄梅县", "湖北", "黄冈"), ("蕲春县", "湖北", "黄冈"), ("麻城市", "湖北", "黄冈"),
    ("台山市", "广东", "江门"), ("开平市", "广东", "江门"), ("鹤山市", "广东", "江门"),
    ("恩平市", "广东", "江门"), ("兴宁市", "广东", "梅州"), ("五华县", "广东", "梅州"),
    ("大埔县", "广东", "梅州"), ("丰顺县", "广东", "梅州"), ("龙川县", "广东", "河源"),
    ("紫金县", "广东", "河源"), ("连平县", "广东", "河源"), ("和平县", "广东", "河源"),
    ("阳春市", "广东", "阳江"), ("阳西县", "广东", "阳江"), ("信宜市", "广东", "茂名"),
    ("高州市", "广东", "茂名"), ("化州市", "广东", "茂名"), ("雷州市", "广东", "湛江"),
    ("廉江市", "广东", "湛江"), ("吴川市", "广东", "湛江"), ("英德市", "广东", "清远"),
    ("连州市", "广东", "清远"), ("佛冈县", "广东", "清远"), ("南雄市", "广东", "韶关"),
]

# 地形：按真实地理归类（湖北 24 + 广东 24，比例接近 平原 12 / 丘陵 18 / 山地 18）。
# 之所以要三个组而不是两个，是因为"组间差异"的演示需要 k>=3 才体现得出
# 单因素方差分析 / Kruskal-Wallis 的价值；两组只能做 t 检验，说明不了什么问题。
HILLY = {"枣阳市", "宜城市", "松滋市", "京山市", "钟祥市", "安陆市", "孝昌县",
         "黄梅县", "台山市", "开平市", "鹤山市", "恩平市", "兴宁市", "阳西县",
         "高州市", "化州市", "廉江市", "南雄市"}
MOUNTAIN = {"南漳县", "谷城县", "保康县", "大悟县", "蕲春县", "麻城市",
            "五华县", "大埔县", "丰顺县", "龙川县", "紫金县", "连平县",
            "和平县", "阳春市", "信宜市", "英德市", "连州市", "佛冈县"}

# 2023 年被人为放大的县（模拟"口径变更/数据录入异常"），用于演示异常检测
OUTLIER_COUNTIES = {"钟祥市", "高州市", "廉江市"}
OUTLIER_FACTOR = 2.5


def terrain_of(name):
    if name in MOUNTAIN:
        return "山地"
    if name in HILLY:
        return "丘陵"
    return "平原"


def build():
    rng = np.random.RandomState(SEED)
    random.seed(SEED)

    rows = []
    info_rows = []

    for idx, (name, prov, city) in enumerate(COUNTIES):
        cid = "CN-%s-%02d" % ("HB" if prov == "湖北" else "GD", idx + 1)
        terrain = terrain_of(name)

        # 县域潜在经济活力，决定 GDP / 产业结构的共同水平
        z = float(rng.normal(0, 1))
        # 人口有独立的区划因素（面积、行政层级），不与经济活力完全共线
        v = float(rng.normal(0, 1))
        # 农业规模有独立的自然禀赋因素（耕地、气候），刻意与经济活力弱耦合
        w = float(rng.normal(0, 1))

        terrain_effect = {"平原": 0.0, "丘陵": -0.35, "山地": -0.75}[terrain]

        pop_base = math.exp(3.35 + 0.16 * z + 0.28 * v + 0.10 * terrain_effect)   # 万人
        # 先定人均 GDP（元/人），再由 人口 x 人均 反推总量——比分别抽样更不容易出现离谱组合
        pc_gdp = math.exp(10.20 + 0.26 * z + 0.10 * terrain_effect)                # 元/人，2015 基年
        # 兜住业务边界：2015 年县域人均 GDP 大致在 1.6 万 ~ 5.2 万元/人之间
        pc_gdp = min(52000.0, max(16000.0, pc_gdp))
        growth = float(rng.normal(0.068, 0.012))                                   # 年均名义增速
        income_ratio = float(rng.normal(0.44, 0.045))                              # 收入/人均GDP
        # 平原农业规模大；与经济活力仅弱负相关（农业县往往工业弱、收入低，但远非定式）
        agri_base = math.exp(12.90 + 0.28 * terrain_effect + 0.55 * w - 0.12 * z)  # 万元
        fiscal_ratio = float(rng.normal(0.085, 0.018))
        secondary = 36 + 17 * z + float(rng.normal(0, 6))

        area = round(math.exp(6.9 + rng.normal(0, 0.40)), 0)           # km2
        is_poverty = 1 if (terrain == "山地" and rng.rand() < 0.75) or \
                          (terrain == "丘陵" and rng.rand() < 0.30) else 0

        # 原贫困县的经济基础更弱：财政转移支付依赖度高、产业以初级部门为主。
        # 给 GDP 与居民收入一个 14% 的折价——这是数据里**真实存在**的组间差异，
        # 用来支撑"贫困县与非贫困县收入差异是否显著"的分组演示。
        # 注意：地形与收入之间刻意**不**留显著差异（见 terrain_effect 只有 0.10），
        # 这样同一个分组检验模块既能演示"显著"，也能演示"不显著也要如实说"。
        pov_factor = 0.86 if is_poverty else 1.0

        info_rows.append({
            "county_id": cid, "county_name": name, "province": prov,
            "city": city, "terrain": terrain,
            "is_poverty_county": is_poverty, "area_km2": int(area),
        })

        for t in range(N_YEARS):
            year = YEAR_START + t
            # 增长路径 + 短期波动
            shock = float(rng.normal(0, 0.018))
            pop = pop_base * (1 + 0.004 * t) * (1 + rng.normal(0, 0.006))
            pc = pc_gdp * ((1 + growth) ** t) * (1 + shock)          # 元/人
            gdp = pc * pop * pov_factor                              # 万元 = 元/人 x 万人

            income = pc * income_ratio * (1 + 0.012 * t) * pov_factor
            income *= (1 + rng.normal(0, 0.025))

            agri = agri_base * ((1 + growth * 0.72) ** t) * (1 + rng.normal(0, 0.05))
            fiscal = gdp * fiscal_ratio * (1 + rng.normal(0, 0.06))
            sec = min(78.0, max(12.0, secondary + rng.normal(0, 2.2) + 0.15 * t))

            if name in OUTLIER_COUNTIES and year == 2023:
                gdp *= OUTLIER_FACTOR

            rows.append({
                "county_id": cid,
                "county_name": name,
                "province": prov,
                "year": year,
                "gdp": round(gdp, 1),
                "income": round(income, 1),
                "agri_output": round(agri, 1),
                "finance_exp": round(fiscal, 1),
                "population": round(pop, 2),
                "secondary_ratio": round(sec, 2),
            })

    # ---- 注入缺失值：真实统计年鉴里指标缺失很常见，占比约 3%
    miss_targets = ["income", "agri_output", "finance_exp", "secondary_ratio"]
    for col in miss_targets:
        n_missing = int(len(rows) * 0.03)
        for i in rng.choice(len(rows), n_missing, replace=False):
            rows[int(i)][col] = None

    return rows, info_rows


def write_csv(path, rows, fields):
    # utf-8-sig：保证 Excel 直接双击打开不乱码
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, "..", "data", "raw")
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    rows, info_rows = build()

    write_csv(os.path.join(out_dir, "county_economy.csv"), rows,
              ["county_id", "county_name", "province", "year", "gdp", "income",
               "agri_output", "finance_exp", "population", "secondary_ratio"])
    write_csv(os.path.join(out_dir, "county_info.csv"), info_rows,
              ["county_id", "county_name", "province", "city", "terrain",
               "is_poverty_county", "area_km2"])

    print("county_economy.csv : %d 行 x %d 列" % (len(rows), 10))
    print("county_info.csv    : %d 行 x %d 列" % (len(info_rows), 7))
    print("年份范围           : %d - %d" % (YEAR_START, YEAR_START + N_YEARS - 1))


if __name__ == "__main__":
    main()
