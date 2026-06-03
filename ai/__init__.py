"""LangChain/LangGraph-powered AI layer for the Code Review Agent.

Public surface (mirrors the instacart-agent pattern):

* :func:`setup_ai_instance` — build the singleton ``AI`` instance once at startup.
* :func:`ask` — async generator that streams progress updates and the final
  structured :class:`ai.models.Response`.
"""

from ai.ai import ask, setup_ai_instance

__all__ = ["ask", "setup_ai_instance"]
