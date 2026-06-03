"""Pydantic models used by the AI agent and its tools.

The shape mirrors the instacart-agent convention:

- ``UserContext`` is a frozen dataclass passed to ``create_agent`` via
  ``context_schema`` and reachable inside every tool as ``runtime.context``.
- All tool inputs/outputs are ``BaseModel`` with ``extra="forbid"`` so the
  LLM cannot make up extra fields.
- ``Response`` is the final structured output returned by the agent on
  every turn (enforced via ``ToolStrategy(schema=Response)``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class UserContext:
    """Per-session user/runtime context."""

    user_id: str
    session_id: str

    asi_one_api_key: str
    github_token: str

    dry_run: bool = False


class ChangedFile(BaseModel):
    """One file changed in a PR (returned from ``fetch_pr_metadata``)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"required": ["path", "status", "additions", "deletions"]},
    )
    path: str = Field(description="Path of the file relative to repo root")
    status: str = Field(description="GitHub file status: added, modified, removed, renamed")
    additions: int = Field(description="Lines added in this PR")
    deletions: int = Field(description="Lines deleted in this PR")


class PRMetadata(BaseModel):
    """Result of ``fetch_pr_metadata``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "required": [
                "repo",
                "pr_number",
                "title",
                "body",
                "head_sha",
                "head_branch",
                "base_branch",
                "url",
                "changed_files",
                "error",
            ],
        },
    )
    repo: str = Field(description="owner/repo")
    pr_number: int
    title: str
    body: str = Field(description="PR description (may be empty)")
    head_sha: str = Field(description="Commit SHA at the PR head — ALWAYS pass this as `ref` to get_file_content and as `head_sha` to post_pr_comment")
    head_branch: str
    base_branch: str
    url: str = Field(description="Full https://github.com/... URL of the PR")
    changed_files: list[ChangedFile] = Field(
        default_factory=list,
        description="Reviewable files changed in this PR (excludes lockfiles, binaries)",
    )
    error: str | None = Field(
        default=None,
        description="If non-null, the fetch failed; everything else is empty/default",
    )


class FileContent(BaseModel):
    """Result of ``get_file_content``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"required": ["path", "content", "truncated", "lines", "error"]},
    )
    path: str
    content: str = Field(description="Raw file text. May be truncated if very large. Empty when error is set.")
    truncated: bool = Field(description="True if the file was cut at the size cap")
    lines: int = Field(description="Total number of lines in the (possibly truncated) content")
    error: str | None = Field(
        default=None,
        description=(
            "If non-null, the file could not be read. Common causes: "
            "(a) the file is new in this PR and you forgot to pass ref=head_sha; "
            "(b) the file was deleted; (c) network/auth error. Skip this file "
            "and continue reviewing the others."
        ),
    )


class RepoFileList(BaseModel):
    """Result of ``fetch_repo_files``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"required": ["repo", "branch", "head_sha", "files", "error"]},
    )
    repo: str
    branch: str
    head_sha: str
    files: list[str] = Field(
        default_factory=list,
        description="Paths of reviewable files (capped at 50)",
    )
    error: str | None = Field(
        default=None,
        description="If non-null, the listing failed; files will be empty",
    )


class Finding(BaseModel):
    """One static-analysis signal — output of ``analyze_file``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"required": ["file", "line", "type", "severity", "message"]},
    )
    file: str
    line: int = Field(description="1-indexed line number")
    type: Literal["security", "style", "logic", "note"] = Field(
        description="Category of the finding",
    )
    severity: Literal["critical", "warning", "info"]
    message: str


class FindingList(BaseModel):
    """List of findings returned by ``analyze_file``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"required": ["file", "findings"]},
    )
    file: str
    findings: list[Finding] = Field(default_factory=list)


class CommentPostResult(BaseModel):
    """Result of ``post_pr_comment``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"required": ["posted", "kind", "url", "message"]},
    )
    posted: bool = Field(description="True if the comment hit GitHub")
    kind: Literal["summary", "line", "fallback", "skipped", "error"] = Field(
        description=(
            "summary=top-level comment; line=line-anchored review comment; "
            "fallback=line-anchor failed and we posted as summary instead; "
            "skipped=dry_run or no token; error=request failed"
        ),
    )
    url: str | None = Field(default=None, description="URL of the posted comment if available")
    message: str = Field(default="", description="Short status string")


class HistoryEntry(BaseModel):
    """One row of the persisted review history."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"required": ["repo", "pr", "date", "issues_count"]},
    )
    repo: str
    pr: int | None
    date: str = Field(description="ISO 8601 timestamp")
    issues_count: int
    posted_count: int | None = Field(default=None)


class HistoryList(BaseModel):
    """Result of ``get_review_history``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"required": ["entries", "total"]},
    )
    entries: list[HistoryEntry] = Field(default_factory=list)
    total: int = Field(description="Total number of records on disk (entries may be a slice)")


class Response(BaseModel):
    """Final structured response returned by the agent on every turn.

    Enforced via ``ToolStrategy(schema=Response)`` in :func:`ai.ai.AI.setup_agent`.
    The chat protocol surfaces ``text`` to the user and uses the optional
    fields for structured callers (CI, dashboards).
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "required": ["text", "pr_url", "issues_count", "posted_comments"],
        },
    )
    text: str = Field(
        description="User-facing markdown reply. Keep under ~1500 words.",
    )
    pr_url: str | None = Field(
        default=None,
        description="https://github.com/owner/repo/pull/N if a PR was reviewed",
    )
    issues_count: int | None = Field(
        default=None,
        description="Number of static-analysis issues observed during the review",
    )
    posted_comments: int | None = Field(
        default=None,
        description="Number of comments actually posted (0 in dry-run)",
    )
