"""Validated five-dimension article scoring and ad heuristics."""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping


RUBRIC_TECHNICAL = "technical"
RUBRIC_CONTENT_OPS = "content_ops"
RUBRIC_NAMES = (RUBRIC_TECHNICAL, RUBRIC_CONTENT_OPS)

RUBRICS: dict[str, dict[str, dict[str, float | str]]] = {
    RUBRIC_TECHNICAL: {
        "技术深度": {"weight": 0.30, "description": "技术细节、原创方案与推导"},
        "信息新颖度": {"weight": 0.20, "description": "独家程度、时效性与增量信息"},
        "分析深度与独立观点": {"weight": 0.25, "description": "独立判断、批判分析与趋势推演"},
        "实用参考价值": {"weight": 0.15, "description": "可复用方法、行动建议与决策价值"},
        "内容质量与可信度": {"weight": 0.10, "description": "引用、来源、事实观点区分"},
    },
    RUBRIC_CONTENT_OPS: {
        "选题与问题定义": {"weight": 0.20, "description": "读者是谁、要解决的问题是否清楚"},
        "证据与信息增量": {"weight": 0.25, "description": "新数据、案例或一手观察，而不是复述常识"},
        "判断与框架": {"weight": 0.25, "description": "有独立判断和可用框架，而不是堆砌信息"},
        "可执行建议": {"weight": 0.20, "description": "读完能采取的具体动作"},
        "来源与可信度": {"weight": 0.10, "description": "出处、事实和观点是否分开"},
    },
}

# Backward-compatible alias for the default technical rubric.
SCORING_DIMENSIONS = RUBRICS[RUBRIC_TECHNICAL]

AD_TITLE_PATTERNS = [
    re.compile(r"^(推广|广告|赞助|特约)\s*[|｜：:]\s*"),
    re.compile(r"[|｜：:]\s*(推广|广告|赞助|特约)\s*$"),
    re.compile(r"^[【\[(](推广|广告|赞助|特约)[】\])]"),
]
AD_DISCLOSURES = (
    "本文为推广",
    "本文为广告",
    "本文包含广告",
    "本文含广告",
    "商业合作",
    "赞助内容",
)


def rubric_dimensions(name: str = RUBRIC_TECHNICAL) -> dict[str, dict[str, float | str]]:
    try:
        return RUBRICS[name]
    except KeyError as exc:
        raise ValueError(
            "rubric must be technical or content_ops"
        ) from exc


def public_rubric(name: str = RUBRIC_TECHNICAL) -> dict[str, object]:
    dimensions = rubric_dimensions(name)
    return {
        "rubric": name,
        "dimensions": [
            {
                "name": key,
                "weight": details["weight"],
                "description": details["description"],
            }
            for key, details in dimensions.items()
        ],
    }


def validate_scores(
    scores: Mapping[str, float], *, rubric: str = RUBRIC_TECHNICAL
) -> dict[str, float]:
    dimensions = rubric_dimensions(rubric)
    if set(scores) != set(dimensions):
        missing = sorted(set(dimensions) - set(scores))
        extra = sorted(set(scores) - set(dimensions))
        raise ValueError(
            f"all five {rubric} dimensions are required; missing={missing}, extra={extra}"
        )
    validated: dict[str, float] = {}
    for name in dimensions:
        value = scores[name]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{name} must be numeric")
        if not 1 <= float(value) <= 10:
            raise ValueError(f"{name} must be between 1 and 10")
        validated[name] = float(value)
    return validated


def calculate_score(
    scores: Mapping[str, float], *, rubric: str = RUBRIC_TECHNICAL
) -> float:
    validated = validate_scores(scores, rubric=rubric)
    dimensions = rubric_dimensions(rubric)
    total = sum(
        Decimal(str(validated[name])) * Decimal(str(dimensions[name]["weight"]))
        for name in dimensions
    )
    return float(total.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def validate_total_score(score: float) -> float:
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise ValueError("score must be numeric")
    value = float(score)
    if not 1 <= value <= 10:
        raise ValueError("score must be between 1 and 10")
    return round(value, 1)


def format_rationale(
    scores: Mapping[str, float], *, rubric: str = RUBRIC_TECHNICAL
) -> str:
    validated = validate_scores(scores, rubric=rubric)
    return " | ".join(
        f"{name} {validated[name]:g}/10" for name in rubric_dimensions(rubric)
    )


def is_advertisement(title: str = "", content: str = "") -> bool:
    normalized_title = title.strip()
    if any(pattern.search(normalized_title) for pattern in AD_TITLE_PATTERNS):
        return True
    prefix = content[:800]
    return any(disclosure in prefix for disclosure in AD_DISCLOSURES)


def should_sync(score: float, minimum: float) -> bool:
    return validate_total_score(score) >= validate_total_score(minimum)
