# -*- coding: utf-8 -*-
"""自动识别检查：列角色、指标词匹配、结构→方法推荐。

Excel 通道没有表结构字典，列名是用户随手写的。这里验的是三件事能不能扛住
真实表格的各种写法，以及"用户不指定方法"时给出的默认动作是否说得通。

用法：
    python tests/check_auto.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pandas as pd

from core import config, schema_infer
from core.pipeline import answer_frame
from core.stats_router import classify_columns, route
from core.intent import Intent

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok), detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                        ("  · " + str(detail)) if detail else ""))


def _quarter_frame():
    rows = []
    for y in range(2018, 2025):
        for q in range(1, 5):
            if y == 2024 and q > 3:
                continue
            rows.append({"周期": "%d第%d季度" % (y, q),
                         "生产总值-累计值": 10000 + (y - 2018) * 900 + q * 120,
                         "同比增值 (%)": 5.0 + (y - 2018) * 0.3})
    return pd.DataFrame(rows)


def _panel_frame():
    rows = []
    for i in range(40):
        terrain = "平原" if i % 2 == 0 else "山地"
        for y in range(2021, 2024):
            rows.append({"县名称": "县%02d" % i, "年份": y, "地形": terrain,
                         "居民人均可支配收入(元)": 18000 + i * 90 + (y - 2021) * 500,
                         "地区生产总值(万元)": 900000 + i * 5000 + (y - 2021) * 30000})
    return pd.DataFrame(rows)


def _county_like():
    return pd.DataFrame({
        "县名称": ["县%02d" % i for i in range(48)],
        "地形": ["平原", "丘陵", "山地"] * 16,
        "地区生产总值(万元)": [900000 + i * 3000 for i in range(48)],
        "居民人均可支配收入(元)": [18000 + i * 120 for i in range(48)],
        "农业总产值(万元)": [300000 + i * 800 for i in range(48)],
    })


def main():
    # ---- 1. 列角色
    q = _quarter_frame()
    roles = schema_infer.guess_roles(q)
    check("季度表：认出时间列「周期」", roles["time"] == "周期", roles["time"])
    check("季度表：识别 27 个时期", roles["n_periods"] == 27, roles["n_periods"])
    check("季度表：水平值排在比率列前面",
          schema_infer.order_metrics(roles["metrics"])[0] == "生产总值-累计值",
          schema_infer.order_metrics(roles["metrics"]))
    check("季度表：两个数值指标", len(roles["metrics"]) == 2, roles["metrics"])

    p = _panel_frame()
    proles = schema_infer.guess_roles(p)
    check("面板表：认出时间列「年份」", proles["time"] == "年份", proles["time"])
    check("面板表：认出分组列「地形」", proles["group"] == "地形", proles["group"])
    check("面板表：个体列「县名称」不当分组",
          proles["unit"] == "县名称" and proles["n_units"] == 40,
          "%s/%s" % (proles["unit"], proles["n_units"]))

    age = pd.DataFrame({"年龄": [30, 41, 52, 28, 37], "收入": [9000, 12000, 15000, 8000, 11000]})
    check("「年龄」不被当成时间列",
          schema_infer.guess_roles(age)["time"] is None,
          schema_infer.guess_roles(age)["time"])

    # ---- 2. 指标词匹配
    cols = ["地区生产总值(万元)", "居民人均可支配收入(元)", "农业总产值(万元)"]
    col, said, _ = schema_infer.match_metric("居民收入怎么样", cols)
    check("「居民收入」→ 收入列", col == cols[1], "%s ← %s" % (col, said))
    col, _, _ = schema_infer.match_metric("GDP 最高的是哪个县", cols)
    check("「GDP」→ 生产总值列", col == cols[0], col)
    col, _, _ = schema_infer.match_metric("农业产值有多少", cols)
    check("「农业产值」→ 农业列", col == cols[2], col)
    col, _, _ = schema_infer.match_metric("人均GDP 是多少", ["gdp", "人均gdp"])
    check("「人均GDP」优先于 GDP", col == "人均gdp", col)
    col, _, _ = schema_infer.match_metric("随便看看", cols)
    check("没点名指标时返回 None（不瞎猜）", col is None, col)

    # ---- 3. 结构 → 方法
    c = _county_like()
    rec = schema_infer.recommend(c, None, "")
    check("有分组列 → 分组比较", rec["intent"] == "group", rec["reason"])
    rec_q = schema_infer.recommend(q, None, "")
    check("有 ≥8 期时间列 → 时序", rec_q["intent"] == "timeseries", rec_q["reason"])
    corr = pd.DataFrame({"收入": [1, 2, 3, 5, 8, 13], "产值": [2, 3, 5, 8, 13, 21]})
    check("多数值、无分组无时间 → 相关",
          schema_infer.recommend(corr, None, "")["intent"] == "corr")
    one = pd.DataFrame({"收入": [1, 2, 3, 5, 8, 13]})
    check("单指标 → 描述统计",
          schema_infer.recommend(one, None, "")["intent"] == "describe")

    # ---- 4. 路由接入：本地列名的表也能选对指标/分组
    plan = route(c, Intent("group", "rule", "test"), "不同地形的县收入有差异吗")
    check("路由：分组列取「地形」", (plan.params or {}).get("group") == "地形",
          (plan.params or {}).get("group"))
    check("路由：指标取收入列",
          (plan.params or {}).get("value") == "居民人均可支配收入(元)",
          (plan.params or {}).get("value"))

    plan2 = route(q, Intent("timeseries", "rule", "test"), "预测未来三年生产总值")
    check("路由：时序指标取「生产总值-累计值」",
          (plan2.params or {}).get("value") == "生产总值-累计值",
          (plan2.params or {}).get("value"))

    # ---- 5. 端到端：不写问题时按结构自动选方法
    r = answer_frame(q, "", persist=False)
    check("空问题 + 季度表 → 自动走时序",
          (r.intent or {}).get("intent") == "timeseries",
          "%s / %s" % ((r.intent or {}).get("intent"), (r.intent or {}).get("detected_by")))
    check("自动意图标记为 structure",
          (r.intent or {}).get("detected_by") == "structure",
          (r.intent or {}).get("detected_by"))
    check("季度表报告有内容", bool((r.report or {}).get("markdown")),
          (r.report or {}).get("source"))
    check("季度表出图", len(r.charts or []) >= 1, len(r.charts or []))

    # ---- 6. 面板 + 分组：自动只取最新一期
    r2 = answer_frame(p, "", persist=False)
    check("面板表：自动选出分组比较",
          (r2.intent or {}).get("intent") == "group",
          (r2.intent or {}).get("reason"))
    steps = [s.get("step") for s in (r2.plan or {}).get("steps", [])]
    check("面板表：做了截面化", "截面化" in steps, steps)
    check("面板表：样本从 120 缩到 40", r2.n_rows == 40, r2.n_rows)
    check("面板表：报告里说明只取最新一期",
          "最新一期" in (r2.report or {}).get("markdown", ""),
          "见报告")

    # ---- 7. 用户明确说"看分布"时不被结构覆盖
    r3 = answer_frame(c, "看看居民收入的分布", persist=False)
    check("用户说「分布」→ 不被结构改成分组比较",
          (r3.intent or {}).get("intent") == "describe",
          (r3.intent or {}).get("intent"))

    # ---- 8. 识别结果进了决策轨迹
    names = [s.get("step") for s in (r2.plan or {}).get("steps", [])]
    check("决策轨迹含「识别数据角色」", "识别数据角色" in names, names)

    failed = [n for n, ok, _ in _checks if not ok]
    print("\n%d 项检查，%d 通过，%d 失败" % (len(_checks), len(_checks) - len(failed), len(failed)))
    for n in failed:
        print("  未通过：%s" % n)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
