# -*- coding: utf-8 -*-
"""图表层检查：SVG 必须自洽。

跑法：cd stat_agent && python tests/check_charts.py

为什么单独写这个：图表"看起来有输出"和"画对了"是两件事。
之前存在过一个 bug——坐标轴上界没有覆盖数据最大值，最大的几个点被画到了
画布外面（cy=-65 这种坐标）。肉眼扫一眼 SVG 文件根本发现不了，
所以这里直接把 circle 的坐标解析出来，检查它们是否都落在绘图区内。
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import charts                    # noqa: E402

PASS, FAIL = [], []
PAD_L, PAD_R, PAD_T, PAD_B = 64, 24, 44, 52
W, H = 660, 360


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s %s" % ("ok  " if cond else "FAIL", name, detail))


def circles(svg):
    return [(float(a), float(b)) for a, b in
            re.findall(r'<circle cx="(-?[\d.]+)" cy="(-?[\d.]+)"', svg)]


def inside(pts):
    return [p for p in pts
            if not (PAD_L - 0.5 <= p[0] <= W - PAD_R + 0.5
                    and PAD_T - 0.5 <= p[1] <= H - PAD_B + 0.5)]


def main():
    print("== 1. 散点：坐标轴必须包住极值 ==")
    # 真实场景：收入 1 万~4.6 万，农业产值 11 万~147 万。量纲差别很大，
    # 正好用来验证刻度上界是否被抬高到覆盖最大值。
    pts = [[10000 + i * 380, 120000 + (i * 79000) % 1300000] for i in range(48)]
    svg = charts.render({"type": "scatter", "title": "t", "points": pts,
                         "x_label": "x", "y_label": "y",
                         "fit": [[10000, 200000], [46000, 900000]]})
    cs = circles(svg)
    check("圆点数量与数据一致", len(cs) == len(pts), "%d/%d" % (len(cs), len(pts)))
    out = inside(cs)
    check("没有点被画到绘图区外", not out, out[:3])

    print("== 2. 单点 / 常量序列不能崩 ==")
    one = charts.render({"type": "scatter", "points": [[5, 5]], "title": "t"})
    check("单点也能出图", "<circle" in one)
    flat = charts.render({"type": "line", "title": "t",
                          "series": [{"name": "a", "points": [[2020, 7], [2021, 7], [2022, 7]]}]})
    check("常量序列不除零", "<svg" in flat and "无数据" not in flat)

    print("== 3. 空数据要说人话 ==")
    empty = charts.render({"type": "scatter", "points": [], "title": "t"})
    check("空散点返回占位图", "无数据" in empty)

    print("== 4. 直方图 / 柱状图 ==")
    hist = charts.render({"type": "histogram", "title": "t",
                          "values": [15000 + (i * 733) % 20000 for i in range(48)]})
    check("直方图有矩形", hist.count("<rect") >= 10, hist.count("<rect"))
    bar = charts.render({"type": "bar", "title": "t",
                         "categories": ["非贫困县", "贫困县"], "values": [22398.27, 18576.61]})
    check("柱状图标签不被截断", "非贫困县" in bar)

    print("\n结果：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
