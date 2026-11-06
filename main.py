# -*- coding: utf-8 -*-
"""
命令行入口。

    python main.py                 # 进入交互模式
    python main.py ask "问题"       # 单次问答
    python main.py serve           # 启动内网 Web 服务
    python main.py doctor          # 启动自检（数据源、Schema、模型、审计）
    python main.py schema          # 打印当前给模型看的表结构提示词
    python main.py traces          # 查看最近的审计记录
    python main.py history         # 查看历史查询记录（--show ID 回看完整报告）
    python main.py excel 文件.xlsx  # 分析上传的 Excel（第二个参数可带问题）
    python main.py eval            # 跑黄金用例集

所有子命令都假设当前目录是项目根目录（或已设置 PYTHONPATH）。
"""

import argparse
import json
import os
import sys

if sys.version_info < (3, 6):
    print("需要 Python 3.6 及以上（当前 %s）" % sys.version.split()[0])
    sys.exit(1)

from core import config                                   # noqa: E402
from core.logging_setup import setup_logging              # noqa: E402


def _banner(info):
    print("")
    print("  县域经济统计分析 Agent")
    print("  数据源 : %s" % info.get("datasource"))
    print("  数据表 : %s" % "、".join(info.get("tables") or []))
    print("  模型   : %s" % info.get("llm"))
    print("  模式   : %s" % ("在线（LLM 参与）" if info.get("llm_online")
                             else "离线（规则兜底，全链路仍可用）"))
    if info.get("schema_blocking"):
        print("  [!] Schema 存在 ERROR 级问题，SQL 生成可能不可靠，请先运行 doctor")
    print("")


def cmd_ask(args):
    from core.pipeline import answer
    r = answer(args.question, session_id=args.session, actor=args.actor)
    print("")
    print(r.report.get("markdown", ""))
    if r.charts:
        paths = _save_charts(r)
        print("图表（SVG，浏览器打开）：")
        for p in paths:
            print("  " + p)
        print("")
    print("trace_id=%s  耗时 %sms  报告来源=%s"
          % (r.trace_id, r.timings.get("total_ms"), (r.report or {}).get("source")))
    if r.degraded:
        print("降级标记：%s" % "、".join(r.degraded))
    return 0 if r.ok else 1


