"""
Notion output adapter.

Renders the digest Markdown into Notion blocks and creates / updates a page
in a database. Idempotent: re-running on the same date updates the existing
page rather than creating a duplicate.

Property names are configurable so OSS users can match their own DB schema:
the only mandatory property is the title column.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Optional

from .base import DigestPayload, Output

log = logging.getLogger("tg_digest.outputs.notion")

NOTION_BASE = "https://api.notion.com/v1"
MAX_BLOCKS_PER_REQ = 90
_URL_RE = re.compile(r"(https?://[^\s)\]]+)")


def _normalize_post_url(url: str) -> str:
    """Canonical form of a t.me post URL for image-map matching."""
    if not url:
        return ""
    u = url.strip().rstrip(").,;]")
    u = u.replace("http://", "https://")
    u = u.replace("https://telegram.me/", "https://t.me/")
    u = u.replace("https://www.t.me/", "https://t.me/")
    if "?" in u:
        u = u.split("?", 1)[0]
    if "#" in u:
        u = u.split("#", 1)[0]
    return u.rstrip("/")


# ── Markdown → Notion blocks ───────────────────────────────────────────────────

def _rich_text(text: str) -> list[dict]:
    chunks: list[dict] = []
    for part in _URL_RE.split(text):
        if not part:
            continue
        if _URL_RE.fullmatch(part):
            url = part
            while url:
                chunks.append({
                    "type": "text",
                    "text": {"content": url[:2000], "link": {"url": url[:2000]}},
                    "annotations": {"underline": True, "color": "blue"},
                })
                url = url[2000:]
        else:
            while part:
                chunks.append({"type": "text", "text": {"content": part[:2000]}})
                part = part[2000:]
    return chunks or [{"type": "text", "text": {"content": ""}}]


def _heading_block(level: int, text: str) -> dict:
    htype = f"heading_{level}"
    return {"object": "block", "type": htype, htype: {"rich_text": _rich_text(text)}}


def _paragraph_block(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich_text(text)}}


def _bullet_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _rich_text(text)},
    }


def _divider_block() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _image_block(url: str) -> dict:
    return {
        "object": "block",
        "type": "image",
        "image": {"type": "external", "external": {"url": url}},
    }


def markdown_to_blocks(markdown: str) -> list[dict]:
    """Lightweight Markdown → Notion block converter.

    Handles: # / ## / ### headings, bullet lists, `---` dividers, and paragraphs.
    Inline link syntax `[text](url)` is preserved in rich_text spans (handled
    upstream of this function — the prompt already emits Markdown links).
    """
    blocks: list[dict] = []
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if line.startswith("# "):
            blocks.append(_heading_block(1, line[2:].strip()))
        elif line.startswith("## "):
            blocks.append(_heading_block(2, line[3:].strip()))
        elif line.startswith("### "):
            blocks.append(_heading_block(3, line[4:].strip()))
        elif line.startswith("- "):
            blocks.append(_bullet_block(line[2:].strip()))
        elif line.strip() == "---":
            blocks.append(_divider_block())
        else:
            blocks.append(_paragraph_block(line))
    return blocks


def _block_rich_text(block: dict) -> list[dict]:
    """Extract rich_text from block types that can carry links."""
    t = block.get("type", "")
    if t in (
        "paragraph",
        "heading_1",
        "heading_2",
        "heading_3",
        "bulleted_list_item",
        "numbered_list_item",
        "quote",
        "callout",
    ):
        inner = block.get(t, {})
        return inner.get("rich_text", []) or []
    return []


def inject_images(blocks: list[dict], image_map: dict[str, str]) -> list[dict]:
    """For each block that contains a post URL we have an image for, insert
    the image block right after it."""
    if not image_map:
        return blocks

    enriched: list[dict] = []
    for block in blocks:
        enriched.append(block)
        rich_text_list = _block_rich_text(block)
        matched: Optional[str] = None

        for rt in rich_text_list:
            link = rt.get("text", {}).get("link", {}) or {}
            link_url = (link.get("url", "") or "").strip()
            if link_url:
                norm = _normalize_post_url(link_url)
                if norm in image_map:
                    matched = image_map[norm]
                    break
            content = rt.get("text", {}).get("content", "") or ""
            for m in _URL_RE.finditer(content):
                norm = _normalize_post_url(m.group(1))
                if norm in image_map:
                    matched = image_map[norm]
                    break
            if matched:
                break

        if matched:
            enriched.append(_image_block(matched))

    return enriched


# ── Notion API client ──────────────────────────────────────────────────────────

class NotionOutput(Output):
    """Create / update a Notion page per digest date.

    Properties written by default: just the title (every DB has one).
    Set ``summary_prop`` / ``topics_relation_prop`` to also write extras.

    Parameters
    ----------
    token:
        Notion integration token.
    notes_db_id:
        Database ID where digest pages are created.
    title_prop:
        Name of the title column in the DB. Default: "Title".
    summary_prop:
        Optional name of a rich_text column for the one-line summary.
    topics_relation_prop:
        Optional name of a relation column to a Topics DB (for --detect-topics).
    """

    name = "notion"

    def __init__(
        self,
        *,
        token: str,
        notes_db_id: str,
        title_prop: str = "Title",
        summary_prop: str = "",
        topics_relation_prop: str = "",
    ) -> None:
        self.token = token
        self.notes_db_id = notes_db_id
        self.title_prop = title_prop or "Title"
        self.summary_prop = summary_prop
        self.topics_relation_prop = topics_relation_prop

    @classmethod
    def from_env(cls) -> "NotionOutput | None":
        token = (
            os.environ.get("NOTION_TOKEN")
            or os.environ.get("NOTION_WRITER_TOKEN")
            or os.environ.get("NOTION_READER_TOKEN", "")
            or ""
        ).strip()
        db = (os.environ.get("NOTION_NOTES_DB_ID", "") or "").strip()
        if not (token and db):
            return None
        return cls(
            token=token,
            notes_db_id=db,
            title_prop=(os.environ.get("NOTION_TITLE_PROP") or "Title").strip(),
            summary_prop=(os.environ.get("NOTION_SUMMARY_PROP") or "").strip(),
            topics_relation_prop=(os.environ.get("NOTION_TOPICS_RELATION_PROP") or "").strip(),
        )

    # ── HTTP helpers ───────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        url = f"{NOTION_BASE}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                if not raw:
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            raise RuntimeError(f"Notion HTTP {e.code} ({method} {path}): {detail}") from e

    # ── Page lifecycle ─────────────────────────────────────────────────────────

    def _find_existing_page_id(self, title: str) -> Optional[str]:
        resp = self._request(
            "POST",
            f"/databases/{self.notes_db_id}/query",
            {
                "filter": {
                    "property": self.title_prop,
                    "title": {"equals": title},
                },
                "page_size": 1,
            },
        )
        results = resp.get("results", []) or []
        return results[0]["id"] if results else None

    def _delete_block_children(self, page_id: str) -> None:
        while True:
            resp = self._request("GET", f"/blocks/{page_id}/children?page_size=100")
            block_ids = [b["id"] for b in resp.get("results", [])]
            if not block_ids:
                break
            for block_id in block_ids:
                self._request("DELETE", f"/blocks/{block_id}")

    def _build_properties(self, title: str, summary: str, topic_ids: list[str]) -> dict:
        props: dict = {self.title_prop: {"title": [{"text": {"content": title}}]}}
        if self.summary_prop and summary:
            props[self.summary_prop] = {
                "rich_text": [{"text": {"content": summary[:2000]}}]
            }
        if self.topics_relation_prop and topic_ids:
            props[self.topics_relation_prop] = {
                "relation": [{"id": tid} for tid in topic_ids]
            }
        return props

    def _append_block_batches(self, page_id: str, blocks: list[dict]) -> None:
        remaining = blocks
        while remaining:
            batch = remaining[:MAX_BLOCKS_PER_REQ]
            remaining = remaining[MAX_BLOCKS_PER_REQ:]
            self._request("PATCH", f"/blocks/{page_id}/children", {"children": batch})

    # ── Output protocol ────────────────────────────────────────────────────────

    def write(self, payload: DigestPayload) -> str | None:
        blocks = markdown_to_blocks(payload.markdown)

        # Optional image map from payload.extra (built by digest.py if images are on).
        image_map = payload.extra.get("image_map") or {}
        if image_map:
            blocks = inject_images(blocks, image_map)
            log.info("Injected %s images into Notion blocks.", len(image_map))

        topic_ids = payload.extra.get("notion_topic_ids") or []
        properties = self._build_properties(payload.title, payload.quick_summary, topic_ids)

        existing_id = self._find_existing_page_id(payload.title)
        if existing_id:
            self._request("PATCH", f"/pages/{existing_id}", {"properties": properties})
            self._delete_block_children(existing_id)
            first_batch = blocks[:MAX_BLOCKS_PER_REQ]
            self._request("PATCH", f"/blocks/{existing_id}/children", {"children": first_batch})
            self._append_block_batches(existing_id, blocks[MAX_BLOCKS_PER_REQ:])
            page_id = existing_id
        else:
            first_batch = blocks[:MAX_BLOCKS_PER_REQ]
            resp = self._request(
                "POST",
                "/pages",
                {
                    "parent": {"database_id": self.notes_db_id},
                    "properties": properties,
                    "children": first_batch,
                },
            )
            page_id = resp["id"]
            self._append_block_batches(page_id, blocks[MAX_BLOCKS_PER_REQ:])

        return resp.get("url", "") if not existing_id else f"https://www.notion.so/{page_id.replace('-', '')}"


# ── Topics detection (separate concern, used optionally before write) ─────────

def load_topics_cache(token: str, topics_db_id: str) -> dict[str, str]:
    """Load all Topics from a Notion DB. Returns {name: page_id}."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    body = json.dumps({"page_size": 100}).encode()
    req = urllib.request.Request(
        f"{NOTION_BASE}/databases/{topics_db_id}/query",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise RuntimeError(f"Notion Topics DB query HTTP {e.code}: {detail}") from e

    cache: dict[str, str] = {}
    for r in data.get("results", []):
        title_arr = r.get("properties", {}).get("Name", {}).get("title", [])
        name = title_arr[0]["plain_text"] if title_arr else ""
        if name:
            cache[name] = r["id"]
    return cache
