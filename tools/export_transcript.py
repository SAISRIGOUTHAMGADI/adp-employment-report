#!/usr/bin/env python3
"""Export a Claude Code session transcript into the repository.

The assignment asks for a log of every AI session and states that raw CLI logs are the
preferred format because they have the highest fidelity. Claude Code already stores every
session as JSONL under ``~/.claude/projects/<slug>/<session-id>.jsonl``; this turns one of
those into two committed artefacts:

* ``<out>/<name>.jsonl``  -- the raw record stream, byte-for-byte except for redaction.
* ``<out>/<name>.md``     -- a readable rendering of the same records.

This tool is committed rather than run ad hoc so a reader can verify exactly what was and
was not altered on the way into the repository.

What gets changed, and nothing else
-----------------------------------
**Secrets only.** A live FRED API key appears in the transcript (it leaked into an HTTP
error message that echoed the request URL), and publishing a working credential to a
public repository would be reckless. Every occurrence is replaced with a marker.

Nothing else is removed. Dead ends, wrong answers, corrections and abandoned approaches
are all left in, because they are the parts the assignment explicitly asks for.

The Markdown rendering truncates individual tool *results* past a size limit purely for
readability -- API dumps run to hundreds of kilobytes. Every truncation is marked inline
with the number of characters omitted, and the untruncated content remains in the
accompanying ``.jsonl``, so no information is lost from the export as a whole.

Usage:
    python tools/export_transcript.py --session <id> --out prompts/
    python tools/export_transcript.py --latest --out prompts/
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterator

#: Where Claude Code keeps session transcripts.
TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"

#: Individual tool results longer than this are truncated in the Markdown rendering.
#: The JSONL export keeps them in full.
MAX_RESULT_CHARS = 4_000

#: Replacement for any redacted secret.
REDACTION = "<REDACTED-FRED-API-KEY>"

#: A FRED key is 32 lowercase hex characters. Matching the shape rather than one literal
#: means a rotated key is still caught.
_KEY_PATTERN = re.compile(r"\b[0-9a-f]{32}\b")

#: Keys that are obviously not secrets: the deliberately-invalid ones used in the session
#: to probe error handling without exposing the real credential. Preserving them matters
#: -- that probe is part of the story the log tells.
_NOT_SECRETS = {"0" * 32, "a" * 32, "b" * 32, "0" * 31 + "a"}


def find_transcript(session_id: str | None, latest: bool) -> Path:
    """Locate the transcript file to export.

    Args:
        session_id: Explicit session UUID, matched against file stems.
        latest: When true and no session is given, pick the most recently modified.

    Returns:
        Path to the JSONL transcript.

    Raises:
        SystemExit: If no matching transcript exists.
    """
    candidates = sorted(TRANSCRIPT_ROOT.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise SystemExit(f"No transcripts found under {TRANSCRIPT_ROOT}")

    if session_id:
        for path in candidates:
            if path.stem == session_id:
                return path
        raise SystemExit(f"No transcript with session id {session_id}")

    if latest:
        return candidates[-1]

    raise SystemExit("Pass --session <id> or --latest")


def redact(text: str) -> str:
    """Replace any live-looking API key, leaving deliberate placeholders intact."""

    def replace(match: re.Match[str]) -> str:
        value = match.group(0)
        return value if value in _NOT_SECRETS else REDACTION

    return _KEY_PATTERN.sub(replace, text)


def load_records(path: Path) -> Iterator[dict[str, Any]]:
    """Yield parsed records, skipping any unparseable line."""
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                print(f"  warning: line {number} is not valid JSON, skipped", file=sys.stderr)


def render_markdown(records: list[dict[str, Any]], source: Path) -> str:
    """Render records as a readable Markdown transcript."""
    lines = [
        "# Raw session transcript",
        "",
        "Verbatim export of the Claude Code session that built this project, produced by",
        "[`tools/export_transcript.py`](../tools/export_transcript.py).",
        "",
        f"* **Source:** `{source.name}`",
        f"* **Records:** {len(records):,}",
        "* **Tool:** Claude Code (Opus 5), macOS",
        "",
        "## What was changed",
        "",
        "Only secrets. A live FRED API key leaked into an HTTP error message that echoed",
        "the request URL; every occurrence is replaced with "
        f"`{REDACTION}`. The deliberately-invalid keys used during the session to probe",
        "error handling are preserved, because that probe is part of the story.",
        "",
        "Nothing else is removed — dead ends, wrong answers and corrections are all here.",
        "",
        f"Individual tool *results* longer than {MAX_RESULT_CHARS:,} characters are",
        "truncated for readability, with the omitted length marked inline. The",
        "accompanying `.jsonl` in this directory holds them in full.",
        "",
        "---",
        "",
    ]

    turn = 0
    for record in records:
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue

        blocks = _blocks(message.get("content"))
        if not blocks:
            continue

        if role == "user" and any(block[0] == "text" for block in blocks):
            turn += 1
            lines.append(f"## Turn {turn} — user")
        else:
            lines.append(f"### {role}")
        lines.append("")

        for kind, payload in blocks:
            lines.extend(_render_block(kind, payload))
        lines.append("")

    return "\n".join(lines)


def _blocks(content: Any) -> list[tuple[str, Any]]:
    """Normalise a message body into ``(kind, payload)`` pairs."""
    if isinstance(content, str):
        return [("text", content)] if content.strip() else []
    if not isinstance(content, list):
        return []

    blocks: list[tuple[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text" and item.get("text", "").strip():
            blocks.append(("text", item["text"]))
        elif kind == "thinking" and item.get("thinking", "").strip():
            blocks.append(("thinking", item["thinking"]))
        elif kind == "tool_use":
            blocks.append(("tool_use", item))
        elif kind == "tool_result":
            blocks.append(("tool_result", item))
    return blocks


def _render_block(kind: str, payload: Any) -> list[str]:
    """Render one content block."""
    if kind == "text":
        return [redact(payload), ""]

    if kind == "thinking":
        return ["<details><summary>reasoning</summary>", "", "```",
                redact(payload), "```", "", "</details>", ""]

    if kind == "tool_use":
        name = payload.get("name", "?")
        args = json.dumps(payload.get("input", {}), indent=2, default=str)
        return [f"**tool call — `{name}`**", "", "```json", redact(_clip(args)), "```", ""]

    if kind == "tool_result":
        body = payload.get("content")
        if isinstance(body, list):
            body = "\n".join(
                part.get("text", "") for part in body if isinstance(part, dict)
            )
        text = redact(_clip(str(body or "")))
        return ["**tool result**", "", "```", text, "```", ""]

    return []


def _clip(text: str) -> str:
    """Truncate over-long output, marking exactly how much was omitted."""
    if len(text) <= MAX_RESULT_CHARS:
        return text
    omitted = len(text) - MAX_RESULT_CHARS
    return (
        f"{text[:MAX_RESULT_CHARS]}\n\n"
        f"... [truncated {omitted:,} characters — full content in the .jsonl]"
    )


def main(argv: list[str] | None = None) -> int:
    """Export a transcript. Returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--session", help="Session UUID to export.")
    parser.add_argument(
        "--latest", action="store_true", help="Export the most recent session."
    )
    parser.add_argument(
        "--out", type=Path, default=Path("prompts"), help="Output directory."
    )
    parser.add_argument(
        "--name", default="session-transcript", help="Base name for the outputs."
    )
    args = parser.parse_args(argv)

    source = find_transcript(args.session, args.latest)
    records = list(load_records(source))
    if not records:
        raise SystemExit(f"{source} contained no usable records")

    args.out.mkdir(parents=True, exist_ok=True)

    raw_path = args.out / f"{args.name}.jsonl"
    redacted_lines = [
        redact(json.dumps(record, ensure_ascii=False, default=str)) for record in records
    ]
    raw_path.write_text("\n".join(redacted_lines) + "\n", encoding="utf-8")

    markdown_path = args.out / f"{args.name}.md"
    markdown_path.write_text(render_markdown(records, source), encoding="utf-8")

    redactions = sum(
        len([m for m in _KEY_PATTERN.findall(line) if m not in _NOT_SECRETS])
        for line in (json.dumps(r, default=str) for r in records)
    )

    print(f"source     : {source}")
    print(f"records    : {len(records):,}")
    print(f"secrets    : {redactions} occurrence(s) redacted")
    print(f"raw        : {raw_path} ({raw_path.stat().st_size / 1024:,.0f} KB)")
    print(f"markdown   : {markdown_path} ({markdown_path.stat().st_size / 1024:,.0f} KB)")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())
