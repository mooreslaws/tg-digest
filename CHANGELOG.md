# Changelog

All notable changes to **tg-digest** are documented here. Versions follow
[Semantic Versioning](https://semver.org/) once the project leaves 0.x.

## [0.2.0] — 2026-05-18

First public release. Extracted and genericised from a personal monorepo.

### Added
- **Pluggable output adapter system** (`tg_digest.outputs`):
  - `FileOutput` with `{date}` / `{year}` / `{month}` / `{day}` / `{title}` placeholders
    — covers Obsidian, Logseq, Hugo, Jekyll, and any other path-based store
  - `NotionOutput` — idempotent page-per-date in a Notion database
  - `StdoutOutput` — pipe-friendly Markdown output
  - Third-party adapter loading via `TG_DIGEST_EXTRA_OUTPUTS=module:Class`
- Up to three parallel file outputs via `TG_DIGEST_OUTPUT_PATH` / `_2` / `_3`,
  each with optional YAML frontmatter (`TG_DIGEST_FRONTMATTER` / `_2` / `_3`)
- Built-in **bilingual prompts** (`tg_digest/prompts/system_{en,ru}.md`).
  English is the default; opt into Russian via `TG_DIGEST_LANG=ru`.
- `TG_DIGEST_SYSTEM_PROMPT_FILE` / `TG_DIGEST_STRUCTURED_PROMPT_FILE` for
  arbitrary custom prompts.
- Per-channel `mode: structured` for high-volume / tabular feeds (deal flow,
  job listings, product launches, …). Customise via
  `TG_DIGEST_STRUCTURED_PROMPT_FILE`.
- GitHub Actions workflow template at `.github/workflows/digest.yml`.
- CLI entrypoints: `tg-digest`, `tg-digest-auth`, `tg-digest-channels`.
- Topic detection (`--detect-topics`) — only meaningful with a configured
  `NotionOutput` and `NOTION_TOPICS_DB_ID`.

### Changed
- Format spec moved entirely into the system prompt; user prompts now carry
  only data. Switching language is a one-env-var change.
- All Telegram-digest-specific env vars renamed `TELEGRAM_DIGEST_*` → `TG_DIGEST_*`.
  Telegram client config (`TELEGRAM_API_*`, `TELEGRAM_SESSION*`) keeps its
  original prefix.
- Notion is now an optional adapter, not a required output target. The script
  no longer aborts when Notion env vars are missing.

### Removed
- Hardcoded Notion database IDs.
- Personal artefacts (folder names, channel handles, audience framing,
  internal debug log paths). The default `channels.example.yaml` uses a
  generic `News` folder placeholder.
- Inline `SYSTEM_PROMPT` / `ANALYTICS_SYSTEM_PROMPT` strings — replaced by
  Markdown files under `tg_digest/prompts/`.
