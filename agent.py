#!/usr/bin/env python3
"""Code Review Agent — autonomous GitHub PR reviewer (pure LangChain).

Architecture:

* :mod:`ai` houses the LangChain/LangGraph agent (``ai/ai.py``), the system
  prompt (``ai/PROMPT.md``), the Pydantic models (``ai/models.py``), and the
  tool surface (``ai/tools.py``).
* This file is a thin entry point with two modes, both funnelling through the
  same ``ai.ask`` pipeline so they take *exactly* the same code path:
    - ``review`` (default): run one review and exit (used by GitHub Actions).
    - ``chat``: a simple interactive REPL.

The agent never merges or approves PRs. The only writes it can make are
issue comments and pull-request review comments.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from typing import Any, Optional

import dotenv

dotenv.load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("agent")


_REQUIRED_ENV_VARS = ["ASI_ONE_API_KEY"]


def _validate_env(strict: bool = True) -> None:
    missing = [v for v in _REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing and strict:
        raise RuntimeError(f"Missing required env var(s): {', '.join(missing)}")
    if missing:
        log.warning("Env not fully configured: missing %s", ", ".join(missing))


async def _setup_ai(dry_run: bool) -> None:
    from ai import setup_ai_instance

    await setup_ai_instance(
        asi_one_api_key=os.environ["ASI_ONE_API_KEY"],
        asi_one_model=os.getenv("ASI_ONE_MODEL", "asi1"),
        github_token=os.getenv("GITHUB_TOKEN", ""),
        dry_run=dry_run,
    )


async def _stream_review(question: str, dry_run: bool) -> dict[str, Any]:
    """Run a single ``ask()`` call to completion and return the final response."""
    from ai import ask

    session_id = f"cli-{uuid.uuid4().hex[:12]}"
    final: dict[str, Any] | None = None
    async for chunk in ask(
        user_id="cli",
        session_id=session_id,
        question=question,
        logger=log,
        github_token=os.getenv("GITHUB_TOKEN", ""),
        dry_run=dry_run,
    ):
        kind = chunk.get("type")
        if kind == "update":
            log.info("[%s] %s", session_id, chunk.get("text"))
        elif kind in {"response", "error"}:
            final = chunk

    return final or {"type": "error", "text": "no response"}


# ---------------------------------------------------------------------------
# CLI review mode (used by GitHub Actions auto-review)
# ---------------------------------------------------------------------------


async def run_cli_review(argv: list[str]) -> int:
    """Run one code review and exit."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="code-review-agent review",
        description="Run one code review and exit.",
    )
    parser.add_argument("repo", help="GitHub repo as 'owner/repo' or full URL")
    parser.add_argument("--pr", type=int, default=None, help="PR number to review")
    parser.add_argument("--branch", default=None, help="Branch (only used when --pr is omitted)")
    parser.add_argument(
        "--message",
        default=None,
        help="Override the user message sent to the agent",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate the review but do not post comments")
    args = parser.parse_args(argv)

    dry_run = args.dry_run or os.getenv("DRY_RUN", "false").lower() == "true"

    if args.message:
        question = args.message
    elif args.pr:
        question = (
            f"Auto-review pull request #{args.pr} on {args.repo}. "
            "Follow the standard playbook: fetch the PR metadata, read the changed "
            "files, run static analysis on each, synthesize a structured review, "
            f"and post comments. {'DRY-RUN MODE: do not post.' if dry_run else 'Post the comments.'}"
        )
    else:
        branch_clause = f" branch {args.branch}" if args.branch else ""
        question = (
            f"Review the{branch_clause} repo {args.repo}. List the reviewable files, "
            "read the most important ones, run static analysis, and produce a "
            "review in your final response. There is no PR to post to."
        )

    _validate_env(strict=True)
    await _setup_ai(dry_run=dry_run)
    result = await _stream_review(question, dry_run=dry_run)

    text = (result.get("text") or "").strip()
    posted = result.get("posted_comments")
    print(text)
    if posted is not None:
        print(f"\n[posted_comments={posted} dry_run={dry_run}]")

    return 0 if result.get("type") == "response" else 1


# ---------------------------------------------------------------------------
# Interactive chat mode
# ---------------------------------------------------------------------------


WELCOME = (
    "Code Review Agent (LangChain). I review GitHub repos and PRs and post "
    "comments only — I never merge.\n"
    "  review owner/repo #123   review PR #123\n"
    "  review owner/repo         review the default branch (no posting)\n"
    "  history                   recent reviews\n"
    "  exit / quit               leave\n"
)


async def run_chat() -> int:
    """A simple interactive REPL over ``ai.ask`` — pure LangChain, no network agent."""
    from ai import ask

    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    _validate_env(strict=True)
    await _setup_ai(dry_run=dry_run)

    session_id = f"chat-{uuid.uuid4().hex[:12]}"
    print(WELCOME)

    loop = asyncio.get_event_loop()
    while True:
        try:
            question = (await loop.run_in_executor(None, input, "you > ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            continue
        if question.lower() in {"exit", "quit", ":q"}:
            break

        final_text: str | None = None
        async for chunk in ask(
            user_id="chat",
            session_id=session_id,
            question=question,
            logger=log,
            github_token=os.getenv("GITHUB_TOKEN", ""),
            dry_run=dry_run,
        ):
            kind = chunk.get("type")
            if kind == "update":
                upd = (chunk.get("text") or "").strip()
                if upd:
                    print(f"  … {upd}")
            elif kind == "response":
                final_text = (chunk.get("text") or "").strip() or "Review complete."
                posted = chunk.get("posted_comments")
                pr_url = chunk.get("pr_url")
                trailer = []
                if posted is not None:
                    trailer.append(f"posted {posted} comment(s)")
                if pr_url:
                    trailer.append(pr_url)
                if trailer:
                    final_text += "\n\n— " + " · ".join(trailer)
            elif kind == "error":
                final_text = f"Error: {chunk.get('text') or 'something went wrong.'}"

        print(f"\nagent > {final_text or 'No response.'}\n")

    print("Bye.")
    return 0


# ---------------------------------------------------------------------------
# Backward-compatible synchronous helper (used by older callers / tests)
# ---------------------------------------------------------------------------


def run_review(
    repo_url: str,
    pr_number: Optional[int] = None,
    branch: Optional[str] = None,
    files: Optional[list[str]] = None,  # noqa: ARG001
    dry_run: bool = False,
) -> str:
    """Sync convenience wrapper that runs one review through the AI pipeline."""
    if pr_number:
        question = (
            f"Auto-review pull request #{pr_number} on {repo_url}. "
            "Follow the standard playbook end-to-end. "
            f"{'DRY-RUN: do not post.' if dry_run else 'Post the comments.'}"
        )
    else:
        bc = f" branch {branch}" if branch else ""
        question = (
            f"Review the{bc} repo {repo_url}. Produce the review in your final "
            "response. No PR to post to."
        )

    async def _go() -> str:
        await _setup_ai(dry_run=dry_run)
        result = await _stream_review(question, dry_run=dry_run)
        return (result.get("text") or "").strip()

    return asyncio.run(_go())


def main() -> int:
    """Entry point. ``review`` (default) runs one review; ``chat`` starts a REPL."""
    argv = sys.argv[1:]
    mode = "review"
    if argv and argv[0] in ("--cli", "cli", "--review", "review"):
        argv = argv[1:]
    elif argv and argv[0] in ("--chat", "chat"):
        mode = "chat"
        argv = argv[1:]

    if mode == "chat":
        return asyncio.run(run_chat())
    return asyncio.run(run_cli_review(argv))


if __name__ == "__main__":
    sys.exit(main())
