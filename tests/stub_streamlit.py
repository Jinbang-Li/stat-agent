# -*- coding: utf-8 -*-
"""
streamlit 的最小替身，只为了让 `app.py` 在没有 streamlit 的机器上也能被"执行一遍"。

这不是 UI 测试，它验证不了像素长什么样；它验证的是更实际的东西：
app.py 里没有拼错的字段名、没有对不上的数据类型、没有漏处理的空值、
从提问到渲染整条路径不会抛异常。

用法见 tests/check_app.py。
"""

import sys
import types


class StopApp(Exception):
    """对应 st.stop()：正常终止脚本，不代表出错。"""


CALLS = []          # [(dotted_name, args, kwargs)]，断言用
_CLICK = {"label": None}


def click(label):
    """让下一次 label 匹配的 button() 返回 True，用来模拟点击。"""
    _CLICK["label"] = label


class _Recorder(object):
    """任何没显式实现的方法都落到这里：记录调用，返回 None。"""

    def __init__(self, name):
        self._name = name

    def __getattr__(self, item):
        if item.startswith("__"):
            raise AttributeError(item)

        def _call(*a, **kw):
            CALLS.append(("%s.%s" % (self._name, item), a, kw))
            return None
        return _call


class DeltaGenerator(_Recorder):
    def __init__(self, name):
        _Recorder.__init__(self, name)

    # 上下文管理器：with st.spinner(...) / with st.sidebar.expander(...)
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def markdown(self, body="", **kw):
        CALLS.append(("%s.markdown" % self._name, (body,), kw))
        return self

    def caption(self, body="", **kw):
        CALLS.append(("%s.caption" % self._name, (body,), kw))
        return self

    def write(self, *a, **kw):
        CALLS.append(("%s.write" % self._name, a, kw))

    def code(self, body="", **kw):
        CALLS.append(("%s.code" % self._name, (body,), kw))

    def json(self, body=None, **kw):
        CALLS.append(("%s.json" % self._name, (body,), kw))

    def dataframe(self, body=None, **kw):
        CALLS.append(("%s.dataframe" % self._name, (body,), kw))

    def metric(self, label, value, **kw):
        CALLS.append(("%s.metric" % self._name, (label, value), kw))

    def button(self, label, **kw):
        CALLS.append(("%s.button" % self._name, (label,), kw))
        if _CLICK["label"] is not None and label == _CLICK["label"]:
            _CLICK["label"] = None
            return True
        return False

    def spinner(self, *a, **kw):
        CALLS.append(("%s.spinner" % self._name, a, kw))
        return self

    def expander(self, *a, **kw):
        CALLS.append(("%s.expander" % self._name, a, kw))
        return DeltaGenerator(self._name + ".expander")

    def container(self):
        return DeltaGenerator(self._name + ".container")


class _SessionState(dict):
    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError:
            raise AttributeError(item)

    def __setattr__(self, item, value):
        self[item] = value


def install():
    """把本模块伪装成 streamlit 塞进 sys.modules。"""
    m = types.ModuleType("streamlit")
    m.__version__ = "0.0.0-stub"

    m.session_state = _SessionState()
    m.sidebar = DeltaGenerator("sidebar")

    m.set_page_config = lambda **kw: CALLS.append(("set_page_config", (), kw))
    m.markdown = lambda body="", **kw: DeltaGenerator("st").markdown(body, **kw)
    m.caption = lambda body="", **kw: DeltaGenerator("st").caption(body, **kw)
    m.write = lambda *a, **kw: DeltaGenerator("st").write(*a, **kw)
    m.code = lambda body="", **kw: DeltaGenerator("st").code(body, **kw)
    m.json = lambda body=None, **kw: DeltaGenerator("st").json(body, **kw)
    m.dataframe = lambda body=None, **kw: DeltaGenerator("st").dataframe(body, **kw)
    m.metric = lambda label, value, **kw: DeltaGenerator("st").metric(label, value, **kw)
    m.button = lambda label, **kw: DeltaGenerator("st").button(label, **kw)
    m.spinner = lambda *a, **kw: DeltaGenerator("st").spinner(*a, **kw)
    m.container = lambda: DeltaGenerator("st").container()
    m.download_button = lambda *a, **kw: CALLS.append(("download_button", a, kw))

    def _notify(kind):
        def _f(body="", **kw):
            CALLS.append(("st." + kind, (body,), kw))
        return _f

    for kind in ("warning", "error", "info", "success"):
        setattr(m, kind, _notify(kind))

    def _text_area(label, **kw):
        CALLS.append(("st.text_area", (label,), kw))
        return kw.get("value", "")

    m.text_area = _text_area

    def _columns(spec):
        n = spec if isinstance(spec, int) else len(spec)
        return [DeltaGenerator("st.col%d" % i) for i in range(n)]

    m.columns = _columns

    def _tabs(labels):
        CALLS.append(("tabs", (list(labels),), {}))
        return [DeltaGenerator("st.tab[%s]" % s) for s in labels]

    m.tabs = _tabs

    def _stop():
        raise StopApp()

    m.stop = _stop

    sys.modules["streamlit"] = m
    return m
