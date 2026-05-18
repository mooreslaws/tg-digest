"""
Output adapters — write a generated digest somewhere persistent.

Built-ins: :class:`tg_digest.outputs.local.FileOutput`,
:class:`tg_digest.outputs.notion.NotionOutput`,
:class:`tg_digest.outputs.stdout.StdoutOutput`.

All adapters share one interface (:class:`tg_digest.outputs.base.Output`),
configured by environment variables:

* **Local file** (always on unless ``TG_DIGEST_NO_LOCAL=1``).
  Default path ``./reports/digest_{date}.md`` — override with
  ``TG_DIGEST_OUTPUT_PATH``. Optional YAML frontmatter via
  ``TG_DIGEST_FRONTMATTER`` (JSON object).
  Add up to two extra file outputs via
  ``TG_DIGEST_OUTPUT_PATH_2`` / ``TG_DIGEST_OUTPUT_PATH_3``
  (each with optional ``..._FRONTMATTER_2``/``_3``).

* **Notion** (active when both ``NOTION_TOKEN`` and ``NOTION_NOTES_DB_ID``
  are set). See :class:`tg_digest.outputs.notion.NotionOutput` for the full
  list of ``NOTION_*_PROP`` knobs.

* **Stdout** (active when ``TG_DIGEST_STDOUT=1``).

* **Third-party adapters** — set ``TG_DIGEST_EXTRA_OUTPUTS`` to a comma-
  separated list of ``module.path:ClassName`` references. Each class must
  subclass :class:`Output` and have a no-arg ``__init__`` or a classmethod
  ``from_env`` returning an instance (or ``None`` to opt out at runtime).
"""

from __future__ import annotations

import importlib
import logging
import os
from typing import Iterable

from .base import DigestPayload, Output
from .local import FileOutput
from .notion import NotionOutput, load_topics_cache
from .stdout import StdoutOutput

log = logging.getLogger("tg_digest.outputs")


def _env_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _load_extra_outputs() -> list[Output]:
    """Instantiate user-defined Output subclasses from TG_DIGEST_EXTRA_OUTPUTS."""
    raw = (os.environ.get("TG_DIGEST_EXTRA_OUTPUTS") or "").strip()
    if not raw:
        return []

    extras: list[Output] = []
    for ref in [r.strip() for r in raw.split(",") if r.strip()]:
        try:
            mod_path, _, cls_name = ref.partition(":")
            if not mod_path or not cls_name:
                log.warning("TG_DIGEST_EXTRA_OUTPUTS: bad reference %r (expected module:Class)", ref)
                continue
            mod = importlib.import_module(mod_path)
            cls = getattr(mod, cls_name)
            instance = cls.from_env() if hasattr(cls, "from_env") else cls()
            if instance is None:
                log.info("Extra output %s: from_env() returned None, skipping.", ref)
                continue
            extras.append(instance)
            log.info("Loaded extra output: %s", ref)
        except Exception as e:
            log.warning("Failed to load extra output %r: %s", ref, e)
    return extras


def collect_outputs() -> list[Output]:
    """Build the list of Output instances from current environment variables."""
    outputs: list[Output] = []

    # 1. Local file output — on by default, can be disabled or re-configured.
    if not _env_truthy("TG_DIGEST_NO_LOCAL"):
        primary_path = (os.environ.get("TG_DIGEST_OUTPUT_PATH") or "./reports/digest_{date}.md").strip()
        primary = FileOutput.from_env(
            env_path="TG_DIGEST_OUTPUT_PATH",
            env_frontmatter="TG_DIGEST_FRONTMATTER",
            name="local",
        )
        # FileOutput.from_env returns None only when the env var is empty;
        # we set a default fallback, so build directly if needed.
        if primary is None:
            primary = FileOutput(primary_path, name="local")
        outputs.append(primary)

    # 1b. Up to two additional FileOutputs — for users who want digest fanned out to
    #     multiple Markdown stores at once (e.g. local archive + Obsidian vault).
    for idx in (2, 3):
        extra_file = FileOutput.from_env(
            env_path=f"TG_DIGEST_OUTPUT_PATH_{idx}",
            env_frontmatter=f"TG_DIGEST_FRONTMATTER_{idx}",
            name=f"local-{idx}",
        )
        if extra_file is not None:
            outputs.append(extra_file)

    # 2. Notion — only if both token and DB ID are set.
    notion = NotionOutput.from_env()
    if notion is not None:
        outputs.append(notion)

    # 3. Stdout — opt-in.
    if _env_truthy("TG_DIGEST_STDOUT"):
        outputs.append(StdoutOutput())

    # 4. Third-party adapters.
    outputs.extend(_load_extra_outputs())

    return outputs


__all__ = [
    "DigestPayload",
    "FileOutput",
    "NotionOutput",
    "Output",
    "StdoutOutput",
    "collect_outputs",
    "load_topics_cache",
]
