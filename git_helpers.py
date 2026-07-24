"""Lightweight pure helpers for commit message validation and heuristic generation.

These are kept separate from git_agent to allow importing in tests without requiring API keys.
"""
from typing import Any
import re


def validate_conventional_commit(message: str) -> dict[str, Any]:
    """Validate and suggest fixes for a Conventional Commit message.

    Same rules as in git_agent but implemented here for tests.
    """
    if message is None:
        return {"valid": False, "errors": ["Empty message"], "suggested_message": ""}

    lines = message.strip().splitlines()
    subject = lines[0] if lines else ""
    body = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""

    errors: list[str] = []
    suggested = subject

    prefixes = ("feat:", "fix:", "chore:", "docs:", "style:", "refactor:", "perf:", "test:")
    lower_sub = subject.lower()
    if not any(lower_sub.startswith(p) for p in prefixes):
        if any(k in lower_sub for k in ("fix", "bug", "error", "typo")):
            pref = "fix:"
        elif any(k in lower_sub for k in ("doc", "readme", "comment", "documentation")):
            pref = "docs:"
        elif any(k in lower_sub for k in ("refactor", "cleanup", "restructure")):
            pref = "refactor:"
        elif any(k in lower_sub for k in ("add ", "new ", "implement", "feature")):
            pref = "feat:"
        else:
            pref = "chore:"

        suggested = f"{pref} {subject}".strip()
        errors.append("Missing conventional commit prefix; suggested prefix added.")

    if len(subject) > 50:
        errors.append(f"Subject longer than 50 chars ({len(subject)})")
        sug_subject = (suggested[:47].rstrip() + "...") if len(suggested) > 50 else suggested
        suggested = sug_subject

    if body and (len(lines) > 1 and lines[1].strip() != ""):
        errors.append("Missing blank line between subject and body")
        suggested = suggested + "\n\n" + body

    valid = len(errors) == 0
    suggested_message = suggested
    if body and "\n\n" not in suggested_message:
        suggested_message = suggested_message + ("\n\n" + body if body else "")

    return {"valid": valid, "errors": errors, "suggested_message": suggested_message}


def generate_commit_message_from_diff(diff: str) -> dict[str, Any]:
    """Generate a Conventional Commit-style message from a diff string (heuristic).

    Returns a dict with commit_message and metadata.
    """
    if not diff:
        return {"status": "warning", "message": "No diff provided.", "commit_message": ""}

    added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))

    lower = diff.lower()
    if any(k in lower for k in ("fix", "bug", "error", "panic", "typo")):
        prefix = "fix:"
    elif any(k in lower for k in ("add ", "new file", "create ", "implement ", "feature")) or added > removed:
        prefix = "feat:"
    elif any(k in lower for k in ("doc", "readme", "comment", "documentation")):
        prefix = "docs:"
    elif any(k in lower for k in ("refactor", "cleanup", "restructure")):
        prefix = "refactor:"
    elif any(k in lower for k in ("format", "style", "whitespace")):
        prefix = "style:"
    else:
        prefix = "chore:"

    filenames = []
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 3:
                a = parts[2]
                if a.startswith("a/"):
                    filenames.append(a[2:])

    subject = f"{prefix} update {filenames[0] if filenames else ''}".strip()
    if len(subject) > 50:
        subject = subject[:47].rstrip() + "..."

    body_lines = []
    body_lines.append(f"Changes: +{added} / -{removed} (staged)")
    if filenames:
        body_lines.append("Files changed:")
        for f in filenames[:5]:
            body_lines.append(f"- {f}")

    body = "\n".join(body_lines)
    commit_message = subject + ("\n\n" + body if body else "")

    return {"status": "success", "commit_message": commit_message, "generated_by": "heuristic"}
