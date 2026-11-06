# -*- coding: utf-8 -*-
"""
.env 读取。

为什么自己写而不是用 python-dotenv：
1. 项目对外的承诺是"clone 下来就能跑"，多一个依赖就多一道安装门槛；
2. .env 的格式本身极简单，一百行足够覆盖真实用到的写法；
3. 自己写才能把"优先级"讲清楚——**真实环境变量永远高于 .env**，
   这样部署时（Docker/K8s 注入变量）不会被仓库里的 .env 偷偷改掉。

支持写法：
    KEY=value
    KEY="value with spaces"
    KEY='single quoted'
    export KEY=value
    # 整行注释
    KEY=value   # 行尾注释（前面必须有空格）

不支持的：多行值、变量插值（${OTHER}）。用到再说，不做没人用的功能。
"""

import os
import re

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")

# 本进程实际从 .env 里读到了哪些键（不存值），doctor 里展示用
LOADED = {}


def parse(text):
    """把 .env 文本解析成 dict。空值保留为空串（表示"显式关掉"）。"""
    out = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(raw)
        if not m:
            continue
        key, val = m.group(1), m.group(2)

        # 去行尾注释：只有引号外、且前面是空白的 # 才算注释
        if val and val[0] in ("'", '"'):
            quote = val[0]
            end = val.find(quote, 1)
            if end > 0:
                val = val[1:end]
            else:
                val = val[1:]
        else:
            cut = val.find(" #")
            if cut < 0:
                cut = val.find("\t#")
            if cut >= 0:
                val = val[:cut]
            val = val.strip()

        out[key] = val
    return out


def find_file(explicit=None):
    """按优先级找 .env：显式指定 > $STAT_AGENT_ENV_FILE > 项目根 > 当前目录。

    $STAT_AGENT_ENV_FILE 设为 off / none / - 表示**不要读任何 .env**。
    部署环境靠外部注入配置时，仓库里那份 .env 不该生效；测试也需要这个开关。
    """
    env_path = os.environ.get("STAT_AGENT_ENV_FILE")
    if env_path and env_path.strip().lower() in ("off", "none", "-"):
        return None
    candidates = []
    if explicit:
        candidates.append(explicit)
    if env_path:
        candidates.append(env_path)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.path.join(root, ".env"))
    candidates.append(os.path.join(os.getcwd(), ".env"))
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def load_dotenv(path=None, override=False):
    """读取 .env 并写进 os.environ。

    override=False（默认）时，环境里已有的变量不覆盖——部署环境注入的
    配置优先级最高，.env 只是本地开发的便利。
    返回 {键: 来源文件}，失败的键不会出现。
    """
    global LOADED
    target = find_file(path)
    if not target:
        return {}
    try:
        with open(target, "r", encoding="utf-8-sig") as f:
            data = parse(f.read())
    except (IOError, OSError):
        return {}

    applied = {}
    for key, val in data.items():
        if key in os.environ and not override:
            continue
        if val != "":
            os.environ[key] = val
            applied[key] = target
        elif key not in os.environ:
            # 空值也写进去：调用方据此判断"显式留空 = 走离线"
            os.environ[key] = ""
            applied[key] = target

    LOADED.update(applied)
    return applied
