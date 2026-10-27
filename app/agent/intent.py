from __future__ import annotations

from enum import Enum


class AnalysisIntent(str, Enum):
    QUERY = "query"
    RANKING = "ranking"
    TREND = "trend"
    PROPORTION = "proportion"
    CHAT = "chat"


PATTERNS = {
    AnalysisIntent.PROPORTION: ("占比", "比例", "百分比", "份额", "ratio", "percentage", "share"),
    AnalysisIntent.TREND: ("趋势", "变化", "每月", "每年", "环比", "同比", "trend", "monthly", "yearly"),
    AnalysisIntent.RANKING: ("排名", "最高", "最低", "最多", "最少", "前五", "前10", "top", "rank"),
    AnalysisIntent.QUERY: ("多少", "数量", "总数", "平均", "合计", "销售额", "订单", "客户", "count", "sum", "average"),
}
CHAT_PATTERNS = (
    "你是谁", "你好", "谢谢", "能做什么", "支持哪些", "支持分析", "可以分析",
    "经营指标", "怎么使用", "帮助", "hello", "hi",
)


def classify_intent(question: str) -> tuple[AnalysisIntent, str]:
    text = question.strip().lower()
    if any(pattern in text for pattern in CHAT_PATTERNS):
        return AnalysisIntent.CHAT, "问题是在询问助手能力或进行一般对话"
    for intent in (AnalysisIntent.PROPORTION, AnalysisIntent.TREND, AnalysisIntent.RANKING, AnalysisIntent.QUERY):
        matched = [pattern for pattern in PATTERNS[intent] if pattern in text]
        if matched:
            return intent, f"命中{intent.value}特征：{', '.join(matched[:3])}"
    return AnalysisIntent.QUERY, "未命中闲聊特征，按通用数据查询处理"
