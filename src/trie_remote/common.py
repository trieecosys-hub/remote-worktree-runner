"""Shared validation and filesystem safety helpers."""

from __future__ import annotations

from pathlib import Path
import re


IDENTIFIER_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def validate_identifier(value: str, label: str) -> str:
    """Return a safe identifier or raise a descriptive error."""
    if IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"invalid {label}: expected lowercase letters, digits, and hyphens",
        )
    return value


def ensure_below(root: Path, candidate: Path) -> Path:
    """Resolve a path and ensure it is a strict descendant of root."""
    resolved_root = root.expanduser().resolve()
    resolved_candidate = candidate.expanduser().resolve()
    if resolved_candidate == resolved_root or not resolved_candidate.is_relative_to(
        resolved_root,
    ):
        raise ValueError(f"path escapes {resolved_root}: {resolved_candidate}")
    return resolved_candidate

