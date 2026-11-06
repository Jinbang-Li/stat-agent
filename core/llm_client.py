# -*- coding: utf-8 -*-
"""
LLM 调用封装。

设计取舍：
1. 默认不依赖 openai 官方 SDK，直接用标准库 urllib。理由：DeepSeek 的接口是
   OpenAI 兼容的，一个 POST 就够了；少一个依赖，clone 下来就能跑。
   但如果环境里装了 openai（`pip install openai`），会自动改走 SDK——
   SDK 负责重试、超时、流式等细节，生产环境更省心；两条路径的返回与计量完全一致。
2. 三种运行模式：
     deepseek —— 有 API Key，走官方接口
     ollama   —— 本地模型，走 11434 端口（Ollama 也提供 OpenAI 兼容的 /v1）
     offline  —— 无任何模型可用，所有 LLM 环节降级为规则实现
   offline 不是"残废模式"：意图识别走关键词规则、SQL 走模板，整个链路依然能跑完，
   而且这个降级路径本身就是幻觉控制的一部分（LLM 不可用时系统必须仍然给出可靠结果）。
3. 每次调用都打印 token 用量（prompt / completion / total），并把明细写进
   output/logs/llm_calls.jsonl。理由很实际：没有计量就没有成本意识，
   一个"每次多问两轮"的实现和一个"只问一轮"的实现，账单能差三倍。
"""

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

try:                                    # 可选依赖：装了就用，没装也不影响
    from openai import OpenAI as _OpenAI
    _HAS_OPENAI = True
except Exception:                       # noqa: BLE001
    _OpenAI = None
    _HAS_OPENAI = False


def _host_port(url):
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname or "127.0.0.1"
        if host == "localhost":      # 避免先试 ::1 再试 127.0.0.1，白白多等一倍
            host = "127.0.0.1"
        return (host, p.port or 11434)
    except Exception:
        return ("127.0.0.1", 11434)


_OLLAMA_CACHE = {"ok": None, "ts": 0.0}


class LLMError(Exception):
    """LLM 调用失败（网络、鉴权、返回格式异常）。"""


