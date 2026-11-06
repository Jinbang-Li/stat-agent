# -*- coding: utf-8 -*-
"""
Excel 读取与列整理。

只做三件事，且都不改数据含义：

1. **读**：`.xlsx/.xls` 用 pandas（底层 xlrd），支持指定 sheet、支持从内存字节读
   （Web 上传走这条路，不落盘）。
2. **整列名**：去掉首尾空格、把纯数字表头转成字符串、重名加后缀。
   Excel 里常见"表头占两行"或"最后一个空列"，这些会让后面的路由取到空列名。
3. **认类型**：整列 90% 以上能解析成数字的 object 列转成数值——Excel 里数字经常
   以文本形式存着（左对齐、前面带空格、千分位逗号）。**不满足 90% 就不转**，
   否则"县名"里混一个数字会被转成 NaN 然后整列丢失。

刻意不做的事：不填补缺失、不删异常行、不改列的顺序。清洗的边界在
`core/data_loader.py`，这里只负责"把表格读成一张干净的表"。
"""

import io
import os
import re

import pandas as pd


class ExcelError(Exception):
    """读文件失败。消息面向用户，不带堆栈。"""


_SUPPORTED = (".xlsx", ".xlsm", ".xls")


def _ext(path):
    return os.path.splitext(str(path))[1].lower()


def read_frame(source, sheet=None, max_rows=None):
    """读取表格。source 可以是路径、文件对象或 bytes。"""
    ext = _ext(source) if isinstance(source, str) else ""
    if ext and ext not in _SUPPORTED:
        raise ExcelError("只支持 %s 文件，收到的是 %s" % ("、".join(_SUPPORTED), ext))

    try:
        if isinstance(source, bytes):
            buf = io.BytesIO(source)
            df = pd.read_excel(buf, sheet_name=sheet if sheet is not None else 0)
        else:
            df = pd.read_excel(source, sheet_name=sheet if sheet is not None else 0)
    except ImportError as exc:
        raise ExcelError("缺少读取 Excel 的依赖：%s。执行 pip install xlrd 后重试。" % exc)
    except Exception as exc:                              # noqa: BLE001
        raise ExcelError("无法读取这个文件：%s" % exc)

    if isinstance(df, dict):        # 老版本 pandas 传 sheet_name=None 会返回 dict
        df = list(df.values())[0] if df else pd.DataFrame()

    df = normalize(df)
    if max_rows and len(df) > max_rows:
        df = df.head(max_rows)
    return df


def list_sheets(source):
    """列出工作表名。读不出来时返回空列表，由调用方决定怎么提示。"""
    try:
        xls = pd.ExcelFile(source)
        try:
            return list(xls.sheet_names)
        finally:
            close = getattr(xls, "close", None)
            if callable(close):
                close()
    except Exception:                                     # noqa: BLE001
        return []


def normalize(df):
    """整理列名与类型。空表返回空表，不抛异常。"""
    if df is None or not len(df.columns):
        return pd.DataFrame()

    df = df.copy()
    df.columns = _clean_columns(df.columns)

    # 全空列、全空行：Excel 导出的文件尾部经常带一片空白
    df = df.dropna(axis=1, how="all")
    df = df.dropna(axis=0, how="all")
    if not len(df.columns):
        return pd.DataFrame()

    for col in list(df.columns):
        df[col] = _coerce(df[col])
    return df.reset_index(drop=True)


def _clean_columns(columns):
    seen = {}
    out = []
    for i, c in enumerate(columns):
        name = "" if c is None else str(c)
        name = name.replace("\u3000", " ").strip()
        if not name or name.lower().startswith("unnamed:"):
            name = "列%d" % (i + 1)
        name = re.sub(r"\s+", " ", name)
        if name in seen:
            seen[name] += 1
            name = "%s_%d" % (name, seen[name])
        else:
            seen[name] = 0
        out.append(name)
    return out


_NUM_CLEAN = re.compile(r"[,\s¥$%]")

# 这些词在表格里通常表示"这一格没值"，转数值时可以落成 NaN。
# 只有认得出的缺失标记才允许被吃掉——"待定""约 3 万"这类文本是真信息，
# 把它们变成 NaN 等于静默删数据，宁可整列保持文本、让分析层当分类变量处理。
_MISSING_TOKENS = {"", "na", "n/a", "null", "none", "nan", "-", "—", "/",
                   "缺失", "无", "未填", "不详", "空", "未知"}


def _is_missing_token(v):
    return _is_blank(v) or str(v).strip().lower() in _MISSING_TOKENS


def _coerce(series):
    """只在"绝大多数值确实是数字"时转数值，避免把分类列毁掉。"""
    if series.dtype != object:
        return series

    vals = [v for v in series.tolist() if v is not None and not _is_blank(v)]
    if not vals:
        return series

    ok = 0
    for v in vals:
        if _is_number(v):
            ok += 1
    if ok < max(1, int(len(vals) * 0.9)):
        return series

    # 剩下那不到一成的非数字，必须都是"缺失标记"才敢转
    for v in vals:
        if not _is_number(v) and not _is_missing_token(v):
            return series

    def conv(v):
        if v is None or _is_blank(v) or _is_missing_token(v):
            return None
        try:
            return float(_NUM_CLEAN.sub("", str(v)))
        except ValueError:
            return None

    converted = series.map(conv)
    # 转换后如果整列都空了，说明刚才的"像数字"是误判，退回原样
    if converted.notna().sum() == 0:
        return series
    return converted


def _is_blank(v):
    return isinstance(v, str) and not v.strip()


def _is_number(v):
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    try:
        float(_NUM_CLEAN.sub("", str(v)))
        return True
    except (TypeError, ValueError):
        return False


def describe_columns(df):
    """给前端/CLI 的一行摘要：哪些是数值列、哪些是分类列。"""
    numeric, categorical, other = [], [], []
    for col in df.columns:
        if df[col].dtype.kind in "if":
            numeric.append(col)
        elif df[col].dtype.kind in "biu":
            categorical.append(col)
        elif df[col].dtype == object:
            categorical.append(col)
        else:
            other.append(col)
    return {"numeric": numeric, "categorical": categorical, "other": other,
            "n_rows": int(len(df)), "n_cols": int(len(df.columns))}
