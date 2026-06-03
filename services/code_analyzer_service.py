"""Cheap, deterministic static checks.

These run BEFORE the LLM so the model gets concrete signals to riff on.
The output schema is::

    [
        {
            "file": "path/to/file.py",
            "line": 42,
            "type": "security" | "style" | "logic" | "note",
            "severity": "critical" | "warning" | "info",
            "message": "human readable description",
        },
        ...
    ]
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

from config.sources import REVIEW_RULES

log = logging.getLogger("analyzer")

MAX_LINE_LENGTH = REVIEW_RULES.get("max_line_length", 120)


def analyze_code(code_data: dict) -> list[dict]:
    """Run all enabled checks against ``code_data['files']``."""
    issues: list[dict] = []
    for file_obj in code_data.get("files", []):
        path = file_obj["path"]
        content = file_obj["content"]
        lower = path.lower()

        if lower.endswith(".py"):
            issues.extend(_check_python(path, content))
        elif lower.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")):
            issues.extend(_check_javascript(path, content))

        issues.extend(_check_common(path, content))

    log.info("Static analysis: %d issues across %d files",
             len(issues), len(code_data.get("files", [])))
    return issues


def _check_python(path: str, content: str) -> Iterable[dict]:
    if not REVIEW_RULES.get("python", True):
        return []
    out: list[dict] = []
    for i, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()

        if re.search(
            r'\b(password|passwd|api[_-]?key|secret|token)\s*=\s*[\'"][^\'"]+[\'"]',
            line,
            re.IGNORECASE,
        ) and not stripped.startswith("#"):
            out.append(_iss(path, i, "security", "critical",
                            "Possible hardcoded secret — move to env var or secret manager"))

        if "execute(" in line and "%" in line and "execute(%" not in line:
            out.append(_iss(path, i, "security", "warning",
                            "Possible SQL injection: pass parameters via cursor.execute(query, params)"))

        if re.search(r"\beval\s*\(|\bexec\s*\(", line):
            out.append(_iss(path, i, "security", "warning",
                            "eval/exec usage — confirm input is fully trusted"))

        if re.match(r"\s*except\s*:\s*(#.*)?$", line):
            out.append(_iss(path, i, "logic", "warning",
                            "Bare `except:` — catch specific exceptions or `except Exception:`"))

        if re.search(r"\bprint\s*\(", line) and not stripped.startswith("#"):
            out.append(_iss(path, i, "style", "info",
                            "`print()` in source — consider using `logging` instead"))

    return out


def _check_javascript(path: str, content: str) -> Iterable[dict]:
    if not REVIEW_RULES.get("javascript", True):
        return []
    out: list[dict] = []
    for i, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()

        if "eval(" in line:
            out.append(_iss(path, i, "security", "warning",
                            "`eval()` is a known XSS/injection vector — refactor"))

        if re.search(
            r'\b(apiKey|api_key|token|secret|password)\s*[:=]\s*[\'"][^\'"]+[\'"]',
            line,
            re.IGNORECASE,
        ) and not stripped.startswith("//"):
            out.append(_iss(path, i, "security", "critical",
                            "Possible hardcoded credential — move to env var"))

        if "console.log" in line and "// keep" not in line.lower():
            out.append(_iss(path, i, "style", "info",
                            "`console.log` left in code — gate behind debug or remove"))

        if re.search(r"\b(var)\s+\w+", line):
            out.append(_iss(path, i, "style", "info",
                            "Prefer `const`/`let` over `var`"))

        if re.search(r"==[^=]", line) and "===" not in line:
            out.append(_iss(path, i, "logic", "info",
                            "Use strict equality (`===`) to avoid type-coercion bugs"))

    return out


def _check_common(path: str, content: str) -> Iterable[dict]:
    out: list[dict] = []
    for i, line in enumerate(content.splitlines(), 1):
        if len(line) > MAX_LINE_LENGTH:
            out.append(_iss(path, i, "style", "info",
                            f"Line is {len(line)} chars (>{MAX_LINE_LENGTH})"))

        m = re.search(r"\b(TODO|FIXME|HACK|XXX|BUG)\b[: ]?(.*)", line)
        if m:
            note = m.group(2).strip() or m.group(1)
            out.append(_iss(path, i, "note", "info",
                            f"{m.group(1)}: {note[:100]}"))

    return out


def _iss(file: str, line: int, type_: str, severity: str, message: str) -> dict:
    return {
        "file": file,
        "line": line,
        "type": type_,
        "severity": severity,
        "message": message,
    }
