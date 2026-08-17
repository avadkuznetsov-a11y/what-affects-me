"""Что на меня влияет — ядро гипотезы: дневник, данные, связи, проверка."""

from .schema import DayRecord, Fact, Timeline
from .extract import RuleExtractor, LLMExtractor
from .insights import find_links, summarise, Link
from .experiments import Experiment, evaluate, Verdict
from .wearables import SberRingSource, AppleHealthSource, merge_into

__version__ = "0.1.0"
__all__ = [
    "DayRecord", "Fact", "Timeline",
    "RuleExtractor", "LLMExtractor",
    "find_links", "summarise", "Link",
    "Experiment", "evaluate", "Verdict",
    "SberRingSource", "AppleHealthSource", "merge_into",
]
