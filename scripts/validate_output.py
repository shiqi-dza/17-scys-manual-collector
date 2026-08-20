#!/usr/bin/env python3
"""Validate a collected SCYS manual output directory.

This script uses only the Python standard library. It validates structure,
Plain Text block counts, link resolution, output-style invariants, and common
privacy leaks. It does not prove that transition prose is factually grounded;
that still requires comparison with the source manual.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlsplit


FORBIDDEN_FILES = {
    ".DS_Store",
    ".git",
    "__pycache__",
    "node_modules",
    ".playwright-cli",
    ".wrangler",
}

FORBIDDEN_FIELD_RE = re.compile(
    r"^-\s*(用途|何时使用|来源|blockId|前置条件|人工关口|本关顺序)\s*[：:]",
    re.MULTILINE | re.IGNORECASE,
)
RAW_QUOTE_RE = re.compile(r"^##\s*原话记录\s*$", re.MULTILINE)
H1_RE = re.compile(r"^#\s+\S", re.MULTILINE)
COMPLETION_RE = re.compile(r"^##\s+完成本关后\s*$", re.MULTILINE)
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")

SENSITIVE_PATTERNS = [
    ("signed media URL", re.compile(r"(?:Expires|Signature|OSSAccessKeyId)=")),
    ("OpenAI-style secret", re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")),
    ("GitHub-style token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("Slack-style token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{12,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "authorization header",
        re.compile(r"Authorization\s*:\s*(?:Bearer\s+)?[A-Za-z0-9._-]{12,}", re.I),
    ),
    (
        "assigned credential",
        re.compile(
            r"\b(?:api[_-]?key|token|cookie|secret|password)\b\s*[:=]\s*[\"']?[^\s\"']{8,}",
            re.I,
        ),
    ),
]

POSIX_HOME_RE = re.compile(r"/(?:Users|home)/([^/\s]+)/")
WINDOWS_HOME_RE = re.compile(r"\b[A-Za-z]:\\Users\\([^\\\s]+)\\")
SAFE_USER_PLACEHOLDERS = {
    "你的用户名",
    "用户名",
    "username",
    "user",
    "<user>",
    "${user}",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a SCYS manual Markdown output directory."
    )
    parser.add_argument("manual_dir", type=Path, help="Manual output directory")
    parser.add_argument(
        "--mode", choices=("obsidian", "markdown"), required=True, help="Link mode"
    )
    parser.add_argument(
        "--expected-plain-text",
        type=int,
        default=None,
        help="Expected number of ```text blocks",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON only"
    )
    return parser.parse_args()


def add_issue(items: list[dict[str, object]], file: Path, message: str, line: int | None = None) -> None:
    issue: dict[str, object] = {"file": str(file), "message": message}
    if line is not None:
        issue["line"] = line
    items.append(issue)


def fenced_regions(text: str) -> tuple[list[tuple[int, int, str]], list[str]]:
    """Return fenced block line ranges and fence errors."""
    lines = text.splitlines()
    regions: list[tuple[int, int, str]] = []
    errors: list[str] = []
    opening: tuple[int, int, str] | None = None

    for index, line in enumerate(lines, start=1):
        match = re.match(r"^(`{3,})([^`]*)$", line.strip())
        if not match:
            continue
        ticks, suffix = match.groups()
        if opening is None:
            opening = (index, len(ticks), suffix.strip().lower())
            continue
        start, tick_count, language = opening
        if len(ticks) >= tick_count and not suffix.strip():
            regions.append((start, index, language))
            opening = None

    if opening is not None:
        errors.append(f"unclosed code fence opened at line {opening[0]}")
    return regions, errors


def remove_fenced_content(text: str, regions: list[tuple[int, int, str]]) -> str:
    lines = text.splitlines()
    hidden: set[int] = set()
    for start, end, _ in regions:
        hidden.update(range(start, end + 1))
    return "\n".join("" if i in hidden else line for i, line in enumerate(lines, start=1))


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def check_personal_paths(text: str) -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    for regex in (POSIX_HOME_RE, WINDOWS_HOME_RE):
        for match in regex.finditer(text):
            username = match.group(1).lower()
            if username not in SAFE_USER_PLACEHOLDERS:
                hits.append((match.group(0), line_number(text, match.start())))
    return hits


def resolve_obsidian_links(
    file: Path,
    visible_text: str,
    stem_map: dict[str, list[Path]],
    relative_map: dict[str, Path],
    root: Path,
    errors: list[dict[str, object]],
) -> None:
    for match in WIKILINK_RE.finditer(visible_text):
        raw = match.group(1)
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if not target or re.match(r"^[a-z]+://", target, re.I):
            continue
        normalized = target[:-3] if target.endswith(".md") else target
        if "/" in normalized:
            key = normalized.lstrip("./")
            if key not in relative_map:
                add_issue(
                    errors,
                    file.relative_to(root),
                    f"unresolved Obsidian path link: {target}",
                    line_number(visible_text, match.start()),
                )
            continue
        candidates = stem_map.get(normalized, [])
        if len(candidates) == 0:
            add_issue(
                errors,
                file.relative_to(root),
                f"unresolved Obsidian link: {target}",
                line_number(visible_text, match.start()),
            )
        elif len(candidates) > 1:
            add_issue(
                errors,
                file.relative_to(root),
                f"ambiguous Obsidian link: {target}",
                line_number(visible_text, match.start()),
            )


def resolve_markdown_links(
    file: Path,
    visible_text: str,
    root: Path,
    errors: list[dict[str, object]],
) -> None:
    for match in MARKDOWN_LINK_RE.finditer(visible_text):
        raw = match.group(1).strip().strip("<>")
        parsed = urlsplit(raw)
        if parsed.scheme or raw.startswith("#"):
            continue
        link_path = unquote(parsed.path)
        if not link_path:
            continue
        resolved = (file.parent / link_path).resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            add_issue(
                errors,
                file.relative_to(root),
                f"relative Markdown link escapes manual directory: {raw}",
                line_number(visible_text, match.start()),
            )
            continue
        if not resolved.exists():
            add_issue(
                errors,
                file.relative_to(root),
                f"unresolved Markdown link: {raw}",
                line_number(visible_text, match.start()),
            )


def main() -> int:
    args = parse_args()
    root = args.manual_dir.expanduser().resolve()
    errors: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []

    if not root.is_dir():
        result = {
            "ok": False,
            "manual_dir": str(root),
            "errors": [{"file": ".", "message": "manual directory does not exist"}],
            "warnings": [],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    for item in root.rglob("*"):
        if item.name in FORBIDDEN_FILES:
            add_issue(errors, item.relative_to(root), "forbidden artifact in output")
        if item.is_symlink():
            add_issue(errors, item.relative_to(root), "symlink is not allowed in output")

    md_files = sorted(p for p in root.rglob("*.md") if not any(part.startswith(".") for part in p.relative_to(root).parts))
    if not md_files:
        add_issue(errors, Path("."), "no Markdown files found")

    overview_files = [p for p in md_files if p.name.startswith("00-")]
    if len(overview_files) != 1:
        add_issue(errors, Path("."), f"expected exactly one 00- overview, found {len(overview_files)}")

    stem_map: dict[str, list[Path]] = defaultdict(list)
    relative_map: dict[str, Path] = {}
    for file in md_files:
        stem_map[file.stem].append(file)
        relative_map[str(file.relative_to(root).with_suffix(""))] = file

    total_plain_text = 0
    for file in md_files:
        relative = file.relative_to(root)
        text = file.read_text(encoding="utf-8")
        regions, fence_errors = fenced_regions(text)
        for message in fence_errors:
            add_issue(errors, relative, message)
        total_plain_text += sum(1 for _, _, lang in regions if lang == "text")
        visible = remove_fenced_content(text, regions)

        if len(H1_RE.findall(visible)) != 1:
            add_issue(errors, relative, "expected exactly one H1 heading")
        if RAW_QUOTE_RE.search(visible):
            add_issue(errors, relative, "forbidden ## 原话记录 section")
        for match in FORBIDDEN_FIELD_RE.finditer(visible):
            add_issue(
                errors,
                relative,
                f"forbidden output field: {match.group(1)}",
                line_number(visible, match.start()),
            )
        if overview_files and file not in overview_files and not COMPLETION_RE.search(visible):
            add_issue(warnings, relative, "missing ## 完成本关后 section")

        lines = text.splitlines()
        for start, _, lang in regions:
            if lang != "text":
                continue
            previous = start - 2
            while previous >= 0 and not lines[previous].strip():
                previous -= 1
            if previous < 0 or lines[previous].lstrip().startswith("#"):
                add_issue(
                    errors,
                    relative,
                    "Plain Text block is missing a transition paragraph",
                    start,
                )

        prose_without_code = visible.split("\n## 原话记录\n", 1)[0]
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", prose_without_code) if p.strip()]
        for paragraph in paragraphs:
            if paragraph.startswith(("#", ">", "- ")) or re.match(r"^\d+\.\s", paragraph):
                continue
            if len(paragraph) > 220:
                add_issue(warnings, relative, f"long prose paragraph ({len(paragraph)} chars)")

        seen_sensitive: set[tuple[str, int]] = set()
        for label, regex in SENSITIVE_PATTERNS:
            for match in regex.finditer(text):
                matched_line = line_number(text, match.start())
                key = (label, matched_line)
                if key in seen_sensitive:
                    continue
                seen_sensitive.add(key)
                add_issue(
                    errors,
                    relative,
                    f"possible sensitive data: {label}",
                    matched_line,
                )
        for matched_path, matched_line in check_personal_paths(text):
            add_issue(
                errors,
                relative,
                f"possible personal absolute path: {matched_path}",
                matched_line,
            )

        if args.mode == "obsidian":
            resolve_obsidian_links(file, visible, stem_map, relative_map, root, errors)
        else:
            resolve_markdown_links(file, visible, root, errors)

    if args.expected_plain_text is not None and total_plain_text != args.expected_plain_text:
        add_issue(
            errors,
            Path("."),
            f"Plain Text count mismatch: expected {args.expected_plain_text}, found {total_plain_text}",
        )

    result = {
        "ok": not errors,
        "manual_dir": str(root),
        "mode": args.mode,
        "markdown_files": len(md_files),
        "plain_text_blocks": total_plain_text,
        "errors": errors,
        "warnings": warnings,
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("PASS" if result["ok"] else "FAIL")
        print(f"manual_dir: {result['manual_dir']}")
        print(f"markdown_files: {result['markdown_files']}")
        print(f"plain_text_blocks: {result['plain_text_blocks']}")
        for issue in errors:
            location = f"{issue['file']}:{issue.get('line', '')}".rstrip(":")
            print(f"ERROR {location} {issue['message']}")
        for issue in warnings:
            location = f"{issue['file']}:{issue.get('line', '')}".rstrip(":")
            print(f"WARN  {location} {issue['message']}")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
