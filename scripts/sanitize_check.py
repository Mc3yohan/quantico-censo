#!/usr/bin/env python3
"""
Clean-Room & Secret Hygiene Gate for Quantico Censo.
Checks all tracked text files for leaked credentials, private key blocks,
unofficial tile endpoints, non-UTF-8 files, and optional local extra blocklist patterns
loaded from SANITIZE_EXTRA_PATTERNS_FILE (untracked / gitignored).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "media_storage",
}

TEXT_EXTENSIONS = {
    ".py",
    ".ts",
    ".js",
    ".css",
    ".html",
    ".md",
    ".yml",
    ".yaml",
    ".toml",
    ".json",
    ".ini",
    ".conf",
    ".txt",
    ".example",
    ".webmanifest",
}

GENERIC_FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("unofficial-google-tiles", re.compile(r"mt[0-3]\.google\.com", re.I)),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private-key-header", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("hardcoded-lan-http-url", re.compile(r"https?://(?:192\.168\.|10\.\d+\.|172\.(?:1[6-9]|2\d|3[01])\.)\d+\.\d+")),
]


def _load_extra_patterns() -> list[tuple[str, re.Pattern[str]]]:
    extra_file = os.getenv("SANITIZE_EXTRA_PATTERNS_FILE", "").strip()
    if not extra_file:
        return []
    path = Path(extra_file)
    if not path.is_file():
        return []
    patterns: list[tuple[str, re.Pattern[str]]] = []
    for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        patterns.append((f"extra-rule-{idx}", re.compile(raw, re.I)))
    return patterns


def main() -> int:
    patterns = GENERIC_FORBIDDEN_PATTERNS + _load_extra_patterns()
    violations: list[str] = []
    scanned = 0

    for path in REPO_ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS and path.name not in {"Dockerfile", "LICENSE"}:
            continue

        scanned += 1
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            violations.append(f"{path.relative_to(REPO_ROOT)}: non-UTF-8 encoding detected")
            continue

        for label, regex in patterns:
            match = regex.search(content)
            if match:
                line_no = content.count("\n", 0, match.start()) + 1
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{line_no}: [{label}] matched '{match.group(0)}'"
                )

    if violations:
        print(f"[SANITIZE_CHECK] FAILED with {len(violations)} violation(s) across {scanned} files:")
        for v in violations:
            print(f"  - {v}")
        return 1

    print(f"[SANITIZE_CHECK] PASS - {scanned} files verified clean (0 hygiene or secret violations).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
