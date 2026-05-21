from __future__ import annotations

import re
from typing import List

from app.config import settings


class UrgencyEngine:
    """Rule-based urgency boosts and classification for mention analysis."""

    def __init__(self) -> None:
        self.critical_terms = self._parse_terms(settings.urgency_patterns_critical)
        self.high_terms = self._parse_terms(settings.urgency_patterns_high)
        self.medium_terms = self._parse_terms(settings.urgency_patterns_medium)

        self._critical_patterns = [(term, re.compile(re.escape(term), re.IGNORECASE)) for term in self.critical_terms]
        self._high_patterns = [(term, re.compile(re.escape(term), re.IGNORECASE)) for term in self.high_terms]
        self._medium_patterns = [(term, re.compile(re.escape(term), re.IGNORECASE)) for term in self.medium_terms]

    @staticmethod
    def _parse_terms(raw_terms: str | None) -> List[str]:
        if not raw_terms:
            return []
        terms = [term.strip().lower() for term in str(raw_terms).split("|")]
        return [term for term in terms if term]

    @staticmethod
    def _find_matches(text: str, patterns: list[tuple[str, re.Pattern[str]]]) -> List[str]:
        matches: List[str] = []
        seen: set[str] = set()
        for term, pattern in patterns:
            if term in seen:
                continue
            if pattern.search(text):
                matches.append(term)
                seen.add(term)
        return matches

    def extract_factors(self, text: str) -> List[str]:
        normalized_text = str(text or "").lower()

        factors = []
        factors.extend(self._find_matches(normalized_text, self._critical_patterns))
        factors.extend(self._find_matches(normalized_text, self._high_patterns))
        factors.extend(self._find_matches(normalized_text, self._medium_patterns))
        return factors

    def boost_score(self, llm_score: float, factors: List[str]) -> float:
        score = float(llm_score or 0.0)

        critical_count = sum(1 for factor in factors if factor in self.critical_terms)
        high_count = sum(1 for factor in factors if factor in self.high_terms)

        score += min(critical_count * 0.20, 0.40)
        score += min(high_count * 0.10, 0.20)

        return max(0.0, min(round(score, 4), 1.0))

    def classify(self, score: float) -> str:
        value = float(score or 0.0)

        if value >= float(settings.urgency_threshold_critical):
            return "crítica"
        if value >= float(settings.urgency_threshold_high):
            return "alta"
        if value >= float(settings.urgency_threshold_medium):
            return "média"
        return "baixa"


urgency_engine = UrgencyEngine()
