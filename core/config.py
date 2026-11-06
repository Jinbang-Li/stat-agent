# -*- coding: utf-8 -*-
"""
配置层。

设计原则：
1. 所有"魔法数字"集中在这里，README 里能逐条引用、逐条复现。
2. **可外部覆盖**：任何一项都能用环境变量覆盖（前缀 STAT_AGENT_），
   不需要改代码就能换模型、换阈值、换数据源。
3. 模块级常量保持存在，老代码 `from . import config; config.ALPHA` 继续可用；
   新代码用 `config.settings`，支持运行时热改（测试里会用到）。
"""

import json
import os

from . import env as env_mod

# .env 必须在这里加载：下面的模块级常量马上就用到环境变量。
# 优先级是「真实环境变量 > .env」，部署时注入的配置不会被仓库里的文件覆盖。
ENV_FILE = env_mod.find_file()
ENV_LOADED = env_mod.load_dotenv()

# ---------------------------------------------------------------- 路径
CORE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(CORE_DIR)
DATA_DIR = os.path.join(ROOT, "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")
DB_PATH = os.path.join(DATA_DIR, "stat_agent.db")
AUDIT_DB_PATH = os.path.join(DATA_DIR, "audit.db")
SCHEMA_META_PATH = os.path.join(DATA_DIR, "schema_meta.json")
PROMPT_DIR = os.path.join(ROOT, "prompt")
OUTPUT_DIR = os.path.join(ROOT, "output")
CHART_DIR = os.path.join(OUTPUT_DIR, "charts")
REPORT_DIR = os.path.join(OUTPUT_DIR, "reports")
LOG_DIR = os.path.join(OUTPUT_DIR, "logs")
CASE_DIR = os.path.join(ROOT, "tests")


def _env(key, default):
    return os.environ.get("STAT_AGENT_" + key, default)


def _env_int(key, default):
    try:
        return int(os.environ.get("STAT_AGENT_" + key, default))
    except (TypeError, ValueError):
        return default


def _env_float(key, default):
    try:
        return float(os.environ.get("STAT_AGENT_" + key, default))
    except (TypeError, ValueError):
        return default


def _env_bool(key, default):
    v = os.environ.get("STAT_AGENT_" + key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- 模型
# provider 优先级：显式参数 > 环境变量 STAT_AGENT_PROVIDER > 自动判断
# 自动判断的规则只有一条：**有 DEEPSEEK_API_KEY 就上线，没有就离线**。
# 本地 Ollama 需要显式开启（STAT_AGENT_USE_OLLAMA=1），因为大部分机器上
# 探测一个不存在的端口既慢又没意义。
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEFAULT_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "deepseek-r1:7b")


def deepseek_key():
    """每次调用都重新读，这样改环境变量（测试、热切换）不用重启进程。"""
    return os.environ.get("DEEPSEEK_API_KEY", "").strip()


def deepseek_base_url():
    return (os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).strip().rstrip("/")


def mask_key(key=None):
    """日志、doctor、界面里只显示指纹，绝不打印完整 key。"""
    key = deepseek_key() if key is None else (key or "")
    if not key:
        return "（未配置）"
    if len(key) <= 10:
        return "***"
    return "%s…%s（%d 位）" % (key[:6], key[-4:], len(key))


# 温度默认 0.1：留给措辞一点余地，同时避免同一问题两次输出差异过大。
# 但要说清楚——温度不是幻觉控制手段，它只影响采样的随机性。真正的控制靠
# schema 约束、SQL 白名单、数值对账（见 README《幻觉控制方案》）。
# 需要严格可复现（跑回归、录演示）时用 STAT_AGENT_TEMPERATURE=0 把随机性关掉。
TEMPERATURE = _env_float("TEMPERATURE", 0.1)
LLM_TIMEOUT = _env_int("LLM_TIMEOUT", 90)          # 秒
LLM_MAX_RETRIES = _env_int("LLM_MAX_RETRIES", 2)
LLM_MAX_TOKENS = _env_int("LLM_MAX_TOKENS", 2200)

