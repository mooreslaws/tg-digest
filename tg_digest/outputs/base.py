"""
Output adapter protocol.

An Output is anything that takes a generated digest and persists it somewhere:
a local Markdown file, a Notion page, an Obsidian vault path, stdout, a
webhook, an S3 bucket — all behind the same one-method interface.

Built-in adapters live in tg_digest.outputs.{local,notion,stdout}. Third-party
adapters can be registered via the TG_DIGEST_EXTRA_OUTPUTS env var — see
tg_digest.outputs.__init__.collect_outputs() for details.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DigestPayload:
    """Everything an Output adapter might need to render a digest."""

    # Rendered Markdown — already formatted, links inline, sections separated.
    markdown: str

    # ISO date the digest is FOR (e.g. "2026-05-18"), not the wall-clock date
    # of the run. Use this when naming files / pages / commits.
    date_str: str

    # Convenience: same as `# Telegram Digest — {date_str}` by default.
    title: str

    # 1–2 sentence digest summary suitable for previews, list views, embeds.
    # May be empty if upstream couldn't extract one.
    quick_summary: str = ""

    # Free-form metadata an adapter can use — image map for Notion, channel
    # list for frontmatter, etc. Adapters MUST tolerate unknown keys.
    extra: dict[str, Any] = field(default_factory=dict)


class Output:
    """Base class for digest output adapters.

    Subclasses override `write()`. The return value (if any) is used only for
    logging — typically a URL or file path the user can click.

    Implementations should be **idempotent**: re-running on the same date
    should overwrite / update, not create duplicates. The local Markdown
    adapter overwrites the file; the Notion adapter updates the page with the
    matching title.
    """

    # Human-readable name shown in logs, e.g. "local", "notion".
    name: str = "output"

    def write(self, payload: DigestPayload) -> str | None:
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} name={self.name!r}>"
