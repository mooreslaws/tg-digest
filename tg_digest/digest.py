"""
Telegram Daily Digest.

Reads posts from your Telegram channels (resolved from Telegram Folders and/or
a YAML config) via the MTProto API (Telethon), synthesises per-post summaries
and cross-channel trends using an OpenAI chat model, and writes a daily
Markdown report. Notion export is optional.

Channel composition for each Telegram Folder is re-read from your live Telegram
filters on every run — add/remove channels in the Telegram app, no static list
in the repo.

Flow:
  1. Resolve channels from Telegram Folders + YAML config
  2. Fetch posts in the time window (default: full previous calendar day
     in TG_DIGEST_DATE_TZ; --rolling: last N hours or a UTC day)
  3. OpenAI chat model generates per-post summaries and a Trends section
     (if 2+ channels discuss the same topic)
  4. Save local Markdown report → ./reports/digest_YYYY-MM-DD.md
  5. (Optional) Create / update a Notion page in your Notes DB

Usage:
  python -m tg_digest.digest                         # all configured folders
  python -m tg_digest.digest --dry-run               # no Notion write
  python -m tg_digest.digest --hours 48 --rolling    # rolling 48h (no --date)
  python -m tg_digest.digest --date 2026-04-09       # digest for that date
  python -m tg_digest.digest --folder News           # single folder
  python -m tg_digest.digest --channel durov         # single channel

Required env vars:
  TELEGRAM_API_ID         — from https://my.telegram.org
  TELEGRAM_API_HASH       — from https://my.telegram.org
  OPENAI_API_KEY          — for summarisation

Telegram session (one of):
  TELEGRAM_SESSION        — path to .session file (default: ./telegram.session)
  TELEGRAM_SESSION_STRING — Telethon StringSession (for CI; see tg_digest.auth)

Output adapters (env-driven; see tg_digest.outputs.collect_outputs docs):
  TG_DIGEST_OUTPUT_PATH   — primary local Markdown path (default: ./reports/digest_{date}.md)
  TG_DIGEST_OUTPUT_PATH_2 — optional second file output (e.g. Obsidian vault)
  TG_DIGEST_FRONTMATTER   — JSON dict prepended as YAML frontmatter
  TG_DIGEST_NO_LOCAL      — set to 1 to disable the default local file output
  TG_DIGEST_STDOUT        — set to 1 to also print to stdout
  TG_DIGEST_EXTRA_OUTPUTS — comma-sep module:Class refs for custom adapters
  NOTION_TOKEN, NOTION_NOTES_DB_ID  — enable optional Notion adapter

Other knobs:
  TG_DIGEST_LANG               — system prompt language: en (default) or ru
  TG_DIGEST_SYSTEM_PROMPT_FILE — path to a custom system prompt (overrides LANG)
  TG_DIGEST_STRUCTURED_PROMPT_FILE — custom prompt for `mode: structured` channels
  TG_DIGEST_INCLUDE_IMAGES     — 1 to download / upload photos (default: off)
  TG_DIGEST_DATE_TZ            — IANA TZ for "calendar day" (default: Europe/Berlin)
  OPENAI_CHAT_MODEL            — override the default chat model
  TG_DIGEST_CHANNELS_CONFIG    — override channels.yaml path (default: ./channels.yaml)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
import re
import sys
import socket
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

# ── Telethon import ────────────────────────────────────────────────────────────
try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.types import Message, Channel
    from telethon.tl.functions.messages import GetDialogFiltersRequest
    TELETHON_AVAILABLE = True
except ImportError:
    TELETHON_AVAILABLE = False
    StringSession = None  # type: ignore

# ── Config ─────────────────────────────────────────────────────────────────────

TELEGRAM_API_ID   = (os.environ.get("TELEGRAM_API_ID", "") or "").strip()
TELEGRAM_API_HASH = (os.environ.get("TELEGRAM_API_HASH", "") or "").strip()
OPENAI_API_KEY    = (os.environ.get("OPENAI_API_KEY", "") or "").strip()


def _env_truthy(name: str) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


# Image uploads disabled by default (faster CI, fewer external timeouts).
# Enable: TG_DIGEST_INCLUDE_IMAGES=1 or --with-images.
INCLUDE_IMAGES_BY_ENV = _env_truthy("TG_DIGEST_INCLUDE_IMAGES")


# ── OpenAI chat-model helpers (inlined from openai_defaults) ───────────────────

DEFAULT_OPENAI_CHAT_MODEL = "gpt-4o-mini"


def chat_completion_limit_params(model: str, limit: int) -> dict[str, int]:
    """GPT-5 and o-series models require max_completion_tokens instead of max_tokens."""
    m = (model or "").strip().lower()
    if m.startswith("gpt-5") or m.startswith("o1") or m.startswith("o3") or m.startswith("o4"):
        return {"max_completion_tokens": limit}
    return {"max_tokens": limit}


def get_openai_chat_model(*, config_value: str | None = None) -> str:
    """Priority: OPENAI_CHAT_MODEL env → config_value → default."""
    env = os.environ.get("OPENAI_CHAT_MODEL", "").strip()
    if env:
        return env
    if config_value is not None:
        cv = str(config_value).strip()
        if cv:
            return cv
    return DEFAULT_OPENAI_CHAT_MODEL


# ── Prompt loading ─────────────────────────────────────────────────────────────
# Prompts live as Markdown files under tg_digest/prompts/. Users can supply
# their own by setting TG_DIGEST_SYSTEM_PROMPT_FILE / TG_DIGEST_STRUCTURED_PROMPT_FILE.

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_SUPPORTED_LANGS = {"en", "ru"}
_prompt_cache: dict[str, str] = {}


def _read_prompt(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip() + "\n"
    except OSError as e:
        raise RuntimeError(f"Failed to read prompt file {path}: {e}") from e


def _get_system_prompt() -> str:
    if "system" in _prompt_cache:
        return _prompt_cache["system"]

    custom = (os.environ.get("TG_DIGEST_SYSTEM_PROMPT_FILE") or "").strip()
    if custom:
        prompt = _read_prompt(Path(custom).expanduser())
    else:
        lang = (os.environ.get("TG_DIGEST_LANG") or "en").strip().lower()
        if lang not in _SUPPORTED_LANGS:
            log.warning("Unknown TG_DIGEST_LANG=%r, falling back to 'en'", lang)
            lang = "en"
        prompt = _read_prompt(_PROMPTS_DIR / f"system_{lang}.md")
    _prompt_cache["system"] = prompt
    return prompt


def _get_structured_prompt() -> str:
    if "structured" in _prompt_cache:
        return _prompt_cache["structured"]

    custom = (os.environ.get("TG_DIGEST_STRUCTURED_PROMPT_FILE") or "").strip()
    if custom:
        prompt = _read_prompt(Path(custom).expanduser())
    else:
        prompt = _read_prompt(_PROMPTS_DIR / "structured_en.md")
    _prompt_cache["structured"] = prompt
    return prompt


# ── Paths (CWD-relative by default; override via env) ──────────────────────────

CWD = Path.cwd()

SESSION_FILE = Path(
    os.environ.get("TELEGRAM_SESSION") or str(CWD / "telegram.session")
)
TELEGRAM_SESSION_STRING = (os.environ.get("TELEGRAM_SESSION_STRING") or "").strip()

CHANNELS_CONFIG = Path(
    os.environ.get("TG_DIGEST_CHANNELS_CONFIG") or str(CWD / "channels.yaml")
)

# Temp directory for image downloads (only used when TG_DIGEST_INCLUDE_IMAGES=1).
IMAGES_DIR = Path(
    os.environ.get("TG_DIGEST_IMAGES_DIR") or str(CWD / ".tg_digest_images")
)
TELEGRAPH_UPLOAD = "https://telegra.ph/upload"

# Topics DB (used optionally via --detect-topics, see Notion output).
NOTION_TOPICS_DB_ID = (os.environ.get("NOTION_TOPICS_DB_ID", "") or "").strip()

# OpenAI client knobs.
OPENAI_ENDPOINT   = "https://api.openai.com/v1/chat/completions"
OPENAI_REQUEST_TIMEOUT_SEC = int((os.environ.get("TG_DIGEST_OPENAI_TIMEOUT_SEC") or "420").strip() or "420")
OPENAI_MAX_RETRIES = int((os.environ.get("TG_DIGEST_OPENAI_RETRIES") or "3").strip() or "3")


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("telegram_digest")


def _telegram_digest_tz() -> ZoneInfo:
    name = (os.environ.get("TG_DIGEST_DATE_TZ") or "Europe/Berlin").strip()
    try:
        return ZoneInfo(name)
    except Exception:
        log.warning("Invalid TG_DIGEST_DATE_TZ=%r, using Europe/Berlin", name)
        return ZoneInfo("Europe/Berlin")


# ── Config loading ─────────────────────────────────────────────────────────────

def load_channels_config() -> dict:
    """Load telegram_channels.yaml — supports folders and/or manual channels."""
    if not CHANNELS_CONFIG.exists():
        log.error(f"Channels config not found: {CHANNELS_CONFIG}")
        sys.exit(1)

    try:
        import yaml  # type: ignore
        with open(CHANNELS_CONFIG, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        pass

    def _strip_comment(s: str) -> str:
        if "#" in s:
            s = s[:s.index("#")]
        return s.strip()

    config: dict = {"defaults": {}, "folders": [], "channels": []}
    lines = CHANNELS_CONFIG.read_text(encoding="utf-8").splitlines()
    section = None
    current: dict = {}

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if stripped == "defaults:":
            section = "defaults"
        elif stripped == "folders:":
            section = "folders"
        elif stripped == "channels:":
            section = "channels"
        elif section == "folders":
            if stripped.startswith("- "):
                val = _strip_comment(stripped[2:]).strip('"')
                if val:
                    config["folders"].append(val)
        elif section == "channels":
            if stripped.startswith("- username:"):
                if current:
                    config["channels"].append(current)
                current = {"username": _strip_comment(stripped.split(":", 1)[1]).strip('"')}
            elif stripped.startswith("label:") and current:
                current["label"] = _strip_comment(stripped.split(":", 1)[1]).strip('"')
        elif section == "defaults":
            if stripped.startswith("hours_back:"):
                config["defaults"]["hours_back"] = int(_strip_comment(stripped.split(":", 1)[1]))
            elif stripped.startswith("min_post_length:"):
                config["defaults"]["min_post_length"] = int(_strip_comment(stripped.split(":", 1)[1]))

    if current:
        config["channels"].append(current)

    return config


# ── Image upload ───────────────────────────────────────────────────────────────

def _multipart_upload(url: str, file_path: Path, field_name: str = "file") -> Optional[str]:
    import mimetypes

    content_type = mimetypes.guess_type(str(file_path))[0] or "image/jpeg"
    file_data = file_path.read_bytes()
    boundary = b"Boundary9876543210"
    body = b"--" + boundary + b"\r\n"
    body += (
        f'Content-Disposition: form-data; name="{field_name}"; '
        f'filename="{file_path.name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    body += file_data
    body += b"\r\n--" + boundary + b"--\r\n"

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary.decode()}",
            "User-Agent": "Mozilla/5.0 (compatible; tg-digest)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode().strip()
    return raw


def upload_image(file_path: Path) -> Optional[str]:
    """Get a public URL for the image — tries 0x0.st first, falls back to telegra.ph."""
    if not file_path.exists() or file_path.stat().st_size == 0:
        log.warning(f"  Image skip: empty or missing file {file_path}")
        return None

    # 1) 0x0.st
    try:
        raw = _multipart_upload("https://0x0.st", file_path)
        if raw.startswith("http"):
            return raw
        log.warning(f"  0x0.st unexpected response: {raw[:120]!r}")
    except Exception as e:
        log.warning(f"  0x0.st upload failed: {e}")

    # 2) telegra.ph (often more tolerant of data-center IPs than 0x0.st)
    try:
        raw = _multipart_upload(TELEGRAPH_UPLOAD, file_path, field_name="file")
        if raw.startswith("["):
            try:
                arr = json.loads(raw)
            except json.JSONDecodeError:
                arr = []
            if arr and isinstance(arr, list) and isinstance(arr[0], dict) and arr[0].get("src"):
                src = arr[0]["src"]
                if src.startswith("http"):
                    return src
                return f"https://telegra.ph{src}"
        if raw.startswith("http"):
            return raw
        log.warning(f"  telegra.ph unexpected response: {raw[:120]!r}")
    except Exception as e:
        log.warning(f"  telegra.ph upload failed: {e}")

    return None


# ── Telegram folder resolution ─────────────────────────────────────────────────

async def resolve_folders(
    client: "TelegramClient",
    folder_names: list[str],
) -> list[dict]:
    """Resolve Telegram Folder names → list of channel dicts {username, label}."""
    result = await client(GetDialogFiltersRequest())
    channels: list[dict] = []
    seen_ids: set[int] = set()

    target_names = {n.lower() for n in folder_names}

    for f in result.filters:
        title = getattr(f, "title", None)
        if not title:
            continue
        folder_title = getattr(title, "text", title) if hasattr(title, "text") else str(title)

        if folder_title.lower() not in target_names:
            continue

        before_ct = len(channels)
        log.info(f"  Resolving folder: {folder_title}")
        folder_id = getattr(f, "id", None)

        def try_add_broadcast(ent: Any) -> bool:
            if not isinstance(ent, Channel) or ent.megagroup:
                return False
            if ent.id in seen_ids:
                return False
            seen_ids.add(ent.id)
            un = ent.username or ""
            channels.append(
                {
                    "username": un,
                    "label": ent.title or un or str(ent.id),
                    "entity": ent,
                }
            )
            return True

        n_from_dialogs = 0
        if folder_id is not None:
            try:
                async for dialog in client.iter_dialogs(folder=folder_id):
                    if try_add_broadcast(dialog.entity):
                        n_from_dialogs += 1
            except Exception as e:
                log.warning(
                    "  iter_dialogs(folder=%r) failed: %s — using explicit peers only",
                    folder_id,
                    e,
                )

        peer_lists: list[Any] = []
        for attr in ("pinned_peers", "include_peers"):
            lst = getattr(f, attr, None) or []
            peer_lists.extend(lst)

        seen_peer_repr: set[str] = set()
        unique_peers: list[Any] = []
        for p in peer_lists:
            k = repr(p)
            if k in seen_peer_repr:
                continue
            seen_peer_repr.add(k)
            unique_peers.append(p)

        n_from_explicit = 0
        n_exc = 0
        for p in unique_peers:
            try:
                ent_peer = await client.get_entity(p)
                if try_add_broadcast(ent_peer):
                    n_from_explicit += 1
            except Exception:
                n_exc += 1
                continue

        log.info(
            "  folder %r: %s channel(s) via iter_dialogs(folder_id=%s), %s via pinned+include_peers",
            folder_title,
            n_from_dialogs,
            folder_id,
            n_from_explicit,
        )


    log.info(f"  Resolved {len(channels)} channels from {len(folder_names)} folder(s)")
    return channels


# ── Telegram fetch ─────────────────────────────────────────────────────────────

async def fetch_channel_posts(
    client: "TelegramClient",
    username: str,
    hours: int,
    min_length: int,
    skip_images: bool = True,
    *,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
    channel_entity: Any = None,
) -> tuple[list[dict], int, int]:
    """
    Fetch recent text posts from a Telegram channel; optionally attach image URLs.

    Time window:
    - If window_start and window_end are set (UTC): posts with window_start <= date < window_end.
    - Else: rolling window msg.date >= now - hours.

    Returns (posts_passing_min_length, in_window_count, below_min_len_count).
    """
    cutoff: Optional[datetime] = None
    if window_start is None or window_end is None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    posts: list[dict] = []
    in_window_count = 0
    below_min_len_count = 0
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    images_downloaded = 0
    MAX_IMAGES = 3

    slug = (username or "").lstrip("@") or "unknown"
    try:
        if channel_entity is not None:
            entity = channel_entity
        elif username:
            entity = await client.get_entity(username)
        else:
            return [], -1, -1
        slug = (getattr(entity, "username", None) or "").lstrip("@") or f"id{entity.id}"
        async for msg in client.iter_messages(entity, limit=100):
            if not isinstance(msg, Message):
                continue
            d = msg.date.replace(tzinfo=timezone.utc)
            if window_start is not None and window_end is not None:
                if d >= window_end:
                    continue
                if d < window_start:
                    break
            else:
                assert cutoff is not None
                if d < cutoff:
                    break

            in_window_count += 1
            text = msg.text or ""
            has_photo = bool(msg.photo)
            mime = (
                getattr(msg.document, "mime_type", None) or ""
                if msg.document
                else ""
            )
            has_image_doc = bool(msg.document and mime.startswith("image/"))
            # Keep posts with media + short text; the photo itself can still be informative.
            if len(text) < min_length and not has_photo and not has_image_doc:
                below_min_len_count += 1
                continue

            channel_id = getattr(entity, "id", None)
            if hasattr(entity, "username") and entity.username:
                url = f"https://t.me/{entity.username}/{msg.id}"
            elif channel_id:
                cid = str(abs(channel_id))
                if cid.startswith("100"):
                    cid = cid[3:]
                url = f"https://t.me/c/{cid}/{msg.id}"
            else:
                url = ""

            image_url = None
            media = None
            ext = ".jpg"
            if has_photo and images_downloaded < MAX_IMAGES:
                media = msg.photo
            elif has_image_doc and images_downloaded < MAX_IMAGES:
                media = msg.document
                if "png" in mime:
                    ext = ".png"
                elif "webp" in mime:
                    ext = ".webp"
                elif "gif" in mime:
                    ext = ".gif"

            if (
                not skip_images
                and media is not None
                and images_downloaded < MAX_IMAGES
            ):
                local_path = IMAGES_DIR / f"{slug}_{msg.id}{ext}"
                try:
                    await client.download_media(media, file=str(local_path))
                    image_url = upload_image(local_path)
                    if image_url:
                        images_downloaded += 1
                    else:
                        log.warning(
                            f"  No public URL for image msg {msg.id} ({slug}) "
                            f"(upload returned None; see 0x0/telegra logs above)"
                        )
                except Exception as e:
                    log.warning(f"  Telegram download_media failed msg {msg.id} ({slug}): {e}")
                finally:
                    local_path.unlink(missing_ok=True)

            posts.append({
                "id": msg.id,
                "date": msg.date.replace(tzinfo=timezone.utc).isoformat(),
                "text": text,
                "url": url,
                "image_url": image_url,
            })
    except Exception as e:
        log.warning(f"  Could not fetch channel {slug!r}: {e}")
        return [], -1, -1

    return posts, in_window_count, below_min_len_count


async def fetch_all_channels(
    config: dict,
    hours: int,
    only_channel: Optional[str] = None,
    only_folder: Optional[str] = None,
    skip_images: bool = True,
    *,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
) -> tuple[dict[str, list[dict]], dict[str, str], list[tuple[str, str]]]:
    """
    Fetch posts for configured channels (from folders and/or YAML).
    Returns (channel_data, channel_labels, silent_channels) where silent_channels is
    a list of (username, label) with zero Telegram messages in the time window
    (before min_post_length filter).
    """
    if not TELETHON_AVAILABLE:
        log.error("Telethon is not installed. Run: pip install telethon")
        sys.exit(1)

    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        log.error("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set.")
        sys.exit(1)

    if TELEGRAM_SESSION_STRING:
        if StringSession is None:
            log.error("Telethon StringSession unavailable (import error).")
            sys.exit(1)
        client_session: "str | StringSession" = StringSession(TELEGRAM_SESSION_STRING)
        log.info("Using TELEGRAM_SESSION_STRING (CI / string session)")
    else:
        if not SESSION_FILE.exists():
            log.error(
                f"Session file not found: {SESSION_FILE}\n"
                "Run: python -m tg_digest.auth   (one-time interactive login)\n"
                "Or set TELEGRAM_SESSION_STRING in your environment (for CI / GitHub Actions)."
            )
            sys.exit(1)
        client_session = str(SESSION_FILE)

    defaults = config.get("defaults", {})
    default_min = defaults.get("min_post_length", 150)

    results: dict[str, list[dict]] = {}
    labels: dict[str, str] = {}
    silent: list[tuple[str, str]] = []

    async with TelegramClient(client_session, int(TELEGRAM_API_ID), TELEGRAM_API_HASH) as client:
        # Build channel list from folders + manual config
        channels: list[dict] = []

        # Resolve folders (each name maps to one Telegram filter; peer list is live at run time)
        folder_names = config.get("folders", [])
        if only_folder:
            folder_names = [only_folder]
        if folder_names:
            seen_folder_entity_ids: set[int] = set()
            merged_from_folders: list[dict] = []
            for fn in folder_names:
                part = await resolve_folders(client, [fn])
                log.info(
                    "  Folder %r: %s channel(s) — list comes from Telegram filters this run "
                    "(edit folder in the app to add/remove; no static channel list).",
                    fn,
                    len(part),
                )
                for c in part:
                    ent = c.get("entity")
                    if not ent:
                        continue
                    eid = getattr(ent, "id", None)
                    if eid is None or eid in seen_folder_entity_ids:
                        continue
                    seen_folder_entity_ids.add(eid)
                    merged_from_folders.append(c)
            channels.extend(merged_from_folders)

        # Add manual channels from YAML
        if not only_folder:
            for ch in config.get("channels", []):
                channels.append(ch)

        # Filter to single channel if requested
        if only_channel:
            name = only_channel.lstrip("@")
            matched = [c for c in channels if c.get("username", "").lstrip("@") == name]
            channels = matched if matched else [{"username": name, "label": name}]

        log.info(f"  Total channels to scan: {len(channels)}")


        overrides = config.get("channel_overrides", {})

        for ch in channels:
            username = (ch.get("username") or "").lstrip("@")
            ent = ch.get("entity")
            if username:
                channel_key = username
            elif isinstance(ent, Channel):
                channel_key = f"id{ent.id}"
            else:
                log.warning(
                    "  Skip channel (no @username and no Channel entity): %r",
                    ch.get("label"),
                )
                continue

            override = overrides.get(username, {}) if username else {}
            label = override.get("label", ch.get("label", channel_key))
            labels[channel_key] = label
            min_len = override.get("min_post_length", ch.get("min_post_length", default_min))

            log.info(f"  Fetching {channel_key} ({label})...")
            posts, in_w, _below = await fetch_channel_posts(
                client,
                username,
                hours,
                min_len,
                skip_images=skip_images,
                window_start=window_start,
                window_end=window_end,
                channel_entity=ent if isinstance(ent, Channel) else None,
            )
            if in_w < 0:
                log.info("    → fetch error (channel not counted as silent)")
            else:
                log.info(f"    → {len(posts)} posts after filter (raw in window: {in_w})")
                if in_w == 0:
                    silent.append((channel_key, label))
                elif posts:
                    results[channel_key] = posts

    silent.sort(key=lambda t: t[0].lower())
    return results, labels, silent


# ── GPT synthesis ──────────────────────────────────────────────────────────────


def _openai_request(messages: list[dict], max_tokens: int = 6000, request_tag: str = "generic") -> str:
    model = get_openai_chat_model()
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        **chat_completion_limit_params(model, max_tokens),
    }).encode()
    req = urllib.request.Request(
        OPENAI_ENDPOINT,
        data=body,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    data = None
    for attempt in range(OPENAI_MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=OPENAI_REQUEST_TIMEOUT_SEC) as resp:
                data = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            # Retry transient provider-side overload/rate limiting.
            if e.code in (408, 429, 500, 502, 503, 504) and attempt < OPENAI_MAX_RETRIES:
                delay = 2 ** attempt
                log.warning(
                    "OpenAI HTTP %s on digest request, retry %s/%s in %ss",
                    e.code,
                    attempt + 1,
                    OPENAI_MAX_RETRIES,
                    delay,
                )
                time.sleep(delay)
                continue
            raise RuntimeError(
                f"OpenAI HTTP {e.code} ({OPENAI_ENDPOINT}): {detail}"
            ) from e
        except (socket.timeout, TimeoutError, urllib.error.URLError, ConnectionResetError, ssl.SSLEOFError) as e:
            if attempt < OPENAI_MAX_RETRIES:
                delay = 2 ** attempt
                log.warning(
                    "OpenAI network timeout/error (%s), retry %s/%s in %ss",
                    type(e).__name__,
                    attempt + 1,
                    OPENAI_MAX_RETRIES,
                    delay,
                )
                time.sleep(delay)
                continue
            raise RuntimeError(
                f"OpenAI request failed after retries ({OPENAI_ENDPOINT}): {type(e).__name__}: {e}"
            ) from e
        except Exception as e:
            raise
    if data is None:
        raise RuntimeError("OpenAI request did not return data")
    choice = data["choices"][0]
    msg = choice.get("message") or {}
    raw = msg.get("content")
    text = (raw if isinstance(raw, str) else "") or ""
    text = text.strip()
    if not text:
        fr = choice.get("finish_reason")
        log.warning(
            "OpenAI returned empty content (finish_reason=%s); usage=%s",
            fr,
            data.get("usage"),
        )
    return text


MAX_POSTS_PER_CHANNEL = int(
    (os.environ.get("TG_DIGEST_MAX_POSTS_PER_CHANNEL") or "5").strip() or "5"
)
# Structured mode: kept compact by default — high-volume channels can otherwise dominate token usage.
MAX_STRUCTURED_POSTS = int(
    (os.environ.get("TG_DIGEST_STRUCTURED_MAX_POSTS") or "20").strip() or "20"
)
STRUCTURED_MAX_OUTPUT_TOKENS = int(
    (os.environ.get("TG_DIGEST_STRUCTURED_MAX_TOKENS") or "3000").strip() or "3000"
)
# Larger chunks = fewer system_prompt repetitions = better prompt-caching hit rate.
# 20 keeps a typical 25–35 channel digest at 1–2 chunks.
DIGEST_CHANNELS_PER_CHUNK = int(
    (os.environ.get("TG_DIGEST_CHANNELS_PER_CHUNK") or "20").strip() or "20"
)
# Per-post text budget sent to the model (600 was too short for a dense paragraph).
DIGEST_POST_TEXT_CHARS = int(
    (os.environ.get("TG_DIGEST_POST_TEXT_CHARS") or "1500").strip() or "1500"
)

# ── Structured-mode channel prompt ─────────────────────────────────────────────
# This is the per-channel mode for high-volume / structured feeds (deal flow,
# job listings, product launches, etc). Configure per channel in channels.yaml:
#
#     channel_overrides:
#       mychannel:
#         mode: structured
#
# Customise the system prompt by setting TG_DIGEST_STRUCTURED_PROMPT_FILE.


def generate_structured_section(
    username: str,
    label: str,
    posts: list[dict],
    date_str: str,
) -> str:
    """Run an alternate prompt for one channel — used for structured feeds."""
    post_lines = []
    for i, p in enumerate(posts[:MAX_STRUCTURED_POSTS], 1):
        date_short = p["date"][:10]
        post_lines.append(f"--- Post {i} ---")
        post_lines.append(f"Date: {date_short}")
        post_lines.append(f"URL: {p['url']}")
        post_lines.append(p["text"][:1500])
        post_lines.append("")

    all_posts_text = "\n".join(post_lines)

    user_prompt = (
        f"Channel: @{username} ({label})\n"
        f"Date: {date_str}\n"
        f"Total posts: {len(posts)}\n\n"
        f"{all_posts_text}\n"
    )

    structured_md = _openai_request(
        [
            {"role": "system", "content": _get_structured_prompt()},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=STRUCTURED_MAX_OUTPUT_TOKENS,
        request_tag="structured",
    )
    return structured_md


def _format_channel_for_prompt(username: str, label: str, posts: list[dict]) -> str:
    # If too many posts, keep only the longest (most substantive) ones
    if len(posts) > MAX_POSTS_PER_CHANNEL:
        posts = sorted(posts, key=lambda p: len(p.get("text", "")), reverse=True)[:MAX_POSTS_PER_CHANNEL]
    lines = [f"=== CHANNEL: @{username} ({label}) — {len(posts)} post(s) ==="]
    for p in posts:
        date_short = p["date"][:10]
        lines.append(f"[{date_short}] URL: {p['url']}")
        lines.append(p["text"][:DIGEST_POST_TEXT_CHARS])
        lines.append("")
    return "\n".join(lines)


# Matches a Markdown level-2 "Overview" heading in either English or Russian.
# Used to extract a quick-summary snippet from the digest without an extra LLM call.
_OVERVIEW_HEADER_RE = re.compile(
    r"##\s*(?:Overview|Обзор\s+(?:дня|периода|окна))\s*\n+",
    re.IGNORECASE,
)


def _extract_overview_as_summary(full_md: str, date_str: str) -> str:
    """Pull the "Overview" section text from the generated digest to use as a
    short summary. Saves a second GPT round-trip just for a 200-token blurb."""
    text = full_md or ""
    m = _OVERVIEW_HEADER_RE.search(text)
    if m:
        tail = text[m.end():]
        next_header = re.search(r"\n##\s+|\n---\s*\n", tail)
        body = tail[: next_header.start()] if next_header else tail
        body = body.strip()
        if body:
            # Notion's rich_text props max out at 2000 chars; we aim for ~500 then trim on a sentence.
            snippet = body[:500]
            last_dot = snippet.rfind(". ")
            if last_dot > 200:
                snippet = snippet[: last_dot + 1]
            return snippet
    # Fallback: first 300 chars of plain content (skip headings and dividers).
    plain = "\n".join(
        ln for ln in text.splitlines()
        if ln.strip() and not ln.startswith(("#", "---"))
    )
    return plain[:300] or f"Telegram Digest — {date_str}"


def _generate_digest_single_pass(
    date_str: str,
    *,
    n_channels: int,
    n_posts: int,
    all_channels_text: str,
) -> str:
    # User prompt is data-only. The format spec lives in the system prompt
    # (tg_digest/prompts/system_*.md) so changing language is a one-env-var flip.
    user_prompt = (
        f"Date: {date_str}\n"
        f"Channels with posts: {n_channels}\n"
        f"Total posts: {n_posts}\n\n"
        f"{all_channels_text}\n\n"
        "Begin the digest with `# Telegram Digest — <date>` and a `## Overview` "
        "section (2–3 sentences of facts). Then process every channel in order. "
        "Add a final `## Trends` section only if 2+ channels actually discuss the "
        "same topic."
    )

    return _openai_request([
        {"role": "system", "content": _get_system_prompt()},
        {"role": "user", "content": user_prompt},
    ], max_tokens=32000, request_tag="digest_single_pass")


def _generate_digest_chunked(
    channel_data: dict[str, list[dict]],
    channel_labels: dict[str, str],
    date_str: str,
    *,
    n_channels: int,
    n_posts: int,
    window_days: int = 2,
) -> str:
    items = list(channel_data.items())
    n_chunks = (len(items) + DIGEST_CHANNELS_PER_CHUNK - 1) // DIGEST_CHANNELS_PER_CHUNK
    parts: list[str] = []
    for ci in range(n_chunks):
        start = ci * DIGEST_CHANNELS_PER_CHUNK
        chunk = dict(items[start : start + DIGEST_CHANNELS_PER_CHUNK])
        channel_blocks = [
            _format_channel_for_prompt(u, channel_labels.get(u, u), posts)
            for u, posts in chunk.items()
        ]
        chunk_text = "\n\n".join(channel_blocks)
        chunk_posts = sum(len(p) for p in chunk.values())
        # Chunk mode: each call processes a subset of channels and emits only
        # per-channel blocks. The outer code (below) prepends the title +
        # overview manually because no single chunk has the full picture.
        user_prompt = (
            f"Date: {date_str}\n"
            f"Part {ci + 1} of {n_chunks} (auto-chunked by channel count).\n"
            f"Channels in this part: {len(chunk)}, posts: {chunk_posts}\n\n"
            f"{chunk_text}\n\n"
            "Produce per-channel blocks ONLY for the channels above.\n"
            "DO NOT include the top-level `# Telegram Digest` title, the `## Overview`, "
            "or the `## Trends` section — those will be added separately.\n"
            "For every channel: `## @username — Label`, then 2–4 most substantive "
            "posts following the format rules in the system prompt. "
            "Separate posts of the same channel with a blank line, "
            "and different channels with `---` on its own line."
        )

        part = _openai_request([
            {"role": "system", "content": _get_system_prompt()},
            {"role": "user", "content": user_prompt},
        ], max_tokens=16000, request_tag=f"digest_chunk_{ci + 1}_of_{n_chunks}")
        if part.strip():
            parts.append(part.strip())
        else:
            log.warning("Empty GPT response for chunk %s/%s", ci + 1, n_chunks)

    period_word = "1 day" if window_days == 1 else f"{window_days} days"
    header = (
        f"# Telegram Digest — {date_str}\n\n"
        f"## Overview\n"
        f"For the selected {period_word}: **{n_channels}** channels with posts, "
        f"**{n_posts}** posts total. Text generated in {n_chunks} chunks due to "
        f"the channel count.\n\n"
        f"---\n\n"
    )
    return header + "\n\n---\n\n".join(parts)


def generate_digest(
    channel_data: dict[str, list[dict]],
    channel_labels: dict[str, str],
    date_str: str,
    *,
    window_days: int = 2,
) -> tuple[str, str]:
    """
    Generate full digest markdown and short summary.
    Returns (full_markdown, short_summary).
    """
    channel_blocks = []
    for username, posts in channel_data.items():
        label = channel_labels.get(username, username)
        channel_blocks.append(_format_channel_for_prompt(username, label, posts))

    all_channels_text = "\n\n".join(channel_blocks)
    n_channels = len(channel_data)
    n_posts = sum(len(p) for p in channel_data.values())

    if n_channels <= DIGEST_CHANNELS_PER_CHUNK:
        full_md = _generate_digest_single_pass(
            date_str,
            n_channels=n_channels,
            n_posts=n_posts,
            all_channels_text=all_channels_text,
        )
    else:
        log.info(
            "Digest: %s channels > %s — chunked mode",
            n_channels,
            DIGEST_CHANNELS_PER_CHUNK,
        )
        full_md = _generate_digest_chunked(
            channel_data,
            channel_labels,
            date_str,
            n_channels=n_channels,
            n_posts=n_posts,
            window_days=window_days,
        )

    short_summary = _extract_overview_as_summary(full_md, date_str)
    return full_md, short_summary


def append_silent_channels_footer(
    md: str,
    silent: list[tuple[str, str]],
    window_caption: str,
) -> str:
    """Footer listing channels that had zero messages in the window (before min_post_length filter)."""
    if not silent:
        return md
    lines = [
        "",
        "---",
        "",
        "## Silent channels",
        "",
        f"{window_caption}: the channels below had **no messages** in the selected window (before length filter).",
        "",
    ]
    for u, label in silent:
        lines.append(f"- @{u} — {label}")
    return md + "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Telegram Digest (default cadence: every 2 days)")
    p.add_argument("--dry-run", action="store_true", help="Fetch and generate, no Notion write")
    p.add_argument("--hours", type=int, default=24, help="Look back N hours (used only with --rolling)")
    p.add_argument(
        "--days",
        type=int,
        default=int((os.environ.get("TG_DIGEST_DAYS") or "2").strip() or "2"),
        help="Digest window in days (default: 2). "
        "Digest for date D covers the previous N calendar days in TG_DIGEST_DATE_TZ.",
    )
    p.add_argument("--folder", help="Process a single Telegram folder (e.g. News)")
    p.add_argument("--channel", help="Process a single channel username (e.g. durov)")
    p.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="Date in the header and filename; window = previous --days days in TZ. "
        "Without --date: date = today in TG_DIGEST_DATE_TZ.",
    )
    p.add_argument(
        "--with-images",
        action="store_true",
        help="Download Telegram photos and upload to 0x0.st/telegra.ph (slow; default is text-only)",
    )
    p.add_argument(
        "--rolling",
        action="store_true",
        help="Legacy: rolling window of last --hours from now (UTC); with --date — that day in UTC.",
    )
    p.add_argument(
        "--detect-topics",
        action="store_true",
        default=_env_truthy("TG_DIGEST_DETECT_TOPICS"),
        help="Pick Notion Topics via a separate GPT call. Requires a NotionOutput "
        "adapter + NOTION_TOPICS_DB_ID. Off by default.",
    )
    return p.parse_args()


# ── Topic detection (Notion-only feature, but uses our shared OpenAI client) ──

def detect_topics(digest_summary: str, topics_cache: dict[str, str]) -> list[str]:
    """Ask the chat model to pick 2–5 relevant Topics from the available list."""
    topic_names = sorted(topics_cache.keys())
    prompt = (
        f"Here is a short summary of a Telegram channels digest:\n\n{digest_summary}\n\n"
        f"Available Topics:\n{json.dumps(topic_names, ensure_ascii=False)}\n\n"
        "Pick the 2–5 topics that BEST describe the digest. "
        'Return ONLY a JSON array of exact topic names. Example: '
        '["AI Agents", "Macro"]'
    )
    raw = _openai_request([{"role": "user", "content": prompt}], max_tokens=300, request_tag="topics")
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw.strip())
    try:
        chosen = json.loads(raw)
        if isinstance(chosen, list):
            return [t for t in chosen if t in topics_cache]
    except json.JSONDecodeError:
        log.warning("  Topics detection: invalid JSON from GPT: %s", raw[:200])
    return []


def main() -> None:
    args = parse_args()

    # Validate required env (Telegram + OpenAI). Output targets are validated
    # separately below — at least one must be configured.
    missing = []
    if not TELEGRAM_API_ID:
        missing.append("TELEGRAM_API_ID")
    if not TELEGRAM_API_HASH:
        missing.append("TELEGRAM_API_HASH")
    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")
    if missing:
        log.error("Missing env vars: %s", ", ".join(missing))
        sys.exit(1)

    if not TELETHON_AVAILABLE:
        log.error("Telethon not installed. Run: pip install telethon")
        sys.exit(1)

    # Resolve output adapters from environment. At least one must be configured —
    # otherwise the script would do all the work and throw it away.
    from .outputs import collect_outputs, DigestPayload
    from .outputs.notion import NotionOutput, load_topics_cache as _notion_load_topics

    outputs = collect_outputs()
    if not outputs:
        log.error(
            "No output adapters configured — refusing to do work that goes nowhere. "
            "Either unset TG_DIGEST_NO_LOCAL, configure NOTION_TOKEN + NOTION_NOTES_DB_ID, "
            "or set TG_DIGEST_STDOUT=1."
        )
        sys.exit(1)
    log.info("Outputs configured: %s", ", ".join(o.name for o in outputs))

    tz = _telegram_digest_tz()
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    date_str: str

    window_days = max(1, int(args.days or 1))

    if args.rolling:
        if args.date:
            try:
                ds = args.date.strip()
                datetime.strptime(ds, "%Y-%m-%d")
                window_start = datetime.fromisoformat(f"{ds}T00:00:00+00:00")
                window_end = window_start + timedelta(days=1)
                date_str = ds
            except ValueError:
                log.error("--date must be YYYY-MM-DD")
                sys.exit(1)
        else:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    else:
        if args.date:
            try:
                digest_d = datetime.strptime(args.date.strip(), "%Y-%m-%d").date()
            except ValueError:
                log.error("--date must be YYYY-MM-DD")
                sys.exit(1)
        else:
            digest_d = datetime.now(tz).date()
        date_str = digest_d.isoformat()
        prev_d = digest_d - timedelta(days=window_days)
        window_start = datetime(prev_d.year, prev_d.month, prev_d.day, 0, 0, 0, tzinfo=tz).astimezone(
            timezone.utc
        )
        window_end = datetime(
            digest_d.year, digest_d.month, digest_d.day, 0, 0, 0, tzinfo=tz
        ).astimezone(timezone.utc)

    note_title = f"Telegram Digest — {date_str}"
    skip_images = not (INCLUDE_IMAGES_BY_ENV or args.with_images)
    if skip_images:
        log.info("Images disabled (default). Enable: TG_DIGEST_INCLUDE_IMAGES=1 or --with-images.")
    else:
        log.info("Images enabled (TG_DIGEST_INCLUDE_IMAGES or --with-images).")

    if args.rolling:
        if window_start is not None:
            log.info(f"=== Telegram Digest for {date_str} (UTC calendar day, --rolling) ===")
            window_caption = f"Window: UTC calendar day **{date_str}**"
        else:
            log.info(f"=== Telegram Digest for {date_str} (last {args.hours}h rolling, UTC) ===")
            window_caption = f"Window: last **{args.hours}** hours before run (rolling, UTC)"
    else:
        digest_d_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        start_d = (digest_d_obj - timedelta(days=window_days)).isoformat()
        end_d = (digest_d_obj - timedelta(days=1)).isoformat()
        period_label = start_d if window_days == 1 else f"{start_d}..{end_d}"
        log.info(
            "=== Telegram Digest for %s (posts in local period %s, %s day(s), TZ %s) ===",
            date_str,
            period_label,
            window_days,
            tz.key,
        )
        window_caption = (
            f"Window: **{period_label}** ({tz.key}, {window_days} day(s)) → digest for **{date_str}**"
        )

    # Dedup: we will update existing note for this date below (no skip)
    config = load_channels_config()


    # Fetch posts (folders are resolved dynamically from Telegram)
    log.info("Fetching posts...")
    channel_data, channel_labels, silent_channels = asyncio.run(
        fetch_all_channels(
            config,
            hours=args.hours,
            only_channel=args.channel,
            only_folder=args.folder,
            skip_images=skip_images,
            window_start=window_start,
            window_end=window_end,
        )
    )

    total = sum(len(p) for p in channel_data.values())
    log.info(
        f"Fetched {total} posts across {len(channel_data)} channels; "
        f"silent (no msgs in window): {len(silent_channels)}"
    )

    if not channel_data and not silent_channels:
        log.info("No channels with data and no silent list — nothing to write.")
        return

    # Separate analytics channels from regular digest channels
    overrides = config.get("channel_overrides", {})
    structured_channels: dict[str, list[dict]] = {}
    digest_channels: dict[str, list[dict]] = {}

    for username, posts in channel_data.items():
        if overrides.get(username, {}).get("mode") == "structured":
            structured_channels[username] = posts
        else:
            digest_channels[username] = posts

    # Generate the main digest from non-structured channels.
    digest_md = ""
    quick_summary = ""
    if digest_channels:
        log.info("Generating digest with GPT...")
        digest_md, quick_summary = generate_digest(
            digest_channels, channel_labels, date_str, window_days=window_days
        )
        log.info(f"Digest generated ({len(digest_md)} chars).")
        if not (digest_md or "").strip():
            log.warning(
                "Empty digest (0 chars) — usually means the prompt was too big. "
                "Lower TG_DIGEST_CHANNELS_PER_CHUNK or pick a shorter window."
            )
            digest_md = (
                f"# Telegram Digest — {date_str}\n\n"
                f"_The main overview did not generate (empty model response for {len(digest_channels)} channels). "
                f"Structured sections and silent-channels list still follow below._\n"
            )
            quick_summary = (
                f"Telegram digest {date_str}: main overview empty (model limit / error); "
                f"structured sections and channel list still present."
            )
    else:
        digest_md = (
            f"# Telegram Digest — {date_str}\n\n"
            f"No posts in regular channels passed the length filter for this window."
        )
        quick_summary = (
            f"Telegram digest {date_str}: no material in regular channels after length filter."
        )

    # Append structured-mode channel sections.
    for username, posts in structured_channels.items():
        label = channel_labels.get(username, username)
        log.info(f"Generating structured section for @{username} ({len(posts)} posts)...")
        structured_md = generate_structured_section(username, label, posts, date_str)
        digest_md += f"\n\n---\n\n{structured_md}"
        log.info(f"  Structured @{username} done ({len(structured_md)} chars).")

    digest_md = append_silent_channels_footer(digest_md, silent_channels, window_caption)

    # Build the payload that every Output adapter receives.
    payload = DigestPayload(
        markdown=digest_md,
        date_str=date_str,
        title=note_title,
        quick_summary=quick_summary,
    )

    # Topic detection runs only if a NotionOutput is in the chain and the
    # Topics DB is configured. The chosen IDs ride along in payload.extra.
    notion_adapter = next((o for o in outputs if isinstance(o, NotionOutput)), None)
    if args.detect_topics and notion_adapter and NOTION_TOPICS_DB_ID:
        log.info("Detecting topics...")
        try:
            topics_cache = _notion_load_topics(notion_adapter.token, NOTION_TOPICS_DB_ID)
            log.info(f"  Loaded {len(topics_cache)} topics from Notion")
            chosen = detect_topics(quick_summary, topics_cache)
            payload.extra["notion_topic_ids"] = [topics_cache[t] for t in chosen if t in topics_cache]
            log.info(f"  Topics: {chosen or '(none)'}")
        except Exception as e:
            log.warning("Topics detection failed: %s", e)
    elif args.detect_topics:
        log.info("--detect-topics ignored (need a Notion adapter and NOTION_TOPICS_DB_ID).")

    # Image map: only the Notion adapter consumes it. Other adapters just see
    # bare Markdown — that's fine, links in the prompt already point to t.me.
    if not skip_images and notion_adapter:
        image_map: dict[str, str] = {}
        for posts in channel_data.values():
            for p in posts:
                if p.get("image_url") and p.get("url"):
                    image_map[p["url"]] = p["image_url"]
        payload.extra["image_map"] = image_map

    # Fan-out to every configured adapter. A dry run skips persistent writes
    # but still lets stdout fire (so users can preview the digest).
    for out in outputs:
        if args.dry_run and out.name != "stdout":
            log.info("[dry-run] Would write to %s", out.name)
            continue
        try:
            result = out.write(payload)
            if result:
                log.info("  → [%s] %s", out.name, result)
            else:
                log.info("  → [%s] done", out.name)
        except Exception as e:
            log.error("  → [%s] failed: %s", out.name, e)

    log.info("✅ Done.")


if __name__ == "__main__":
    main()
