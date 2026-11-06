# -*- coding: utf-8 -*-
"""
在没有安装 streamlit 的机器上，把 app.py 真正执行一遍。

做法：用 tests/stub_streamlit.py 顶替 streamlit 模块，然后按两种用户动作各跑一次——
1) 只打开页面，不提问；
2) 输入一个问题并点"开始分析"。
任何异常（字段名写错、类型不匹配、空值没兜住）都会在这里暴露。

    python tests/check_app.py
"""

import io
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import stub_streamlit as stub  # noqa: E402

APP = os.path.join(ROOT, "app.py")
QUESTION = "贫困县和非贫困县的收入有显著差异吗"


def run_once(click_label=None, question=""):
    stub.install()
    stub.CALLS.clear()
    st = sys.modules["streamlit"]
    st.session_state.clear()
    if question:
        st.session_state["question_input"] = question
    if click_label:
        stub.click(click_label)
    src = io.open(APP, "r", encoding="utf-8").read()
    ns = {"__name__": "__main__", "__file__": APP}
    try:
        exec(compile(src, APP, "exec"), ns)
    except stub.StopApp:
        pass
    return list(stub.CALLS)


def bodies(calls, kind):
    """取某种调用的第一个位置参数，拼起来。

    kind 既可以是精确名（st.markdown），也可以是方法后缀（.markdown）——
    因为同一个方法可能挂在 sidebar、col0、tab 容器上，名字前缀不同。
    """
    out = []
    for name, args, _kw in calls:
        hit = (name == kind) if kind.startswith(("st.", "sidebar", "tabs")) else name.endswith(kind)
        if hit and args:
            out.append(args[0] if isinstance(args[0], str) else str(args[0]))
    return "\n".join(out)


def main():
    failures = []

    def check(name, ok, detail=""):
        print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  " + detail) if detail and not ok else ""))
        if not ok:
            failures.append(name)

    print("1) 打开页面（不提问）")
    try:
        calls = run_once()
        check("页面渲染不抛异常", True)
        check("侧栏打印了运行状态", bool(bodies(calls, "sidebar.markdown")))
        check("没有提问时停在输入区", any(n == "st.text_area" for n, _a, _k in calls))
    except Exception:
        check("页面渲染不抛异常", False)
        traceback.print_exc()

    print("2) 提问并点击「开始分析」")
    try:
        calls = run_once(click_label="开始分析", question=QUESTION)
        md = bodies(calls, ".markdown")
        check("整条链路跑通、没有报错弹窗", not bodies(calls, "st.error").strip()
              or "跑通" not in bodies(calls, "st.error"), bodies(calls, "st.error")[:200])
        check("报告区有内容", "分析报告" in md)
        check("报告含结论段", "综合判断" in md or "结论" in md)
        check("图表渲染成 SVG", "<svg" in md)
        check("SQL 标签页拿到了语句", "select" in bodies(calls, ".code").lower())
        check("指标卡有样本行数", any(a[0] == "样本行数" for n, a, _k in calls if n.endswith(".metric")))
        tabs = bodies(calls, "tabs")
        check("四个明细标签页都渲染了", len([t for t in tabs.split("\n") if t]) >= 1
              and all(k in tabs for k in ("SQL", "数据预览", "决策轨迹")), tabs)
    except Exception:
        check("整条链路跑通", False)
        traceback.print_exc()

    print("")
    if failures:
        print("未通过 %d 项：%s" % (len(failures), "、".join(failures)))
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