def _save_charts(result):
    from core import config as cfg
    cfg.ensure_dirs()
    paths = []
    for i, c in enumerate(result.charts):
        name = "%s_%d.svg" % (result.trace_id, i)
        path = os.path.join(cfg.CHART_DIR, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(c.get("svg") or "")
        paths.append(path)
    return paths


def cmd_serve(args):
    from web.server import serve
    serve(host=args.host, port=args.port)
    return 0


def cmd_doctor(args):
    from core.pipeline import warmup
    info = warmup()
    _banner(info)

    # 配置来源：.env 到底读没读到、key 有没有生效，是最常见的两个"配了但没生效"
    print("  .env   : %s" % (config.ENV_FILE or "未找到（可用环境变量或复制 .env.example）"))
    if config.ENV_LOADED:
        print("           已加载 %d 项：%s" % (len(config.ENV_LOADED),
                                              "、".join(sorted(config.ENV_LOADED))))
    print("  API Key: %s" % config.mask_key())
    print("  Base   : %s" % config.deepseek_base_url())

    if info.get("llm_online"):
        from core.llm_client import get_default_client
        ok, msg = get_default_client().probe()
        print("  连通性 : %s" % ("✓ " + msg if ok else "✗ " + msg))
    else:
        print("  连通性 : 跳过（离线模式；在 .env 里填 DEEPSEEK_API_KEY 即可上线）")

    print("  Schema 提示词 %d 字符" % info.get("prompt_chars"))
    print("  审计库 %s" % info.get("audit_db"))
    issues = info.get("schema_issues") or []
    if issues:
        print("")
        print("  Schema 一致性检查：")
        for it in issues:
            print("    [%s] %s.%s %s" % (it.get("level"), it.get("table"),
                                         it.get("column") or "-", it.get("message")))
    else:
        print("  Schema 一致性检查：通过（字典与库结构一致）")
    print("")
    return 0 if not info.get("schema_blocking") else 2


def cmd_schema(args):
    from core.datasource import default_source
    from core.schema import snapshot
    snap = snapshot(default_source())
    print(snap["prompt"])
    return 0


def cmd_traces(args):
    from core import audit
    audit.init()
    rows = audit.recent(limit=args.limit)
    if not rows:
        print("暂无审计记录。")
        return 0
    for r in rows:
        print("%s  %-16s %-7s %5s行 %6sms  %s" % (
            r.get("ts"), (r.get("trace_id") or "")[-8:], r.get("intent") or "-",
            r.get("row_count"), int(r.get("latency_ms") or 0),
            (r.get("question") or "")[:40]))
    ok, bad_id, total = audit.verify_chain(limit=args.limit)
    print("")
    if ok:
        print("哈希链校验：通过（校验 %d 条，未发现改动）" % total)
    else:
        print("哈希链校验：第 %s 条记录起对不上（共校验 %d 条），该记录之后的内容不可信" % (bad_id, total))
    return 0 if ok else 3


def cmd_eval(args):
    from tests.evaluator import run_all
    return run_all(verbose=not args.quiet)


def cmd_history(args):
    from core import history
    if args.clear:
        left = history.clear(keep=args.keep)
        print("已清理历史记录，当前剩余 %d 条。" % left)
        return 0

    if args.show:
        item = history.get(args.show)
        if not item:
            print("没有 id=%s 的记录。" % args.show)
            return 1
        print("")
        print("#%s  %s  %s" % (item["id"], item["ts"], item.get("data_source") or "sql"))
        print("问题：%s" % item["question"])
        if item.get("sql"):
            print("SQL ：%s" % item["sql"])
        print("方法：%s   样本：%s 行   耗时：%sms" % (
            item.get("method") or "-", item.get("n_rows"), item.get("latency_ms")))
        if item.get("degraded"):
            print("降级：%s" % item["degraded"])
        print("")
        print(item["report"].get("markdown", ""))
        for i, c in enumerate(item.get("charts") or []):
            path = os.path.join(config.CHART_DIR, "history_%s_%d.svg" % (item["id"], i))
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(c.get("svg") or "")
                print("图表：%s" % path)
            except Exception:                             # noqa: BLE001
                pass
        return 0

    rows = history.recent(limit=args.limit)
    if not rows:
        print("暂无历史记录。执行一次 `python main.py ask \"问题\"` 就有了。")
        return 0
    print("")
    for r in rows:
        flag = "  [有降级]" if r.get("degraded") else ""
        print("#%-3s %s  %s · %s%s" % (
            r["id"], r["ts"], r.get("intent_label") or r.get("intent") or "-",
            r.get("method") or "未建模", flag))
        print("     %s" % (r.get("question") or ""))
        print("     %s 行 · %sms · %s · %s" % (
            r.get("n_rows"), int(r.get("latency_ms") or 0),
            r.get("data_source") or "sql", (r.get("summary") or "")[:60]))
        print("")
    print("用 `python main.py history --show <ID>` 回看完整报告。")
    return 0


def cmd_excel(args):
    from core.pipeline import answer_excel
    path = args.file
    if not os.path.isfile(path):
        print("文件不存在：%s" % path)
        return 1
    try:
        r = answer_excel(path, question=args.question or "", sheet=args.sheet,
                         session_id=args.session, actor="cli")
    except Exception as exc:                              # noqa: BLE001
        print("读取失败：%s" % exc)
        return 1
    print("")
    print(r.report.get("markdown", ""))
    if r.charts:
        print("图表（SVG，浏览器打开）：")
        for p in _save_charts(r):
            print("  " + p)
        print("")
    print("数据来源=%s  样本=%s 行  耗时 %sms  报告来源=%s" % (
        r.data_source, r.n_rows, r.timings.get("total_ms"),
        (r.report or {}).get("source")))
    if r.degraded:
        print("降级标记：%s" % "、".join(r.degraded))
    return 0 if r.ok else 1


def cmd_repl(args):
    from core.pipeline import answer, warmup
    info = warmup()
    _banner(info)
    print("输入问题即可，:q 退出，:traces 看审计，:clear 清屏")
    print("")
    session = args.session
    while True:
        try:
            q = input("问> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            break
        if not q:
            continue
        if q in (":q", ":quit", "exit", "quit"):
            break
        if q == ":clear":
            os.system("cls" if os.name == "nt" else "clear")
            continue
        if q == ":traces":
            cmd_traces(argparse.Namespace(limit=10))
            continue
        if q in (":history", ":h"):
            cmd_history(argparse.Namespace(limit=10, show=None, clear=False, keep=0))
            continue
        r = answer(q, session_id=session, actor="cli")
        print("")
        print(r.report.get("markdown", ""))
        if r.charts:
            for p in _save_charts(r):
                print("图表：%s" % p)
        print("─" * 60)
        print("trace=%s  %sms  %s" % (r.trace_id, r.timings.get("total_ms"),
                                      "、".join(r.degraded) or "无降级"))
        print("")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="县域经济统计分析 Agent（NL2SQL + 自动统计建模）")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("ask", help="单次问答")
    p.add_argument("question")
    p.add_argument("--session", default="cli")
    p.add_argument("--actor", default="cli")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("serve", help="启动 Web 服务")
    p.add_argument("--host", default=os.environ.get("STAT_AGENT_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("STAT_AGENT_PORT", "8000")))
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("doctor", help="启动自检")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("schema", help="打印表结构提示词")
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("traces", help="查看审计记录")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_traces)

    p = sub.add_parser("eval", help="跑黄金用例集")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("history", help="查看历史查询记录")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--show", type=int, default=None, help="按 ID 回看完整报告")
    p.add_argument("--clear", action="store_true", help="清空历史")
    p.add_argument("--keep", type=int, default=0, help="清理时保留最近 N 条")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("excel", help="分析上传的 Excel 文件")
    p.add_argument("file")
    p.add_argument("question", nargs="?", default="")
    p.add_argument("--sheet", default=None, help="工作表名（默认第一个）")
    p.add_argument("--session", default="cli")
    p.set_defaults(func=cmd_excel)

    args = parser.parse_args()
    if not getattr(args, "func", None):
        args = argparse.Namespace(func=cmd_repl, session="cli")

    setup_logging(quiet=os.environ.get("STAT_AGENT_QUIET") == "1")
    config.ensure_dirs()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