# ---------------------------------------------------------------- 统计阈值
ALPHA = _env_float("ALPHA", 0.05)              # 显著性水平
ALPHA_MARGINAL = _env_float("ALPHA_MARGINAL", 0.10)     # 仅用于提示"边缘"，不作为结论依据
MIN_N_DESCRIBE = 1        # 任何样本都可以出描述统计
MIN_N_INFER = _env_int("MIN_N_INFER", 30)          # 低于此样本量：只描述，不做推断
MIN_N_MODEL = _env_int("MIN_N_MODEL", 50)          # 低于此样本量：不建预测模型
MAX_MISSING_RATE = _env_float("MAX_MISSING_RATE", 0.30)   # 单列缺失率超过此值：该列不参与分析
MAX_CATEGORIES = _env_int("MAX_CATEGORIES", 30)       # 类别数超过此值：不做分组比较
MAX_CORR_PAIRS = _env_int("MAX_CORR_PAIRS", 6)        # 一次最多检验的变量对数量（超出则做多重比较校正）
MIN_GROUP_SIZE = _env_int("MIN_GROUP_SIZE", 5)        # 组内样本少于这个数：该组不进入组间检验
SHAPIRO_MAX_N = 5000      # Shapiro 检验的样本上限（超过就抽样，避免 O(n²)）

# SQL 安全
SQL_MAX_ROWS = _env_int("SQL_MAX_ROWS", 5000)
SQL_TIMEOUT = _env_int("SQL_TIMEOUT", 20)          # 秒
SQL_MAX_LENGTH = _env_int("SQL_MAX_LENGTH", 4000)      # 单条 SQL 字符上限

# ---------------------------------------------------------------- 服务
HOST = _env("HOST", "127.0.0.1")
PORT = _env_int("PORT", 8000)
SESSION_TTL = _env_int("SESSION_TTL", 1800)        # 会话闲置多久过期（秒）
SESSION_MAX_TURNS = _env_int("SESSION_MAX_TURNS", 12)
RATE_LIMIT_PER_MIN = _env_int("RATE_LIMIT_PER_MIN", 30)   # 单会话每分钟提问上限
MONTHLY_TOKEN_BUDGET = _env_int("MONTHLY_TOKEN_BUDGET", 5000000)  # 软预算，超了打警告
MAX_QUESTION_CHARS = _env_int("MAX_QUESTION_CHARS", 500)

# 兜底统计量：报告里最多展示多少行数据样本
SAMPLE_ROWS_IN_REPORT = _env_int("SAMPLE_ROWS_IN_REPORT", 8)

# 开启后，报告里出现的每个数字都必须能在统计结果中找到，否则标记"疑似编造"
STRICT_NUMBER_CHECK = _env_bool("STRICT_NUMBER_CHECK", True)


def ensure_dirs():
    for d in (OUTPUT_DIR, CHART_DIR, REPORT_DIR, LOG_DIR, DATA_DIR):
        if not os.path.isdir(d):
            os.makedirs(d)


# ---------------------------------------------------------------- 数据源
class DataSourceConfig(object):
    """数据源配置。默认 SQLite；mysql/pg 需要额外装驱动。"""

    def __init__(self, kind=None, dsn=None):
        self.kind = (kind or _env("DB_KIND", "sqlite")).strip().lower()
        self.dsn = dsn or _env("DB_DSN", DB_PATH)

    def to_dict(self):
        return {"kind": self.kind, "dsn": _mask_dsn(self.dsn)}


def _mask_dsn(dsn):
    """日志里不打印密码。"""
    if "://" not in dsn:
        return dsn
    try:
        head, rest = dsn.split("://", 1)
        if "@" in rest:
            cred, host = rest.split("@", 1)
            if ":" in cred:
                user = cred.split(":", 1)[0]
                return "%s://%s:***@%s" % (head, user, host)
        return dsn
    except ValueError:
        return "<dsn>"


# ---------------------------------------------------------------- 汇总
class Settings(object):
    """运行时配置视图。测试里可以改属性而不影响全局。"""

    # 路径
    db_path = DB_PATH
    audit_db_path = AUDIT_DB_PATH
    chart_dir = CHART_DIR
    report_dir = REPORT_DIR
    log_dir = LOG_DIR

    # 统计
    alpha = ALPHA
    min_n_infer = MIN_N_INFER
    min_n_model = MIN_N_MODEL
    max_missing_rate = MAX_MISSING_RATE
    min_group_size = MIN_GROUP_SIZE

    # 服务
    session_ttl = SESSION_TTL
    rate_limit_per_min = RATE_LIMIT_PER_MIN
    strict_number_check = STRICT_NUMBER_CHECK

    def to_dict(self):
        return {k: getattr(self, k) for k in dir(self)
                if not k.startswith("_") and not callable(getattr(self, k))}


settings = Settings()


def load_schema_meta():
    """读取业务字典（字段中文名、单位、口径、别名）。

    这份文件是**人和数据库之间的对照表**，schema 自动发现负责"库里有什么"，
    它负责"这些字段在业务上是什么意思"。两者不一致会被启动校验拦下来。
    """
    if not os.path.isfile(SCHEMA_META_PATH):
        return {}
    with open(SCHEMA_META_PATH, "r", encoding="utf-8") as f:
        return json.load(f)
