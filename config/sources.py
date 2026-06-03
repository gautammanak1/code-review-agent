"""Static configuration: which file types to review, which checks to run,
and an optional list of repos for batch / scheduled reviews.

Anything tunable lives here so callers don't need to dig into service code.
"""

from __future__ import annotations

LANGUAGES_TO_REVIEW: list[str] = [
    ".py",
    ".js", ".jsx", ".mjs", ".cjs",
    ".ts", ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".rb",
    ".cpp", ".cc", ".cxx",
    ".c", ".h", ".hpp",
    ".cs",
    ".php",
    ".swift",
]

REVIEW_RULES: dict = {
    "python":         True,
    "javascript":     True,

    "max_line_length": 120,
    "max_files":       20,
    "max_issues":      50,

    "severity_min": "info",
}

TRACKED_REPOS: list[str] = [
]
