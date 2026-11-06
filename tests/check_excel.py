# -*- coding: utf-8 -*-
"""Excel 导入：读文件、整理列、走同一条分析链路、从 Web 上传。

覆盖四类问题：
  1. 表头脏（空列名、重名、数字表头）——必须整成可用列名；
  2. 数字存成文本（"1,234"、带空格）——90% 规则内才转数值，否则保留原样；
  3. 分析结果必须和数据库通道一致（同一套路由/计算/报告）；
  4. 上传接口是 multipart，最容易在编码上出错，所以真起一个 HTTP 服务打一遍。
"""
import io
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import pandas as pd                                   # noqa: E402

from core import excel_io, history, pipeline          # noqa: E402
from core.llm_client import LLMClient                 # noqa: E402

PASS, FAIL = [], []
TMP = os.path.join(ROOT, "output", "_excel_test")


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s %s" % ("ok  " if ok else "FAIL", name, detail))


def make_xlsx(df, name="t.xlsx"):
    if not os.path.isdir(TMP):
        os.makedirs(TMP)
    path = os.path.join(TMP, name)
    df.to_excel(path, index=False)
    return path


def main():
    llm = LLMClient(provider="offline", quiet=True)

    print("1) 读文件：列名整理与类型识别")
    raw = pd.DataFrame([
        ["甲县", "1,200", "12.5%", "农业"],
        ["乙县", "980", "9.0%", "工业"],
        ["丙县", "1540", "18.2%", "农业"],
        ["丁县", "760", "7.1%", "服务"],
    ], columns=["名称", "  产量  ", "占比", "  产量  "])
    path = make_xlsx(raw)
    df = excel_io.read_frame(path)
    check("行数正确", len(df) == 4, len(df))
    check("列名去空格", "产量" in df.columns and "  产量  " not in df.columns, list(df.columns))
    check("重名列自动去重", len(df.columns) == 4, list(df.columns))
    check("千分位文本转数值", str(df["产量"].dtype).startswith("int") or
          str(df["产量"].dtype).startswith("float"), df["产量"].dtype)
    check("带 % 的列也识别为数值", str(df["占比"].dtype).startswith("float"), df["占比"].dtype)
    check("分类列保持文本", df["名称"].dtype == object, df["名称"].dtype)

    print("2) 空列名 / 全空列 / 数字表头")
    raw2 = pd.DataFrame([[1, 2, None], [3, 4, None], [5, 6, None]])
    raw2.columns = ["年份", None, "空列"]
    path2 = make_xlsx(raw2, "t2.xlsx")
    df2 = excel_io.read_frame(path2)
    check("空列名补成 列N", any(c.startswith("列") for c in df2.columns), list(df2.columns))
    check("全空列被丢掉", "空列" not in df2.columns, list(df2.columns))

    print("3) 90% 规则：认得出的缺失标记才允许被吃掉")
    mixed = pd.DataFrame({"值": [1, 2, 3, 4, 5, 6, 7, 8, 9, "缺失"]})
    path3 = make_xlsx(mixed, "t3.xlsx")
    df3 = excel_io.read_frame(path3)
    check("「缺失」按缺测落成 NaN", str(df3["值"].dtype).startswith("float") and
          df3["值"].notna().sum() == 9, "%s / %s" % (df3["值"].dtype,
                                                     df3["值"].notna().sum()))

    words = pd.DataFrame({"值": [1, 2, 3, 4, 5, 6, 7, 8, 9, "待定"]})
    path3b = make_xlsx(words, "t3b.xlsx")
    df3b = excel_io.read_frame(path3b)
    check("不认识的文本不让整列变 NaN", df3b["值"].notna().sum() == 10 and
          df3b["值"].dtype == object, "%s / %s" % (df3b["值"].dtype,
                                                   df3b["值"].notna().sum()))

    print("4) 走同一条分析链路")
    conn = None
    try:
        import sqlite3
        conn = sqlite3.connect(os.path.join(ROOT, "data", "stat_agent.db"))
        big = pd.read_sql_query(
            "SELECT county_name AS 县名, year AS 年份, gdp AS 地区生产总值,"
            " income AS 居民收入, agri_output AS 农业总产值 FROM county_economy"
            " WHERE year = 2023", conn)
    finally:
        if conn is not None:
            conn.close()
    path4 = make_xlsx(big, "county.xlsx")
    df4 = excel_io.read_frame(path4)
    check("样例表 48 行", len(df4) == 48, len(df4))

    history.clear()
    r = pipeline.answer_frame(df4, question="居民收入和农业总产值是否显著相关",
                              llm=llm, source_label="excel:county.xlsx")
    check("识别为相关分析", (r.plan or {}).get("method") == "correlation",
          (r.plan or {}).get("method"))
    check("样本量取自表格", r.n_rows == 48, r.n_rows)
    md = (r.report or {}).get("markdown") or ""
    check("报告标注了数据来源", "数据来源：用户上传的表格文件" in md, md[:60].replace("\n", " "))
    check("Excel 通道不生成 SQL", not r.sql, repr(r.sql))
    check("r= 出现在报告里", "r=" in md, "")
    check("历史记录了 excel 来源",
          (history.recent(1)[0]["data_source"] or "").startswith("excel"),
          history.recent(1)[0]["data_source"])

    print("5) 不提问时给出默认描述统计")
    r2 = pipeline.answer_frame(df4, question="", llm=llm, source_label="excel:county.xlsx")
    check("默认走描述统计", (r2.plan or {}).get("method") == "describe",
          (r2.plan or {}).get("method"))
    check("默认问题可读", "分布" in (r2.question or ""), r2.question)

    print("6) 上传接口（真起服务，multipart 打一遍）")
    from web.server import Handler
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        body, boundary = _multipart(
            [("question", "居民收入和农业总产值是否显著相关"),
             ("session_id", "excel-test")],
            ("file", "county.xlsx", open(path4, "rb").read()))
        req = urllib.request.Request(
            base + "/api/excel", data=body,
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
        with urllib.request.urlopen(req, timeout=120) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        check("上传返回 200", True)
        check("来源标记为 excel", (res.get("data_source") or "").startswith("excel"),
              res.get("data_source"))
        check("分析出结果", (res.get("plan") or {}).get("method") == "correlation",
              (res.get("plan") or {}).get("method"))

        with urllib.request.urlopen(base + "/api/history?limit=5", timeout=30) as resp:
            hist = json.loads(resp.read().decode("utf-8"))
        check("历史接口有这条", any((i["data_source"] or "").startswith("excel")
                                    for i in hist["items"]), len(hist["items"]))
        item_id = hist["items"][0]["id"]
        with urllib.request.urlopen(base + "/api/history/%s" % item_id, timeout=30) as resp:
            full = json.loads(resp.read().decode("utf-8"))
        check("回看接口返回报告", bool((full.get("report") or {}).get("markdown")), item_id)

        bad = urllib.request.Request(base + "/api/excel", data=b"not-multipart",
                                     headers={"Content-Type": "text/plain"})
        try:
            urllib.request.urlopen(bad, timeout=30)
            check("非 multipart 被拒", False)
        except urllib.error.HTTPError as e:
            check("非 multipart 被拒", e.code == 400, e.code)
    finally:
        srv.shutdown()
        srv.server_close()

    print("")
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：%s" % "、".join(FAIL))
    return 0 if not FAIL else 1


def _multipart(fields, file_field):
    boundary = "----statagent%s" % int(time.time() * 1000)
    out = b""
    for name, val in fields:
        out += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n'
                % (boundary, name)).encode("utf-8")
        out += str(val).encode("utf-8") + b"\r\n"
    name, filename, data = file_field
    out += ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
            % (boundary, name, filename)).encode("utf-8")
    out += data + b"\r\n"
    out += ("--%s--\r\n" % boundary).encode("utf-8")
    return out, boundary


if __name__ == "__main__":
    sys.exit(main())
