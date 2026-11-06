# -*- coding: utf-8 -*-
"""查一下县名清单，以及某个县名到底在不在库里。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.datasource import default_source   # noqa: E402


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "监利市"
    src = default_source()
    ok, cols, rows, guard, note = __import__("core.nlsql", fromlist=["validate_and_execute"]) \
        .validate_and_execute("SELECT county_name, province FROM county_info ORDER BY county_name", src)
    names = [r[0] for r in rows]
    print("库里共 %d 个县：" % len(names))
    print("、".join(names))
    print("")
    print("查 %s → %s" % (name, "在库里" if name in names else "不在库里"))
    like = [n for n in names if name[:2] in n]
    print("名字相近的：%s" % (like or "无"))
    ok, cols, rows2, guard, note = __import__("core.nlsql", fromlist=["validate_and_execute"]) \
        .validate_and_execute("SELECT MIN(year), MAX(year), COUNT(*) FROM county_economy", src)
    print("年份范围：%s" % (rows2[0],))


if __name__ == "__main__":
    main()
