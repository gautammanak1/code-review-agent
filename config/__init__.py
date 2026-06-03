"""Configuration package: tracked repos, review rules, language list."""

from .sources import (
    LANGUAGES_TO_REVIEW,
    REVIEW_RULES,
    TRACKED_REPOS,
)

__all__ = ["LANGUAGES_TO_REVIEW", "REVIEW_RULES", "TRACKED_REPOS"]
