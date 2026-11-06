# -*- coding: utf-8 -*-
"""
意图识别层。

职责：把用户的一句中文，归到 7 个分析意图之一，并标记它是否在问"因果"。

关键设计：
- 意图识别的输出**只影响路由走向**，不直接产生结论。所以即使识别错了，
  下游还有统计前提校验兜底（例如识别成 corr 但只有一个数值列，router 会拒绝）。
- 有 LLM 时用 LLM 分类，失败或返回不合法值时，回退到关键词规则。
- 关键词规则的顺序是刻意安排的：先判"异常/相关/分组/预测/聚类"，
  最后才落到"描述"和"查询"，因为后者是兜底类别，误判成本最低。
"""

import json
import re

from .llm_client import get_default_client

INTENTS = ("query", "describe", "corr", "group", "timeseries", "cluster", "outlier")

INTENT_LABELS = {
    "query": "数据查询",
    "describe": "描述统计",
    "corr": "相关与显著性检验",
    "group": "组间差异检验",
    "timeseries": "时间序列分析",
    "cluster": "聚类分群",
    "outlier": "异常值诊断",
}

# 规则兜底用的关键词；按"先严后松"的顺序匹配
_RULES = [
    ("outlier", ["异常", "离群", "极端值", "偏高的县", "偏低的县", "找出.*异常",
                 "异常偏高", "异常偏低", "离群点"]),
    ("corr", ["相关", "关联", "是否显著", "显著性", "检验.*关系", "有关系吗",
              "关系如何", "关系", "同向", "正相关", "负相关", "影响", "有关"]),
    ("group", ["分组", "分类", "不同类型", "不同地形", "不同省", "差异", "比较.*均值",
               "方差分析", "组间", "谁更高", "哪类.*高"]),
    ("timeseries", ["预测", "趋势", "时间序列", "走势", "未来", "下一年", "下季度",
                    "未来几年", "增速", "变化趋势", "arima"]),
    ("cluster", ["聚类", "分群", "聚集", "归类", "相似.*县", "分成几类", "kmeans"]),
    ("describe", ["分布", "描述", "概况", "中位数", "四分位", "极值", "离散",
                  "平均水平", "波动", "概况"]),
]

# 因果措辞：识别出来不是为了做因果推断，而是为了在报告里明确否认
_CAUSAL_PATTERNS = [
    r"导致", r"造成", r"引起", r"因为.{0,10}所以", r"影响因素", r"决定",
    r"推动", r"促进", r"带动", r"使得", r"影响(?!因素)",
]


class Intent(object):
    def __init__(self, name, method, reason, causal_request=False, raw=None):
        self.name = name
        self.method = method            # 'llm' | 'rule'
        self.reason = reason
        self.causal_request = causal_request
        self.raw = raw

    @property
    def label(self):
        return INTENT_LABELS.get(self.name, self.name)

    def to_dict(self):
        return {
            "intent": self.name,
            "label": self.label,
            "detected_by": self.method,
            "reason": self.reason,
            "causal_request": self.causal_request,
        }

    def __repr__(self):
        return "<Intent %s (%s)>" % (self.name, self.method)


def _detect_causal(question):
    for p in _CAUSAL_PATTERNS:
        if re.search(p, question):
            return True
    return False


def classify_by_rules(question):
    text = (question or "").strip().lower()
    causal = _detect_causal(question or "")
    for name, keywords in _RULES:
        for kw in keywords:
            if re.search(kw, text):
                return Intent(name, "rule", "命中关键词「%s」" % kw, causal)
    # 默认：有"多少/是多少/均值"这类就是查询，否则按描述统计处理
    if re.search(r"多少|是多少|平均|合计|最高|最低|排名|前几|top", text):
        return Intent("query", "rule", "未命中分析类关键词，判定为简单取数", causal)
    return Intent("describe", "rule", "未命中明确意图，默认走描述统计", causal)


_SYS = """你是一个意图分类器。把用户的经济统计问题归入且仅归入下列 7 类之一：

query       单纯取数。只要一个或几个数值，不需要统计推断。例："2023年各县GDP均值是多少"
describe    要看分布形态、集中趋势、离散程度。例："看看居民收入的分布和极值"
corr        问两个变量是否相关/是否显著。例："收入和消费支出是否显著相关"
group       问不同类别组的均值是否存在差异。例："平原县和山地县的收入有差异吗"
timeseries  问趋势、预测、时间路径。例："预测 2024 年的产值"
cluster     问能否把样本分成几类。例："把这些县按经济结构分成几类"
outlier     问哪些样本异常。例："找出产值异常偏高的县"

只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块：
{"intent": "<上述七选一>", "reason": "<不超过 25 字的中文理由>"}

如果用户问题里带有"导致/影响/因为"这类因果措辞，在 reason 里加一句"用户在问因果"，
但 intent 仍然按上面的语义归类（本系统只做相关分析）。"""


def classify(question, llm=None):
    """主入口：优先 LLM，失败回退规则。"""
    llm = llm or get_default_client()
    causal = _detect_causal(question)

    if llm.online:
        raw = llm.try_chat(_SYS, "用户问题：%s" % question, temperature=0.0,
                           max_tokens=200)
        if raw:
            parsed = _parse_json(raw)
            if parsed and parsed.get("intent") in INTENTS:
                return Intent(parsed["intent"], "llm",
                              parsed.get("reason", "模型分类"),
                              causal, raw=raw)

    return classify_by_rules(question)


def _parse_json(text):
    """模型经常把 JSON 包在代码块里，这里做容错提取。"""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None