class LLMClient(object):
    def __init__(self, provider=None, api_key=None, base_url=None, model=None,
                 temperature=None, quiet=False):
        # 一律从 config 读，config 又优先读环境变量、其次读 .env——
        # 也就是说 .env 里配了 key，这里就自动是 "在线"。
        self.api_key = (api_key or config.deepseek_key()).strip()
        self.base_url = (base_url or config.deepseek_base_url()).rstrip("/")
        self.model = model or config.DEFAULT_MODEL
        self.temperature = config.TEMPERATURE if temperature is None else temperature
        self.quiet = quiet
        self.calls = []          # 本次会话的调用记录，供链路展示
        # token 计量：成本是可运维性的一部分，不能"用着不知道花了多少"
        self.last_usage = {}
        self.total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        # 装了 openai SDK 就走 SDK；某次调用 SDK 出错则自动退回 urllib，不再重试 SDK，
        # 避免"SDK 挂了但每次调用都先等它超时"。
        self.sdk_enabled = _HAS_OPENAI and os.environ.get("STAT_AGENT_NO_SDK", "") == ""
        self._sdk_client = None

        # 模式判定，规则只有一条：**有 key 就上线，没 key 就离线**。
        # 显式 STAT_AGENT_PROVIDER 优先于自动判断（调试用）；
        # 本地 Ollama 必须显式开启（STAT_AGENT_USE_OLLAMA=1），
        # 否则每次提问都要去探一个不存在的端口。
        explicit = (provider or os.environ.get("STAT_AGENT_PROVIDER", "")).strip().lower()
        if explicit in ("deepseek", "ollama", "offline"):
            self.provider = explicit
        elif self.api_key:
            self.provider = "deepseek"
        elif self._ollama_opted_in():
            self.provider = "ollama" if self._ollama_alive() else "offline"
        else:
            self.provider = "offline"

        if self.provider == "deepseek" and not self.api_key:
            self.provider = "offline"      # 显式要求在线但没给 key：退回离线，不炸
        if self.provider == "ollama":
            self.base_url = config.OLLAMA_BASE_URL
            self.model = model or config.OLLAMA_MODEL

    @property
    def has_key(self):
        return bool(self.api_key)

    @staticmethod
    def _ollama_opted_in():
        return os.environ.get("STAT_AGENT_USE_OLLAMA", "").strip().lower() in ("1", "true", "yes", "on")

    # ------------------------------------------------------------ 探测
    def _ollama_alive(self):
        # 本机没有 Ollama 时，向 localhost:11434 发请求在 Windows 上不会立刻被拒绝，
        # 而是被防火墙静默丢包直到超时。实测每次探测要 0.4–0.8 秒，放在用户第一次
        # 提问的路径上就是"点一下先卡一秒"。所以：① 只连 127.0.0.1（不走 IPv6，
        # 避免两次探测）；② 超时压到 0.15 秒；③ 结果缓存 60 秒，进程内只探测一次。
        now = time.time()
        if _OLLAMA_CACHE["ok"] is not None and now - _OLLAMA_CACHE["ts"] < 60:
            return _OLLAMA_CACHE["ok"]
        host, port = _host_port(config.OLLAMA_BASE_URL)
        ok = False
        try:
            sock = socket.create_connection((host, port), timeout=0.15)
            sock.close()
            ok = True
        except Exception:
            ok = False
        if ok:
            try:
                req = urllib.request.Request(config.OLLAMA_BASE_URL + "/api/tags")
                with urllib.request.urlopen(req, timeout=0.5) as resp:
                    ok = resp.status == 200
            except Exception:
                ok = False
        _OLLAMA_CACHE["ok"] = ok
        _OLLAMA_CACHE["ts"] = now
        return ok

    @property
    def online(self):
        return self.provider != "offline"

    def describe(self):
        if self.provider == "offline":
            return "离线规则模式（未配置 DEEPSEEK_API_KEY：意图走关键词、SQL 走模板、报告走模板）"
        return "%s / %s / T=%s / %s / key=%s" % (
            self.provider, self.model, self.temperature,
            "openai-sdk" if self.sdk_enabled else "urllib",
            config.mask_key(self.api_key))

    def probe(self):
        """发一次最小请求，确认 key 能不能用。返回 (ok, message)。

        doctor 里调用。失败不抛异常——"配置检查"这件事本身不该再抛异常，
        它存在的意义就是把异常翻译成一句人话。
        """
        if self.provider == "offline":
            return (False, "未配置 DEEPSEEK_API_KEY（在 .env 里写一行即可）")
        try:
            text = self.chat("你是连通性测试助手。", "只回复两个字符：OK",
                             temperature=0.0, max_tokens=8, retries=0)
            return (True, "连通正常（模型回：%s）" % (text or "").strip()[:40])
        except Exception as e:                       # noqa: BLE001
            return (False, str(e)[:200])

    # ------------------------------------------------------------ 调用
    def chat(self, system_prompt, user_prompt, temperature=None, max_tokens=None,
             retries=None):
        """调用模型，返回纯文本。offline 模式直接抛 LLMError。"""
        if self.provider == "offline":
            raise LLMError("offline：无可用模型")

        retries = config.LLM_MAX_RETRIES if retries is None else retries
        last_err = None
        for attempt in range(retries + 1):
            started = time.time()
            try:
                text = self._request_once(system_prompt, user_prompt,
                                          temperature, max_tokens)
                self._log(True, time.time() - started, len(text), attempt)
                return text
            except Exception as e:                       # noqa: BLE001
                last_err = e
                self._log(False, time.time() - started, 0, attempt, str(e))
                if attempt < retries:
                    time.sleep(1.0 * (attempt + 1))
        raise LLMError("调用失败：%s" % last_err)

    def try_chat(self, system_prompt, user_prompt, **kwargs):
        """安全版本：任何失败都返回 None，由调用方走规则兜底。"""
        try:
            return self.chat(system_prompt, user_prompt, **kwargs)
        except Exception:
            return None

    # ------------------------------------------------------------ 传输层
    def _request_once(self, system_prompt, user_prompt, temperature, max_tokens):
        temperature = self.temperature if temperature is None else temperature
        max_tokens = max_tokens or config.LLM_MAX_TOKENS

        if self.sdk_enabled:
            try:
                return self._request_sdk(system_prompt, user_prompt, temperature, max_tokens)
            except Exception as e:                       # noqa: BLE001
                # SDK 走不通（版本不兼容、代理、参数差异）就退回 urllib，
                # 并且本次进程不再尝试 SDK，省掉后面每次调用的等待。
                self.sdk_enabled = False
                self._sdk_client = None
                if not self.quiet:
                    sys.stderr.write("[llm] openai SDK 不可用，改用 urllib：%s\n" % e)

        return self._request_urllib(system_prompt, user_prompt, temperature, max_tokens)

    def _sdk(self):
        if self._sdk_client is None:
            kwargs = {"api_key": self.api_key or "ollama", "timeout": config.LLM_TIMEOUT,
                      "max_retries": 0}      # 重试由 chat() 统一控制，避免两层重试叠乘
            if self.provider == "ollama":
                kwargs["base_url"] = self.base_url.rstrip("/") + "/v1"
            else:
                kwargs["base_url"] = self.base_url
            self._sdk_client = _OpenAI(**kwargs)
        return self._sdk_client

    def _request_sdk(self, system_prompt, user_prompt, temperature, max_tokens):
        resp = self._sdk().chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        choices = getattr(resp, "choices", None) or []
        if not choices:
            raise LLMError("返回中没有 choices")
        content = (getattr(choices[0].message, "content", "") or "").strip()
        if not content:
            raise LLMError("模型返回空内容")
        usage = getattr(resp, "usage", None)
        self._account_usage(getattr(usage, "prompt_tokens", 0),
                            getattr(usage, "completion_tokens", 0),
                            getattr(usage, "total_tokens", 0))
        return content

    def _request_urllib(self, system_prompt, user_prompt, temperature, max_tokens):
        if self.provider == "ollama":
            url = self.base_url + "/api/chat"
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "options": {"temperature": temperature, "num_predict": max_tokens},
            }
            headers = {"Content-Type": "application/json"}
        else:
            url = self.base_url + "/chat/completions"
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            }
            headers = {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            }

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            raise LLMError("HTTP %s: %s" % (e.code, detail))

        data = json.loads(raw)

        if self.provider == "ollama":
            content = (data.get("message") or {}).get("content", "")
        else:
            choices = data.get("choices") or []
            if not choices:
                raise LLMError("返回中没有 choices：%s" % raw[:200])
            content = (choices[0].get("message") or {}).get("content", "")

        if not content or not content.strip():
            raise LLMError("模型返回空内容")

        usage = data.get("usage") or {}
        if usage:
            self._account_usage(usage.get("prompt_tokens"), usage.get("completion_tokens"),
                                usage.get("total_tokens"))
        return content.strip()

    # ------------------------------------------------------------ 计量
    def _account_usage(self, prompt_tokens, completion_tokens, total_tokens):
        """记录并打印本次调用的 token 用量。

        打印是刻意的：接一次模型花多少，只有每次都看见才会形成直觉。
        累计值也能直接回答"这个功能上线一个月大概多少钱"。
        """
        p = int(prompt_tokens or 0)
        c = int(completion_tokens or 0)
        t = int(total_tokens or (p + c))
        self.last_usage = {"prompt_tokens": p, "completion_tokens": c, "total_tokens": t}
        self.total_usage["prompt_tokens"] += p
        self.total_usage["completion_tokens"] += c
        self.total_usage["calls"] += 1

        if not self.quiet:
            sys.stdout.write("[tokens] prompt_tokens=%d completion_tokens=%d total_tokens=%d"
                             "  (累计 %d 次调用：in=%d out=%d)\n"
                             % (p, c, t, self.total_usage["calls"],
                                self.total_usage["prompt_tokens"],
                                self.total_usage["completion_tokens"]))
            sys.stdout.flush()

    # ------------------------------------------------------------ 日志
    def _log(self, ok, elapsed, n_chars, attempt, error=""):
        record = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "provider": self.provider,
            "model": self.model,
            "temperature": self.temperature,
            "ok": ok,
            "elapsed_s": round(elapsed, 2),
            "chars": n_chars,
            "attempt": attempt,
            "error": error,
        }
        self.calls.append(record)
        if self.quiet:
            return
        try:
            config.ensure_dirs()
            path = os.path.join(config.LOG_DIR, "llm_calls.jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass


_DEFAULT_CLIENT = [None]


def get_default_client(quiet=True):
    """进程内共享的 LLM 客户端。

    每次提问都 new 一个客户端，意味着每次都探测一遍 Ollama 端口；在没装 Ollama
    的机器上这是纯浪费。客户端本身无状态（只有累计 token 计量），共享是安全的。
    """
    if _DEFAULT_CLIENT[0] is None:
        _DEFAULT_CLIENT[0] = LLMClient(quiet=quiet)
    return _DEFAULT_CLIENT[0]


def reset_default_client():
    """配置变更（换 key / 换模型）后调用，让下次请求重新探测。"""
    _DEFAULT_CLIENT[0] = None


def load_prompt(name):
    """读取 prompt/ 下的提示词文件。

    容错两件事：
    1. 调用方写 "report_prompt" 还是 "report_prompt.txt" 都认——少写扩展名是
       最常见的低级错误，而在报告层抛异常只会变成一句"报告生成失败"，很难查；
    2. 文件真的不存在时，把目录里有哪些文件列出来，错误信息直接可行动。
    """
    names = [name]
    if not name.lower().endswith((".txt", ".md")):
        names.append(name + ".txt")
    for n in names:
        path = os.path.join(config.PROMPT_DIR, n)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()

    try:
        have = sorted(os.listdir(config.PROMPT_DIR))
    except OSError:
        have = []
    raise IOError("提示词文件缺失：%s（prompt/ 下有：%s）"
                  % ("、".join(names), "、".join(have) or "空目录"))
