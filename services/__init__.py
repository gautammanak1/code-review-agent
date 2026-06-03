"""Lightweight service helpers used by the LangChain tools.

Most of the heavy lifting (GitHub I/O, LLM calls) now lives in :mod:`ai.tools`.
What remains here are the deterministic helpers worth keeping testable in
isolation:

- :mod:`services.code_analyzer_service` — pure-Python static checks.
- :mod:`services.review_tracker_service` — JSON history persistence.
"""
