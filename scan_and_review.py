#!/usr/bin/env python3
"""Scan GitHub for open PRs assigned to (or review-requested from) a user and
review any that haven't been reviewed at their current head commit yet.

This is meant to run as a one-shot job in GitHub Actions on a schedule, so the
agent can auto-review PRs across **all** repos the token can see — without
installing a workflow into every target repo.

Dedup: after reviewing a PR at commit ``<sha>`` we post a hidden marker
comment ``<!-- code-review-agent: reviewed <sha> -->``. On the next scan we
skip any PR whose current head SHA already has that marker, so the same commit
is never reviewed twice (and pushing a new commit gets a fresh review).

Required env:
    GH_PAT            classic PAT with `repo` scope (cross-repo search + post)
    ASI_ONE_API_KEY   ASI:One key for the LLM

Optional env:
    WATCH_USERNAME    GitHub login to scan for (default: the PAT's own login)
    MAX_PRS           max PRs to review per run (default: 5)
    DRY_RUN           "true" → review but do not post
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from typing import Any

import dotenv
import httpx

dotenv.load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("scan")

GITHUB_API = "https://api.github.com"
SEARCH_API = f"{GITHUB_API}/search/issues"
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=15.0, pool=10.0)


def _marker(sha: str) -> str:
    return f"<!-- code-review-agent: reviewed {sha[:12]} -->"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "code-review-agent-scan",
    }


async def _whoami(client: httpx.AsyncClient) -> str:
    resp = await client.get(f"{GITHUB_API}/user")
    resp.raise_for_status()
    return resp.json().get("login") or ""


async def _find_prs(client: httpx.AsyncClient, user: str) -> list[dict[str, Any]]:
    """Open PRs where the user is an assignee or a requested reviewer."""
    seen: dict[str, dict[str, Any]] = {}
    for qualifier in (f"assignee:{user}", f"review-requested:{user}"):
        query = f"is:pr is:open {qualifier}"
        resp = await client.get(SEARCH_API, params={"q": query, "per_page": 30})
        if resp.status_code >= 400:
            log.warning("search '%s' -> %s: %s", query, resp.status_code, resp.text[:200])
            continue
        for item in resp.json().get("items", []) or []:
            url = (item.get("pull_request") or {}).get("url")
            if url:
                seen[url] = item
    return list(seen.values())


async def _resolve_pr(client: httpx.AsyncClient, item: dict[str, Any]) -> dict[str, Any] | None:
    url = (item.get("pull_request") or {}).get("url")
    if not url:
        return None
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.warning("PR fetch failed: %s", exc)
        return None


async def _already_reviewed(client: httpx.AsyncClient, repo: str, pr: int, sha: str) -> bool:
    marker = _marker(sha)
    page = 1
    while page <= 5:
        resp = await client.get(
            f"{GITHUB_API}/repos/{repo}/issues/{pr}/comments",
            params={"per_page": 100, "page": page},
        )
        if resp.status_code >= 400:
            return False
        batch = resp.json() or []
        for c in batch:
            if marker in (c.get("body") or ""):
                return True
        if len(batch) < 100:
            break
        page += 1
    return False


async def _post_marker(client: httpx.AsyncClient, repo: str, pr: int, sha: str) -> None:
    try:
        await client.post(
            f"{GITHUB_API}/repos/{repo}/issues/{pr}/comments",
            json={"body": _marker(sha)},
        )
    except Exception as exc:
        log.warning("could not post marker on %s#%d: %s", repo, pr, exc)


async def _review(repo: str, pr: int, token: str, dry_run: bool) -> dict[str, Any] | None:
    from ai import ask

    question = (
        f"Auto-review pull request #{pr} on {repo}. Follow the standard playbook "
        "end-to-end: fetch the PR metadata, read the changed files, run static "
        "analysis on each, synthesize a structured review, and "
        f"{'post comments to the PR.' if not dry_run else 'do NOT post (dry-run).'}"
    )
    final: dict[str, Any] | None = None
    async for chunk in ask(
        user_id="scan",
        session_id=f"scan-{uuid.uuid4().hex[:12]}",
        question=question,
        logger=log,
        github_token=token,
        dry_run=dry_run,
    ):
        if chunk.get("type") == "update":
            txt = (chunk.get("text") or "").strip()
            if txt:
                log.info("  %s", txt)
        elif chunk.get("type") in {"response", "error"}:
            final = chunk
    return final


async def main() -> int:
    token = (os.getenv("GH_PAT") or os.getenv("GITHUB_TOKEN") or "").strip()
    if not token:
        log.error("GH_PAT (or GITHUB_TOKEN) is required.")
        return 2
    if not (os.getenv("ASI_ONE_API_KEY") or "").strip():
        log.error("ASI_ONE_API_KEY is required.")
        return 2

    dry_run = (os.getenv("DRY_RUN") or "false").lower() == "true"
    max_prs = max(1, int(os.getenv("MAX_PRS") or "5"))

    from ai import setup_ai_instance

    await setup_ai_instance(
        asi_one_api_key=os.environ["ASI_ONE_API_KEY"],
        asi_one_model=os.getenv("ASI_ONE_MODEL", "asi1"),
        github_token=token,
        dry_run=dry_run,
    )

    async with httpx.AsyncClient(timeout=TIMEOUT, headers=_headers(token)) as client:
        user = (os.getenv("WATCH_USERNAME") or "").strip() or await _whoami(client)
        log.info("Scanning open PRs assigned to / review-requested from @%s (dry_run=%s)", user, dry_run)

        items = await _find_prs(client, user)
        log.info("Found %d candidate PR(s)", len(items))

        reviewed = 0
        for item in items:
            if reviewed >= max_prs:
                log.info("Hit MAX_PRS=%d; stopping this run.", max_prs)
                break
            pr = await _resolve_pr(client, item)
            if not pr:
                continue
            repo = (pr.get("base") or {}).get("repo", {}).get("full_name") or ""
            number = pr.get("number")
            head_sha = (pr.get("head") or {}).get("sha") or ""
            if not (repo and number and head_sha):
                continue

            if await _already_reviewed(client, repo, number, head_sha):
                log.info("skip %s#%d @%s (already reviewed)", repo, number, head_sha[:8])
                continue

            log.info("→ reviewing %s#%d @%s", repo, number, head_sha[:8])
            final = await _review(repo, number, token, dry_run)
            if final and final.get("type") == "response":
                log.info("  done: %s", (final.get("text") or "")[:200].replace("\n", " "))
                if not dry_run:
                    await _post_marker(client, repo, number, head_sha)
                reviewed += 1
            else:
                log.error("  review failed: %s", final.get("text") if final else "no response")

        log.info("Reviewed %d PR(s) this run.", reviewed)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()) or 0)
    except KeyboardInterrupt:
        sys.exit(0)
