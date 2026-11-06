# -*- coding: utf-8 -*-
"""
LLM 配置自检：.env 解析、在线/离线判定、key 打码、连不上时的降级。

    python tests/check_llm_config.py

不联网也能跑：key 是假的，base_url 指向一个必然拒绝连接的端口，
要验证的正是"配错了会怎样"——它必须降级，而不是崩掉。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# 先清掉真实环境里的 key，避免"本机刚好配了 key"让测试结果失真
for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
          "STAT_AGENT_PROVIDER", "STAT_AGENT_USE_OLLAMA", "STAT_AGENT_LLM_MAX_RETRIES",
          "STAT_AGENT_LLM_TIMEOUT"):
    os.environ.pop(k, None)

# 项目根目录的 .env 会在 import config 时被读进 os.environ，把上面的清理覆盖掉。
# 关掉 .env 加载，让这组用例只依赖自己设置的环境变量。
os.environ["STAT_AGENT_ENV_FILE"] = "off"

from core import env as env_mod          # noqa: E402
from core import config                  # noqa: E402
from core.llm_client import LLMClient, LLMError   # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  → " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main():
    print("1) .env 解析")
    text = """
# 注释行
DEEPSEEK_API_KEY=sk-abcdef123456
export DEEPSEEK_BASE_URL="https://api.example.com/v1"
  SPACED = value with spaces
QUOTED='single quoted value'
WITH_COMMENT=abc   # 行尾注释
EMPTY=
HASH_INSIDE=abc#def
"""
    d = env_mod.parse(text)
    check("普通键值", d.get("DEEPSEEK_API_KEY") == "sk-abcdef123456", d.get("DEEPSEEK_API_KEY"))
    check("export + 双引号", d.get("DEEPSEEK_BASE_URL") == "https://api.example.com/v1", d.get("DEEPSEEK_BASE_URL"))
    check("单引号", d.get("QUOTED") == "single quoted value", d.get("QUOTED"))
    check("行尾注释被去掉", d.get("WITH_COMMENT") == "abc", repr(d.get("WITH_COMMENT")))
    check("空值保留为空串", d.get("EMPTY") == "", repr(d.get("EMPTY")))
    check("值里的 # 不被当注释", d.get("HASH_INSIDE") == "abc#def", d.get("HASH_INSIDE"))
    check("注释行被跳过", "# 注释行" not in d and len(d) == 7, len(d))

    print("2) key 打码")
    check("打码不泄露完整 key", config.mask_key("sk-abcdef1234567890") == "sk-abc…7890（19 位）",
          config.mask_key("sk-abcdef1234567890"))
    check("未配置时显示中文提示", config.mask_key("") == "（未配置）", config.mask_key(""))

    print("3) 没有 key → 离线")
    client = LLMClient(quiet=True)
    check("provider=offline", client.provider == "offline", client.provider)
    check("online=False", client.online is False)
    check("has_key=False", client.has_key is False)
    check("offline 下调模型抛 LLMError", _raises(lambda: client.chat("s", "u"), LLMError))
    check("offline 下 try_chat 返回 None", client.try_chat("s", "u") is None)
    check("probe 给出可操作的提示", client.probe()[0] is False and "DEEPSEEK_API_KEY" in client.probe()[1])

    print("4) 有 key → 在线（不需要真的联网）")
    os.environ["DEEPSEEK_API_KEY"] = "sk-test-0123456789abcdef"
    os.environ["DEEPSEEK_BASE_URL"] = "http://127.0.0.1:9"
    os.environ["DEEPSEEK_MODEL"] = "deepseek-chat"
    online = LLMClient(quiet=True)
    check("provider=deepseek", online.provider == "deepseek", online.provider)
    check("online=True 且 has_key=True", online.online and online.has_key)
    check("base_url 取了环境变量", online.base_url == "http://127.0.0.1:9", online.base_url)
    check("describe 里有打码 key", "sk-tes…cdef" in online.describe(), online.describe())

    print("5) 连不上时降级而不是崩")
    os.environ["STAT_AGENT_LLM_MAX_RETRIES"] = "0"
    from core import config as cfg2
    cfg2.LLM_MAX_RETRIES = 0        # 模块级常量已在 import 时求值，这里同步改一次
    ok, msg = online.probe()
    check("probe 返回失败但不抛异常", ok is False and bool(msg), msg)
    check("失败信息是人话", any(w in msg for w in ("调用失败", "HTTP", "连接", "refused", "Error")), msg)

    from core.pipeline import answer
    r = answer("2023年各县农业总产值的平均值是多少", llm=online, persist=False)
    check("整条链路仍然跑完（没把异常抛给用户）", bool(r.report.get("markdown")), r.error)
    check("SQL 降级到模板", r.sql_source == "template", r.sql_source)
    check("降级标记里有 llm 相关项",
          any(d.startswith(("sql:", "report:")) for d in r.degraded), r.degraded)
    check("离线也有结论段", "结论" in r.report.get("markdown", ""))

    print("")
    if FAILS:
        print("未通过 %d 项：%s" % (len(FAILS), "、".join(FAILS)))
        return 1
    print("全部通过。")
    return 0


def _raises(fn, exc_type):
    try:
        fn()
        return False
    except exc_type:
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
