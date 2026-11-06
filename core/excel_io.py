# -*- coding: utf-8 -*-
"""
Excel 读取与列整理。

只做三件事，且都不改数据含义：

1. **读**：`.xlsx/.xlsm/.xls` 用 pandas（底层 xlrd/openpyxl），`.csv` 用 `read_csv`
   并按 utf-8-sig → utf-8 → gbk 依次试编码；支持指定 sheet、支持从内存字节读
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


_SPREADSHEET = (".xlsx", ".xlsm", ".xls")
_SUPPORTED = _SPREADSHEET + (".csv",)

# 文件头识别：浏览器上传时文件名可能被改（比如统一叫 blob），靠字节判断更稳。
_ZIP_MAGIC = b"PK\x03\x04"          # xlsx / xlsm 都是 zip
_OLE_MAGIC = b"\xd0\xcf\x11\xe0"    # xls 是 OLE 复合文档


def _ext(path):
    return os.path.splitext(str(path))[1].lower()


def _kind(source, filename=None):
    """判断文件类型：spreadsheet / csv / unsupported / unknown。

    优先看扩展名（路径或上传文件名），没有扩展名时回退到文件头。
    扩展名明确不支持时直接判 unsupported —— 这样 `报告.pdf` 会得到
    「只支持 .xlsx/.xls/.csv」而不是被当成 CSV 读成一列乱码。
    """
    for candidate in (filename, source if isinstance(source, str) else None):
        ext = _ext(candidate) if candidate else ""
        if not ext:
            continue
        if ext in _SPREADSHEET:
            return "spreadsheet"
        if ext == ".csv":
            return "csv"
        return "unsupported"

    head = b""
    if isinstance(source, bytes):
        head = source[:4096]
    elif isinstance(source, str) and os.path.exists(source):
        try:
            with open(source, "rb") as fh:
                head = fh.read(4096)
        except OSError:
            head = b""

    if not head:
        return "unknown"
    if head[:4] in (_ZIP_MAGIC, _OLE_MAGIC):
        return "spreadsheet"
    return "csv" if _looks_like_text(head) else "unknown"


def _looks_like_text(raw):
    """粗判是不是文本：没有 NUL 字节，且能用 utf-8 或 gbk 解出大半。

    PDF / 图片 / 压缩包都会在头几百字节里出现 NUL 或解码失败。
    """
    if b"\x00" in raw:
        return False
    for enc in ("utf-8-sig", "gbk"):
        try:
            raw.decode(enc)
            return True
        except (UnicodeDecodeError, LookupError):
            continue
    return False


def _load_bytes(source):
    """统一成字节。

    Windows 上 pandas 的 C 解析器打不开含中文的路径
    （`OSError: Initializing from file failed`），所以路径也由我们自己读成字节，
    再交给 pandas 从内存解析。顺带让"上传的文件"和"磁盘上的文件"走同一条路。
    """
    if isinstance(source, bytes):
        return source
    if isinstance(source, str):
        with open(source, "rb") as fh:
            return fh.read()
    data = source.read()
    return data.encode("utf-8") if isinstance(data, str) else data


def _read_csv(raw):
    """按常见编码依次尝试。都失败时抛出说明性的 ExcelError。"""
    last = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return pd.read_csv(io.BytesIO(raw), encoding=enc)
        except UnicodeDecodeError as exc:                  # 换下一个编码
            last = exc
            continue
        except ExcelError:
            raise
        except Exception as exc:                           # noqa: BLE001
            raise ExcelError("无法读取这个 CSV：%s" % exc)
    raise ExcelError("CSV 编码识别失败（试过 utf-8 / gbk）：%s" % last)


def read_frame(source, sheet=None, max_rows=None, filename=None):
    """读取表格。source 可以是路径、文件对象或 bytes；filename 用于判断类型。"""
    kind = _kind(source, filename)
    if kind == "unsupported":
        ext = _ext(filename) if filename else (_ext(source) if isinstance(source, str) else "")
        raise ExcelError("不支持 %s 文件；可用格式：%s" % (ext or "这种", "、".join(_SUPPORTED)))
    if kind == "unknown":
        raise ExcelError("无法识别文件类型，支持：%s" % "、".join(_SUPPORTED))

    try:
        raw = _load_bytes(source)
        if kind == "csv":
            df = _read_csv(raw)
        else:
            df = pd.read_excel(io.BytesIO(raw),
                               sheet_name=sheet if sheet is not None else 0)
    except ExcelError:
        raise
    except ImportError as exc:
        raise ExcelError("缺少读取 Excel 的依赖：%s。执行 pip install xlrd openpyxl 后重试。" % exc)
    except Exception as exc:                              # noqa: BLE001
        raise ExcelError("无法读取这个文件：%s" % exc)

    if isinstance(df, dict):        # 老版本 pandas 传 sheet_name=None 会返回 dict
        df = list(df.values())[0] if df else pd.DataFrame()

    df = normalize(df)
    if max_rows and len(df) > max_rows:
        df = df.head(max_rows)
    return df


def list_sheets(source):
    """列出工作表名。CSV 没有工作表，返回空列表。"""
    if _kind(source) == "csv":
        return []
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
