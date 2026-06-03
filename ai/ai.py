"""LangChain/LangGraph AI agent — code-review edition.

Mirrors the instacart-agent pattern (``AI`` + ``AIManager`` singletons, async
``setup_agent``, async-streaming ``ask``) but with two simplifications:

* Persistence: uses an in-process :class:`InMemorySaver` (no Postgres).
* No cross-thread store: tools persist their own state via
  :mod:`services.review_tracker_service` (a JSON file).

The model is **ASI:One** (OpenAI-compatible) accessed through
``langchain_openai.ChatOpenAI`` with ``base_url`` overridden to
``https://api.asi1.ai/v1``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from logging import Logger
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain.agents import create_agent  # pyright: ignore[reportUnknownVariableType]
from langchain.agents.structured_output import ToolStrategy
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

from ai.models import Response, UserContext
from ai.tools import tools

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig


ASI_ONE_BASE_URL = "https://api.asi1.ai/v1"


class AI:
    """Encapsulates the LangChain agent and its lifecycle."""

    def __init__(
        self,
        *,
        asi_one_api_key: str,
        asi_one_model: str = "asi1",
        github_token: str = "",
        dry_run: bool = False,
    ) -> None:
        self._asi_one_api_key = asi_one_api_key
        self._asi_one_model = asi_one_model
        self._github_token = github_token
        self._dry_run = dry_run
        self._checkpointer: InMemorySaver | None = None
        self._agent: Any = None

    async def setup_agent(self) -> None:
        """Build the LangChain agent. Idempotent."""
        if self._agent is not None:
            return

        self._checkpointer = InMemorySaver()

        prompt_file = Path(__file__).parent / "PROMPT.md"
        system_prompt = prompt_file.read_text(encoding="utf-8")

        model = ChatOpenAI(
            model=self._asi_one_model,
            api_key=self._asi_one_api_key,
            base_url=ASI_ONE_BASE_URL,
            temperature=0.4,
        )

        self._agent = create_agent(
            model=model,
            tools=tools,
            checkpointer=self._checkpointer,
            context_schema=UserContext,
            system_prompt=system_prompt,
            response_format=ToolStrategy(schema=Response),
        )

    @classmethod
    def _response_from_state(
        cls, final_state: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Pull a ``Response``-shaped dict out of LangGraph's final state."""
        if not final_state:
            return {
                "type": "error",
                "text": "Something went wrong. Please try again.",
                "pr_url": None,
                "issues_count": None,
                "posted_comments": None,
            }

        raw = final_state.get("structured_response")
        if raw is not None:
            try:
                if isinstance(raw, Response):
                    validated = raw
                elif isinstance(raw, dict):
                    validated = Response.model_validate(raw)
                else:
                    validated = Response.model_validate_json(json.dumps(raw, default=str))
                return {
                    "type": "response",
                    "text": validated.text,
                    "pr_url": validated.pr_url,
                    "issues_count": validated.issues_count,
                    "posted_comments": validated.posted_comments,
                }
            except Exception:
                pass

        for msg in reversed(final_state.get("messages") or []):
            if getattr(msg, "tool_calls", None):
                continue
            content = getattr(msg, "content", None)
            if not content:
                continue
            text = content if isinstance(content, str) else str(content)
            if text.strip():
                return {
                    "type": "response",
                    "text": text,
                    "pr_url": None,
                    "issues_count": None,
                    "posted_comments": None,
                }

        return {
            "type": "error",
            "text": "I couldn't produce a response. Please try again.",
        }

    async def ask(
        self,
        *,
        user_id: str,
        session_id: str,
        question: str,
        logger: Logger,
        github_token: str = "",
        dry_run: bool | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream progress updates and a final structured response.

        Yields dicts shaped like::

            {"type": "update",   "text": "Analyzing src/foo.py..."}
            {"type": "response", "text": "...", "pr_url": ..., ...}
            {"type": "error",    "text": "..."}
        """
        if not self._agent:
            yield {"type": "error", "text": "AI not initialized."}
            return

        ctx = UserContext(
            user_id=user_id,
            session_id=session_id,
            asi_one_api_key=self._asi_one_api_key,
            github_token=(github_token or self._github_token or ""),
            dry_run=self._dry_run if dry_run is None else dry_run,
        )

        config: RunnableConfig = {
            "configurable": {"thread_id": session_id},
            "recursion_limit": 60,
        }

        final_state: dict[str, Any] | None = None
        try:
            async for stream_mode, chunk in self._agent.astream(
                {"messages": [{"role": "user", "content": question}]},
                config,
                context=ctx,
                stream_mode=["custom", "values"],
            ):
                if stream_mode == "custom":
                    yield {"type": "update", "text": str(chunk)}
                elif stream_mode == "values":
                    final_state = chunk
        except Exception as exc:
            logger.exception("Agent stream failed: %s", exc)
            yield {"type": "error", "text": f"Agent failed: {exc}"}
            return

        out = self._response_from_state(final_state)
        logger.info(
            "[code-review-agent] final response type=%s len=%d posted=%s",
            out.get("type"),
            len(out.get("text") or ""),
            out.get("posted_comments"),
        )
        yield out


class AIManager:
    """Lazy singleton wrapper around :class:`AI`."""

    def __init__(self) -> None:
        self._ai: AI | None = None

    async def setup_ai_instance(
        self,
        *,
        asi_one_api_key: str,
        asi_one_model: str = "asi1",
        github_token: str = "",
        dry_run: bool = False,
    ) -> None:
        if self._ai is None:
            self._ai = AI(
                asi_one_api_key=asi_one_api_key,
                asi_one_model=asi_one_model,
                github_token=github_token,
                dry_run=dry_run,
            )
            await self._ai.setup_agent()

    def ask(
        self,
        *,
        user_id: str,
        session_id: str,
        question: str,
        logger: Logger,
        github_token: str = "",
        dry_run: bool | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        if not self._ai:
            raise RuntimeError("AI not initialized — call setup_ai_instance first")
        return self._ai.ask(
            user_id=user_id,
            session_id=session_id,
            question=question,
            logger=logger,
            github_token=github_token,
            dry_run=dry_run,
        )


_ai_manager_instance = AIManager()


async def setup_ai_instance(
    *,
    asi_one_api_key: str,
    asi_one_model: str = "asi1",
    github_token: str = "",
    dry_run: bool = False,
) -> None:
    """Module-level entry point used by :mod:`agent`."""
    await _ai_manager_instance.setup_ai_instance(
        asi_one_api_key=asi_one_api_key,
        asi_one_model=asi_one_model,
        github_token=github_token,
        dry_run=dry_run,
    )


def ask(
    *,
    user_id: str,
    session_id: str,
    question: str,
    logger: Logger,
    github_token: str = "",
    dry_run: bool | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Module-level entry point used by chat protocol and CLI."""
    return _ai_manager_instance.ask(
        user_id=user_id,
        session_id=session_id,
        question=question,
        logger=logger,
        github_token=github_token,
        dry_run=dry_run,
    )
