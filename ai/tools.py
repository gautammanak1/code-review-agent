"""LangChain tools for the Code Review Agent.

Each tool is an ``@tool`` decorated async function. They:

- Accept a ``runtime: ToolRuntime[UserContext, dict[str, Any]]`` parameter so
  they can read ``runtime.context`` (the per-session ``UserContext``) and emit
  progress strings via ``runtime.stream_writer``.
- Use the GitHub REST API (via ``httpx``) for repo / PR access.
- Reuse the existing ``services.code_analyzer_service`` and
  ``services.review_tracker_service`` for static analysis and history.
- Return Pydantic models from :mod:`ai.models` so the LLM sees a strict
  schema.

No tool ever touches the GitHub merge endpoints; the only writes go to
``/issues/:n/comments`` and ``/pulls/:n/comments``.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

import httpx
from langchain.tools import (  # pyright: ignore[reportUnknownVariableType]
    ToolRuntime,
    tool,
)

from ai.models import (
    ChangedFile,
    CommentPostResult,
    FileContent,
    Finding,
    FindingList,
    HistoryEntry,
    HistoryList,
    PRMetadata,
    RepoFileList,
    UserContext,
)
from config.sources import LANGUAGES_TO_REVIEW
from services.code_analyzer_service import analyze_code
from services.review_tracker_service import (
    get_review_history,
    save_review_record,
)

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
MAX_FILE_BYTES = 200_000
MAX_FILES_LISTED = 50

_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=15.0, pool=10.0)


def _gh_headers(token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "code-review-agent",
    }
    t = (token or "").strip()
    if t:
        headers["Authorization"] = f"Bearer {t}"
    return headers


def _split_repo(repo: str) -> tuple[str, str]:
    s = (repo or "").strip()
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if s.endswith(".git"):
        s = s[: -len(".git")]
    s = s.strip("/")
    parts = s.split("/")
    if len(parts) < 2:
        raise ValueError(f"Bad repo {repo!r}: expected 'owner/repo'")
    return parts[0], parts[1]


# ---------------------------------------------------------------------------
# fetch_pr_metadata
# ---------------------------------------------------------------------------


@tool
async def fetch_pr_metadata(  # noqa: D417
    repo: str,
    pr_number: int,
    runtime: ToolRuntime[UserContext, dict[str, Any]],
) -> PRMetadata:
    """Fetch the metadata of a GitHub pull request.

    Returns the PR title, body, head SHA, base/head branch, and the list of
    *reviewable* changed files (lockfiles, binaries, and very large diffs are
    filtered out so you don't waste tokens on them).

    Args:
        repo: GitHub repo as ``owner/repo`` or full URL.
        pr_number: The PR number.

    Returns:
        PRMetadata: Structured PR info plus the changed-file list.
    """
    write = runtime.stream_writer
    token = runtime.context.github_token

    write(f"Fetching PR #{pr_number} from {repo}...")
    try:
        owner, repo_name = _split_repo(repo)
    except ValueError as exc:
        return PRMetadata(
            repo=repo, pr_number=pr_number, title="", body="",
            head_sha="", head_branch="", base_branch="", url="",
            changed_files=[], error=f"Bad repo argument: {exc}",
        )

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_gh_headers(token)) as client:
            pr_resp = await client.get(
                f"{GITHUB_API}/repos/{owner}/{repo_name}/pulls/{pr_number}"
            )
            if pr_resp.status_code >= 400:
                return PRMetadata(
                    repo=f"{owner}/{repo_name}", pr_number=pr_number, title="", body="",
                    head_sha="", head_branch="", base_branch="",
                    url=f"https://github.com/{owner}/{repo_name}/pull/{pr_number}",
                    changed_files=[],
                    error=f"GitHub {pr_resp.status_code}: {pr_resp.text[:200]}",
                )
            pr = pr_resp.json()

            files: list[ChangedFile] = []
            page = 1
            extensions = set(LANGUAGES_TO_REVIEW)
            skip_dirs = {"node_modules/", "vendor/", "dist/", "build/", ".venv/", "__pycache__/"}

            while page <= 5 and len(files) < 30:
                files_resp = await client.get(
                    f"{GITHUB_API}/repos/{owner}/{repo_name}/pulls/{pr_number}/files",
                    params={"per_page": 100, "page": page},
                )
                if files_resp.status_code >= 400:
                    logger.warning(
                        "PR files listing failed (%s): %s",
                        files_resp.status_code, files_resp.text[:200],
                    )
                    break
                batch = files_resp.json() or []
                if not batch:
                    break
                for entry in batch:
                    path = entry.get("filename") or ""
                    status = entry.get("status") or ""
                    if not path or status == "removed":
                        continue
                    if any(d in path for d in skip_dirs):
                        continue
                    if Path(path).suffix not in extensions:
                        continue
                    files.append(
                        ChangedFile(
                            path=path,
                            status=status,
                            additions=int(entry.get("additions") or 0),
                            deletions=int(entry.get("deletions") or 0),
                        )
                    )
                    if len(files) >= 30:
                        break
                if len(batch) < 100:
                    break
                page += 1
    except httpx.HTTPError as exc:
        logger.exception("fetch_pr_metadata network error: %s", exc)
        return PRMetadata(
            repo=f"{owner}/{repo_name}", pr_number=pr_number, title="", body="",
            head_sha="", head_branch="", base_branch="",
            url=f"https://github.com/{owner}/{repo_name}/pull/{pr_number}",
            changed_files=[], error=f"Network error: {exc}",
        )

    write(f"PR has {len(files)} reviewable file(s).")
    return PRMetadata(
        repo=f"{owner}/{repo_name}",
        pr_number=pr_number,
        title=pr.get("title") or "",
        body=(pr.get("body") or "")[:4000],
        head_sha=pr.get("head", {}).get("sha") or "",
        head_branch=pr.get("head", {}).get("ref") or "",
        base_branch=pr.get("base", {}).get("ref") or "",
        url=pr.get("html_url") or f"https://github.com/{owner}/{repo_name}/pull/{pr_number}",
        changed_files=files,
        error=None,
    )


# ---------------------------------------------------------------------------
# fetch_repo_files (when reviewing a branch, not a PR)
# ---------------------------------------------------------------------------


@tool
async def fetch_repo_files(  # noqa: D417
    repo: str,
    runtime: ToolRuntime[UserContext, dict[str, Any]],
    branch: str = "",
) -> RepoFileList:
    """List reviewable files on a branch of a GitHub repo.

    Use this only when there is no PR (e.g. the user said "review the main
    branch of foo/bar"). For PR reviews, use ``fetch_pr_metadata`` which
    already includes the changed files.

    Args:
        repo: GitHub repo as ``owner/repo`` or full URL.
        branch: Branch name. Empty string means: use the default branch.

    Returns:
        RepoFileList: Branch info and a capped list of reviewable file paths.
    """
    write = runtime.stream_writer
    token = runtime.context.github_token
    try:
        owner, repo_name = _split_repo(repo)
    except ValueError as exc:
        return RepoFileList(
            repo=repo, branch=branch or "", head_sha="", files=[],
            error=f"Bad repo argument: {exc}",
        )

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_gh_headers(token)) as client:
            if not branch:
                repo_resp = await client.get(f"{GITHUB_API}/repos/{owner}/{repo_name}")
                if repo_resp.status_code >= 400:
                    return RepoFileList(
                        repo=f"{owner}/{repo_name}", branch="", head_sha="", files=[],
                        error=f"GitHub {repo_resp.status_code} on repo lookup: {repo_resp.text[:200]}",
                    )
                branch = repo_resp.json().get("default_branch") or "main"

            write(f"Listing files on {owner}/{repo_name}@{branch}...")
            ref_resp = await client.get(
                f"{GITHUB_API}/repos/{owner}/{repo_name}/git/refs/heads/{branch}"
            )
            if ref_resp.status_code >= 400:
                return RepoFileList(
                    repo=f"{owner}/{repo_name}", branch=branch, head_sha="", files=[],
                    error=f"GitHub {ref_resp.status_code} on ref lookup: {ref_resp.text[:200]}",
                )
            head_sha = ref_resp.json().get("object", {}).get("sha") or ""

            tree_resp = await client.get(
                f"{GITHUB_API}/repos/{owner}/{repo_name}/git/trees/{head_sha}",
                params={"recursive": "1"},
            )
            if tree_resp.status_code >= 400:
                return RepoFileList(
                    repo=f"{owner}/{repo_name}", branch=branch, head_sha=head_sha, files=[],
                    error=f"GitHub {tree_resp.status_code} on tree lookup: {tree_resp.text[:200]}",
                )
            tree = tree_resp.json().get("tree") or []
    except httpx.HTTPError as exc:
        logger.exception("fetch_repo_files network error: %s", exc)
        return RepoFileList(
            repo=f"{owner}/{repo_name}", branch=branch or "", head_sha="", files=[],
            error=f"Network error: {exc}",
        )

    extensions = set(LANGUAGES_TO_REVIEW)
    skip_dirs = {"node_modules/", "vendor/", "dist/", "build/", ".venv/", "__pycache__/"}
    files: list[str] = []
    for entry in tree:
        if entry.get("type") != "blob":
            continue
        path = entry.get("path") or ""
        if not path or any(d in path for d in skip_dirs):
            continue
        if Path(path).suffix not in extensions:
            continue
        files.append(path)
        if len(files) >= MAX_FILES_LISTED:
            break

    write(f"Found {len(files)} reviewable file(s).")
    return RepoFileList(
        repo=f"{owner}/{repo_name}",
        branch=branch,
        head_sha=head_sha,
        files=files,
        error=None,
    )


# ---------------------------------------------------------------------------
# get_file_content
# ---------------------------------------------------------------------------


@tool
async def get_file_content(  # noqa: D417
    repo: str,
    path: str,
    runtime: ToolRuntime[UserContext, dict[str, Any]],
    ref: str = "",
) -> FileContent:
    """Read one file from a GitHub repo at a specific ref.

    The returned content is capped at ~200 KB; longer files come back with
    ``truncated=True``. Read only the files you actually plan to comment on.

    Args:
        repo: GitHub repo as ``owner/repo`` or full URL.
        path: File path relative to repo root.
        ref: Branch, tag, or commit SHA. Empty = default branch.

    Returns:
        FileContent: The file's text plus its line count.
    """
    write = runtime.stream_writer
    token = runtime.context.github_token
    try:
        owner, repo_name = _split_repo(repo)
    except ValueError as exc:
        return FileContent(
            path=path, content="", truncated=False, lines=0,
            error=f"Bad repo argument: {exc}",
        )

    write(f"Reading {path}...")
    params = {"ref": ref} if ref else None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_gh_headers(token)) as client:
            resp = await client.get(
                f"{GITHUB_API}/repos/{owner}/{repo_name}/contents/{path}",
                params=params,
            )
    except httpx.HTTPError as exc:
        logger.warning("get_file_content network error for %s: %s", path, exc)
        return FileContent(
            path=path, content="", truncated=False, lines=0,
            error=f"Network error: {exc}",
        )

    if resp.status_code == 404:
        hint = (
            " — did you forget to pass `ref=head_sha`? "
            "New files added in a PR don't exist on the default branch."
            if not ref else ""
        )
        return FileContent(
            path=path, content="", truncated=False, lines=0,
            error=f"GitHub 404: file not found at ref={ref or '(default)'}{hint}",
        )
    if resp.status_code >= 400:
        return FileContent(
            path=path, content="", truncated=False, lines=0,
            error=f"GitHub {resp.status_code}: {resp.text[:200]}",
        )

    try:
        data = resp.json()
    except Exception as exc:
        return FileContent(
            path=path, content="", truncated=False, lines=0,
            error=f"Bad JSON from GitHub: {exc}",
        )

    if isinstance(data, list):
        return FileContent(
            path=path, content="", truncated=False, lines=0,
            error="Path points to a directory, not a file",
        )

    encoding = data.get("encoding")
    raw = data.get("content") or ""
    if encoding == "base64":
        try:
            blob = base64.b64decode(raw, validate=False)
        except Exception:
            blob = b""
    else:
        blob = raw.encode("utf-8", errors="replace")

    truncated = len(blob) > MAX_FILE_BYTES
    if truncated:
        blob = blob[:MAX_FILE_BYTES]
    text = blob.decode("utf-8", errors="replace")
    lines = text.count("\n") + 1 if text else 0

    return FileContent(
        path=path, content=text, truncated=truncated, lines=lines, error=None,
    )


# ---------------------------------------------------------------------------
# analyze_file
# ---------------------------------------------------------------------------


@tool
async def analyze_file(  # noqa: D417
    path: str,
    content: str,
    runtime: ToolRuntime[UserContext, dict[str, Any]],
) -> FindingList:
    """Run cheap static-analysis checks against a single file.

    Detects hardcoded secrets, bare ``except``, ``eval``, ``console.log``,
    long lines, and ``TODO/FIXME``. Each finding has a severity. Treat
    findings as *signals* — confirm them yourself before commenting.

    Args:
        path: File path (used to pick the right linters by extension).
        content: File text.

    Returns:
        FindingList: All findings for the file.
    """
    write = runtime.stream_writer
    write(f"Analyzing {path}...")

    raw = analyze_code({"files": [{"path": path, "content": content}]})
    findings = [
        Finding(
            file=item["file"],
            line=int(item["line"]),
            type=item["type"],
            severity=item["severity"],
            message=item["message"],
        )
        for item in raw
    ]
    return FindingList(file=path, findings=findings)


# ---------------------------------------------------------------------------
# post_pr_comment
# ---------------------------------------------------------------------------


@tool
async def post_pr_comment(  # noqa: D417, PLR0913
    repo: str,
    pr_number: int,
    body: str,
    runtime: ToolRuntime[UserContext, dict[str, Any]],
    file: str = "",
    line: int = 0,
    head_sha: str = "",
) -> CommentPostResult:
    """Post a Markdown comment on a GitHub pull request.

    - If ``file`` AND ``line`` AND ``head_sha`` are all set, tries a line-
      anchored review comment first. On failure (e.g. that line isn't part
      of the diff), falls back to a top-level comment so feedback is
      never lost.
    - Otherwise posts a normal top-level issue comment.

    Respects ``UserContext.dry_run`` — when true, returns ``kind="skipped"``
    without calling GitHub.

    This tool ONLY writes comments. It cannot merge, approve, or close
    anything.

    Args:
        repo: GitHub repo as ``owner/repo`` or full URL.
        pr_number: PR number.
        body: Markdown comment body.
        file: Optional file path for a line-anchored comment.
        line: Optional 1-indexed line number for a line-anchored comment.
        head_sha: Optional commit SHA at PR head (required for line comments).

    Returns:
        CommentPostResult: posted/kind/url/message.
    """
    write = runtime.stream_writer
    ctx = runtime.context
    token = ctx.github_token

    if ctx.dry_run:
        write("dry-run: would have posted comment")
        return CommentPostResult(
            posted=False, kind="skipped", url=None,
            message="dry_run is true; no comment posted",
        )
    if not token:
        write("no GITHUB_TOKEN; skipping post")
        return CommentPostResult(
            posted=False, kind="skipped", url=None,
            message="GITHUB_TOKEN is not set",
        )

    owner, repo_name = _split_repo(repo)
    write(f"Posting comment to {owner}/{repo_name}#{pr_number}...")

    want_line = bool(file) and line > 0 and bool(head_sha)
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_gh_headers(token)) as client:
        if want_line:
            line_resp = await client.post(
                f"{GITHUB_API}/repos/{owner}/{repo_name}/pulls/{pr_number}/comments",
                json={
                    "body": body,
                    "commit_id": head_sha,
                    "path": file,
                    "line": line,
                    "side": "RIGHT",
                },
            )
            if line_resp.status_code < 400:
                data = line_resp.json()
                return CommentPostResult(
                    posted=True, kind="line",
                    url=data.get("html_url"),
                    message=f"Line comment on {file}:{line}",
                )
            logger.warning(
                "Line comment failed (%s) on %s:%d: %s — falling back to issue comment",
                line_resp.status_code, file, line, line_resp.text[:300],
            )
            body = (
                f"_(could not anchor to `{file}:{line}` — see below)_\n\n" + body
            )

        issue_resp = await client.post(
            f"{GITHUB_API}/repos/{owner}/{repo_name}/issues/{pr_number}/comments",
            json={"body": body},
        )
        if issue_resp.status_code >= 400:
            err_body = issue_resp.text[:400]
            hint = ""
            if issue_resp.status_code == 403:
                hint = (
                    " | Likely cause: fine-grained PAT lacks 'Pull requests: Write' "
                    "or this repo is not in its 'Repository access' list. Use a "
                    "classic PAT (ghp_...) with `repo` scope, or add this repo + "
                    "permissions to the fine-grained token at "
                    "https://github.com/settings/tokens"
                )
            elif issue_resp.status_code == 404:
                hint = " | Repo or PR not found, or the token user has no read access"
            elif issue_resp.status_code == 422:
                hint = " | GitHub rejected the comment body (probably too long)"
            full_msg = f"GitHub {issue_resp.status_code}: {err_body}{hint}"
            logger.error("post_pr_comment failed: %s", full_msg)
            write(f"GitHub {issue_resp.status_code} when posting{hint}")
            return CommentPostResult(
                posted=False, kind="error", url=None,
                message=full_msg,
            )
        data = issue_resp.json()
        kind = "fallback" if want_line else "summary"
        return CommentPostResult(
            posted=True, kind=kind,
            url=data.get("html_url"),
            message="Posted",
        )


# ---------------------------------------------------------------------------
# save_review_record / get_review_history
# ---------------------------------------------------------------------------


@tool
async def record_review(  # noqa: D417
    repo: str,
    summary: str,
    issues_count: int,
    runtime: ToolRuntime[UserContext, dict[str, Any]],
    pr_number: int = 0,
    posted_count: int = 0,
) -> str:
    """Append this review to local history (``review_history.json``).

    Call this exactly once at the end of every review.

    Args:
        repo: ``owner/repo`` of the reviewed code.
        summary: The review markdown you just produced.
        issues_count: Number of static-analysis issues you observed.
        pr_number: PR number, or 0 for a branch-only review.
        posted_count: How many comments you actually posted (0 in dry-run).

    Returns:
        str: A short confirmation string.
    """
    runtime.stream_writer("Saving review to history...")
    pr_value: int | None = pr_number if pr_number > 0 else None
    save_review_record(
        repo, pr_value, summary,
        issues_count=issues_count,
        posted={"posted": posted_count, "failed": 0, "skipped": 0},
    )
    return f"Recorded review of {repo}{f' PR#{pr_number}' if pr_value else ''}"


@tool
async def list_review_history(  # noqa: D417
    runtime: ToolRuntime[UserContext, dict[str, Any]],
    limit: int = 10,
) -> HistoryList:
    """Return the most recent reviews from local history.

    Args:
        limit: Max number of entries to return (most recent first).

    Returns:
        HistoryList: Recent entries plus the total count on disk.
    """
    runtime.stream_writer("Loading review history...")
    raw = get_review_history()
    sliced = list(reversed(raw))[: max(1, min(int(limit or 10), 50))]
    entries = [
        HistoryEntry(
            repo=str(r.get("repo") or ""),
            pr=r.get("pr"),
            date=str(r.get("date") or ""),
            issues_count=int(r.get("issues_count") or 0),
            posted_count=(r.get("posted") or {}).get("posted") if isinstance(r.get("posted"), dict) else None,
        )
        for r in sliced
    ]
    return HistoryList(entries=entries, total=len(raw))


tools = [
    fetch_pr_metadata,
    fetch_repo_files,
    get_file_content,
    analyze_file,
    post_pr_comment,
    record_review,
    list_review_history,
]
