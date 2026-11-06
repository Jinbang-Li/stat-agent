# -*- coding: utf-8 -*-
"""上传通道检查：拖拽区、CSV 读取、中文文件名、multipart 上传。

覆盖的是"文件选择器看不到桌面文件"那类问题的两个真因：

1. `accept` 只写了 .xlsx/.xls，桌面上放的 .csv 在系统文件对话框里根本不可选；
2. 读取层只认 Excel，就算选进来了也会被拒；
3. 附带一个 Windows 上的坑：pandas 的 C 解析器打不开中文路径，
   必须自己读成字节再交给 pandas（`Initializing from file failed`）。

用法：
    python tests/check_upload.py            # 另起一个临时服务做上传用例
    STAT_AGENT_BASE=http://127.0.0.1:8010 python tests/check_upload.py   # 打已有服务
"""

import http.client
import io
import json
import os
import random
import socket
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pandas as pd

from core import config
from core.excel_io import ExcelError, _kind, list_sheets, read_frame

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok), detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                        ("  · " + str(detail)) if detail else ""))


def _desktop_csv():
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    path = os.path.join(home, "Desktop", "gdp_统计周期数据.csv")
    return path if os.path.exists(path) else ""


def _multipart(fields):
    boundary = "----statagent%d" % random.randint(10 ** 8, 10 ** 9)
    out = io.BytesIO()
    for name, value in fields:
        out.write(("--%s\r\n" % boundary).encode("utf-8"))
        if name == "file":
            out.write(('Content-Disposition: form-data; name="file"; filename="%s"\r\n'
                       % value[0]).encode("utf-8"))
            out.write(b"Content-Type: application/octet-stream\r\n\r\n")
            out.write(value[1])
            out.write(b"\r\n")
        else:
            out.write(('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode("utf-8"))
            out.write(str(value).encode("utf-8"))
            out.write(b"\r\n")
    out.write(("--%s--\r\n" % boundary).encode("utf-8"))
    return boundary, out.getvalue()


def _post(host, port, path, boundary, body, timeout=180):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    conn.request("POST", path, body=body, headers={
        "Content-Type": "multipart/form-data; boundary=%s" % boundary,
        "Content-Length": str(len(body)),
    })
    resp = conn.getresponse()
    data = json.loads(resp.read().decode("utf-8"))
    conn.close()
    return resp.status, data


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    print("=" * 70)
    print("上传通道检查")
    print("=" * 70)

    # ---------- 1. 页面与脚本 ----------
    # 直接读磁盘上的静态文件：同目录起服务时也走同一份资源
    html = io.open(os.path.join(ROOT, "web", "static", "index.html"),
                   encoding="utf-8").read()
    js = io.open(os.path.join(ROOT, "web", "static", "app.js"),
                 encoding="utf-8").read()
    css = io.open(os.path.join(ROOT, "web", "static", "app.css"),
                  encoding="utf-8").read()

    check("拖拽区存在", 'id="dropzone"' in html)
    check("提示语正确", "拖拽 Excel 文件到这里，或点击选择" in html)
    check("accept 含 csv", '.csv' in html and 'accept=".xlsx,.xlsm,.xls,.csv"' in html)
    check("文件信息条存在", 'id="filechip"' in html and 'id="fcsize"' in html)
    check("移除按钮存在", 'id="fcx"' in html)
    check("拖拽高亮样式", ".dropzone.dragover" in css.replace("\n", "").replace(" ", "")
          or ".dragover" in css)
    check("掉落处理绑定 dataTransfer", "dataTransfer" in js and "preventDefault" in js)
    check("选完文件不立即上传", "setPendingFile" in js and "function submitFile" in js)
    check("确认后按钮文案切换", "分析这个文件" in js)
    check("前端限制 20MB", "20 * 1024 * 1024" in js)

    # ---------- 2. 读取层：格式识别 ----------
    check("_kind 认 csv 扩展名", _kind("a.csv") == "csv")
    check("_kind 认 xlsx 扩展名", _kind("a.xlsx") == "spreadsheet")
    check("_kind 无扩展名回退文件头",
          _kind(b"PK\x03\x04xxxx") == "spreadsheet" and _kind("周期,值\n1,2\n".encode("utf-8")) == "csv")

    # ---------- 3. 读取层：CSV（含中文路径 / 中文文件名 / 编码） ----------
    tmp_dir = os.path.join(config.OUTPUT_DIR, "_upload_probe")
    if not os.path.isdir(tmp_dir):
        os.makedirs(tmp_dir)

    cn_path = os.path.join(tmp_dir, "居民收入 明细.csv")
    with io.open(cn_path, "w", encoding="gbk", newline="") as fh:
        fh.write(u"城市,收入,类型\n武汉,32000,城市\n襄阳,24000,城市\n宜昌,26000,城市\n")

    try:
        df = read_frame(cn_path)
        check("中文路径 + GBK 编码可读", len(df) == 3 and list(df.columns) == ["城市", "收入", "类型"],
              list(df.columns))
        check("数值列被识别", str(df["收入"].dtype).startswith("float") or str(df["收入"].dtype).startswith("int"),
              df["收入"].dtype)
        check("分类列保持文本", str(df["城市"].dtype) == "object", df["城市"].dtype)
    except ExcelError as exc:
        check("中文路径 + GBK 编码可读", False, exc)

    try:
        with io.open(cn_path, "rb") as fh:
            raw = fh.read()
        df_b = read_frame(raw, filename="居民收入 明细.csv")
        check("字节 + 中文文件名可读", len(df_b) == 3)
        df_n = read_frame(raw, filename="上传文件")
        check("字节 + 无扩展名（文件头识别）", len(df_n) == 3)
    except ExcelError as exc:
        check("字节 + 中文文件名可读", False, exc)

    # ---------- 4. 读取层：xlsx 未受影响 ----------
    sample = os.path.join(config.DATA_DIR, "raw", "county_sample.xlsx") \
        if hasattr(config, "DATA_DIR") else ""
    if not sample or not os.path.exists(sample):
        sample = os.path.join(ROOT, "data", "raw", "county_sample.xlsx")
    if os.path.exists(sample):
        try:
            df_x = read_frame(sample)
            check("xlsx 仍可读", len(df_x) == 48, df_x.shape)
            check("xlsx 工作表列表", isinstance(list_sheets(sample), list))
        except ExcelError as exc:
            check("xlsx 仍可读", False, exc)
    else:
        check("xlsx 仍可读", False, "缺少 data/raw/county_sample.xlsx")

    # ---------- 5. 不支持的格式要给出可用清单 ----------
    try:
        read_frame(os.path.join(tmp_dir, "x.pdf"), filename="x.pdf")
        check("拒绝不支持的格式", False, "没有抛异常")
    except ExcelError as exc:
        check("拒绝不支持的格式", ".csv" in str(exc), exc)

    # ---------- 6. 真实桌面文件（存在才测） ----------
    desktop = _desktop_csv()
    if desktop:
        try:
            df_d = read_frame(desktop)
            check("桌面 CSV 可读（含中文路径）", len(df_d) > 0, "%d 行 × %d 列" % df_d.shape)
            check("桌面 CSV 文件头为 csv", _kind(desktop) == "csv")
        except ExcelError as exc:
            check("桌面 CSV 可读（含中文路径）", False, exc)
    else:
        check("桌面 CSV 可读（含中文路径）", True, "桌面没有该文件，跳过")

    # ---------- 7. HTTP 上传（真 multipart，中文文件名） ----------
    base = os.environ.get("STAT_AGENT_BASE", "")
    server = None
    if base:
        host, port = base.split("//", 1)[1].split(":")
        port = int(port.split("/")[0])
    else:
        port = _free_port()
        host = "127.0.0.1"
        from core import audit
        from core.pipeline import warmup
        from web import server as web_server
        warmup()
        audit.init()
        server = web_server.ThreadingHTTPServer(("127.0.0.1", port), web_server.Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        import time
        time.sleep(1.0)

    try:
        payload = io.open(cn_path, "rb").read()
        boundary, body = _multipart(
            [("file", ("居民收入 明细.csv", payload)),
             ("question", "看看收入的分布"),
             ("session_id", "test-upload")])
        status, data = _post(host, port, "/api/excel", boundary, body)
        check("HTTP 上传返回 200", status == 200, status)
        check("上传后能出报告", bool((data.get("report") or {}).get("markdown")),
              (data.get("report") or {}).get("source"))
        check("来源标记为 csv", str(data.get("data_source") or "").startswith("csv:"),
              data.get("data_source"))
        check("Excel 通道不产生 SQL", not data.get("sql"), data.get("sql_source"))
        check("样本行数正确", data.get("n_rows") == 3, data.get("n_rows"))

        boundary, body = _multipart(
            [("file", ("bad.pdf", b"%PDF-1.4 not a table")), ("question", ""),
             ("session_id", "test-upload")])
        status, data = _post(host, port, "/api/excel", boundary, body)
        check("PDF 被拒且提示可用格式", status == 400 and ".csv" in (data.get("error") or ""),
              data.get("error"))
    except Exception as exc:                              # noqa: BLE001
        check("HTTP 上传返回 200", False, "%s: %s" % (type(exc).__name__, exc))
    finally:
        if server is not None:
            server.shutdown()

    # ---------- 汇总 ----------
    failed = [c for c in _checks if not c[1]]
    print("-" * 70)
    print("共 %d 项，通过 %d 项，失败 %d 项" % (len(_checks), len(_checks) - len(failed), len(failed)))
    for name, _, detail in failed:
        print("  FAIL %s  %s" % (name, detail))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
