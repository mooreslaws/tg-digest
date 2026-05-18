"""
File output — write the digest Markdown to a path with a {date} placeholder.

This single adapter covers every "drop a Markdown file somewhere" workflow:

    local file:     ./reports/digest_{date}.md
    Obsidian:       ~/Vault/Inbox/digest_{date}.md
    Logseq:         ~/logseq/journals/{date}.md         (date-as-page)
    Hugo:           ./content/digest/{date}.md          (with frontmatter)
    Jekyll:         ./_posts/{date}-digest.md           (with frontmatter)
    Date-bucketed:  ./reports/{year}/{month}/{date}.md  (extra placeholders)

Optional YAML frontmatter is prepended to the file — handy for static site
generators and Obsidian dataview / templater workflows.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from .base import DigestPayload, Output

_PLACEHOLDER = re.compile(r"\{(date|year|month|day|title|iso_date)\}")


def _render_path(template: str, date_str: str, title: str) -> Path:
    """Substitute {date}, {year}, {month}, {day}, {title}, {iso_date} in a path template."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        dt = None  # title-only template; still expand what we can

    safe_title = re.sub(r"[^\w\-]+", "-", title).strip("-").lower() or "digest"
    repl = {
        "date": date_str,
        "iso_date": date_str,
        "year": f"{dt.year:04d}" if dt else "",
        "month": f"{dt.month:02d}" if dt else "",
        "day": f"{dt.day:02d}" if dt else "",
        "title": safe_title,
    }
    return Path(os.path.expanduser(_PLACEHOLDER.sub(lambda m: repl.get(m.group(1), ""), template)))


def _format_frontmatter(values: dict) -> str:
    """Format a simple dict as YAML frontmatter. Strings / numbers / bools / lists only."""
    lines = ["---"]
    for k, v in values.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif v is None:
            lines.append(f"{k}: null")
        else:
            # Quote strings containing characters that confuse YAML parsers.
            s = str(v)
            if any(c in s for c in ':#-[]{},&*!|>?@`'):
                s = '"' + s.replace('"', '\\"') + '"'
            lines.append(f"{k}: {s}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


class FileOutput(Output):
    """Write digest Markdown to a templated path.

    Parameters
    ----------
    path_template:
        Path with `{date}` (and optionally `{year}`, `{month}`, `{day}`, `{title}`)
        placeholders. `~` is expanded to the home directory.
    frontmatter:
        Optional dict prepended as YAML frontmatter. Use for Obsidian /
        Hugo / Jekyll / Eleventy workflows.
    name:
        Override the human-readable name shown in logs.
    """

    def __init__(
        self,
        path_template: str = "./reports/digest_{date}.md",
        *,
        frontmatter: dict | None = None,
        name: str = "local",
    ) -> None:
        self.path_template = path_template
        self.frontmatter = frontmatter or {}
        self.name = name

    @classmethod
    def from_env(cls, *, env_path: str, env_frontmatter: str, name: str) -> "FileOutput | None":
        """Build an instance from env vars, or return None if not configured."""
        template = (os.environ.get(env_path) or "").strip()
        if not template:
            return None
        fm_raw = (os.environ.get(env_frontmatter) or "").strip()
        fm: dict = {}
        if fm_raw:
            try:
                fm = json.loads(fm_raw)
                if not isinstance(fm, dict):
                    fm = {}
            except json.JSONDecodeError:
                fm = {}
        return cls(template, frontmatter=fm, name=name)

    def write(self, payload: DigestPayload) -> str | None:
        path = _render_path(self.path_template, payload.date_str, payload.title)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Resolve frontmatter: caller can use {date}, {title} placeholders in values.
        fm_resolved: dict = {}
        for k, v in self.frontmatter.items():
            if isinstance(v, str):
                fm_resolved[k] = (
                    v.replace("{date}", payload.date_str)
                     .replace("{title}", payload.title)
                )
            else:
                fm_resolved[k] = v

        content_parts: list[str] = []
        if fm_resolved:
            content_parts.append(_format_frontmatter(fm_resolved))
        content_parts.append(payload.markdown)
        path.write_text("\n".join(content_parts), encoding="utf-8")
        return str(path)
