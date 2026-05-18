# tg-digest

[![PyPI version](https://img.shields.io/pypi/v/tg-digest.svg)](https://pypi.org/project/tg-digest/)
[![Python versions](https://img.shields.io/pypi/pyversions/tg-digest.svg)](https://pypi.org/project/tg-digest/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Daily Markdown digest of your Telegram channels, summarised by an OpenAI chat
model. Reads your subscribed channels via the official MTProto API (Telethon),
batches them into one prompt, and writes a clean Markdown report. Pluggable
output adapters — local file, Notion, Obsidian vault, stdout, or your own.

```
@tldr
- One YAML file lists channels and / or Telegram Folders to track.
- Run once a day (locally or in GitHub Actions) → digest_YYYY-MM-DD.md
- Drop the Markdown into ANY Markdown-aware store via the FileOutput adapter:
  Obsidian, Logseq, Hugo, Jekyll, plain folder — just a path template.
- Optional bundled adapters: Notion (rich blocks), stdout (piping).
- ~2.5k lines of Python, two external deps (telethon, PyYAML).
```

## Why

If you follow 20+ Telegram channels you cannot read them all every day. Most
existing summarisers are bot-based and either (a) require giving a third party
access to your account, or (b) only work on public channels. This script runs
**under your own Telegram identity**, summarises with **your own OpenAI key**,
and stores results in **whatever Markdown system you already use** — no SaaS
in the middle.

## Quick start

```bash
git clone https://github.com/mooreslaws/tg-digest.git
cd tg-digest
pip install -r requirements.txt

# 1. Get Telegram API credentials at https://my.telegram.org
#    (API development tools → "Create application")
cp .env.example .env
$EDITOR .env                       # fill in TELEGRAM_API_ID / TELEGRAM_API_HASH / OPENAI_API_KEY

# 2. One-time auth — creates ./telegram.session
set -a; source .env; set +a
python -m tg_digest.auth

# 3. Configure channels
cp channels.example.yaml channels.yaml
$EDITOR channels.yaml              # list your Telegram Folders and/or @usernames

# 4. Test run (rolling 24h, prints to stdout, no Notion writes)
TG_DIGEST_STDOUT=1 python -m tg_digest.digest --dry-run --hours 24 --rolling

# 5. Real run (default: yesterday's posts → ./reports/digest_*.md)
python -m tg_digest.digest
```

The output lives in `./reports/digest_YYYY-MM-DD.md` (default path,
configurable).

## How it works

```
┌────────────────┐   ┌──────────────┐   ┌───────────────┐   ┌────────────────────┐
│ Telegram       │   │ tg_digest    │   │ OpenAI chat   │   │  Output adapters   │
│ Folders + YAML │ → │ - fetch      │ → │ summariser    │ → │  ┌──────────────┐  │
│ channel list   │   │ - filter     │   │ (one prompt   │   │  │ FileOutput   │  │
│                │   │ - chunk      │   │  per ≤20 ch.) │   │  │ NotionOutput │  │
└────────────────┘   └──────────────┘   └───────────────┘   │  │ StdoutOutput │  │
                                                            │  │ Custom …     │  │
                                                            │  └──────────────┘  │
                                                            └────────────────────┘
```

1. **Channel resolution.** On every run the script reads the current contents of
   the Telegram Folders you listed in `channels.yaml` — adding a channel in the
   Telegram app makes it appear in the next digest automatically.
2. **Fetch.** For each channel, last N messages in the time window (default:
   previous calendar day in your IANA timezone). Posts shorter than
   `min_post_length` are dropped.
3. **Summarise.** Channels are batched into chunks of ≤20 (configurable) and
   sent to one OpenAI chat call per chunk. Format spec lives in
   `tg_digest/prompts/system_en.md` — swap it for your own via env.
4. **Fan-out.** The generated Markdown is handed to every configured Output
   adapter. Adapters are env-driven and idempotent (re-runs update, never
   duplicate).

## Output adapters

A single piece of generated Markdown can fan out to multiple destinations.
Three adapters ship in the box; you can add your own.

### `FileOutput` — any Markdown-aware store

Writes the digest to a path with `{date}` (and optionally `{year}`, `{month}`,
`{day}`, `{title}`) placeholders. This single adapter covers most workflows:

| Workflow | `TG_DIGEST_OUTPUT_PATH` |
|---|---|
| Local archive (default) | `./reports/digest_{date}.md` |
| Obsidian vault | `~/Vault/Telegram/digest_{date}.md` |
| Logseq journal | `~/logseq/journals/{date}.md` |
| Hugo site | `./content/digest/{date}/index.md` |
| Jekyll site | `./_posts/{date}-digest.md` |
| Year/month buckets | `./reports/{year}/{month}/{date}.md` |

For static site generators or Obsidian Dataview / Templater you usually want
YAML frontmatter — set `TG_DIGEST_FRONTMATTER` to a JSON object:

```bash
export TG_DIGEST_FRONTMATTER='{"title":"Telegram Digest {date}","tags":["digest","telegram"],"date":"{date}"}'
```

Want multiple file outputs at once (e.g. local archive **and** Obsidian
vault)? Set `TG_DIGEST_OUTPUT_PATH_2` / `TG_DIGEST_OUTPUT_PATH_3` (with
matching `TG_DIGEST_FRONTMATTER_2` / `_3` if needed).

### `NotionOutput` — rich Notion blocks

Active when both `NOTION_TOKEN` and `NOTION_NOTES_DB_ID` are set. Converts the
Markdown into native Notion blocks (headings, paragraphs, dividers, image
embeds) and creates / updates a page per digest date. Property names are
configurable to match your existing DB schema — see [`docs/notion.md`](docs/notion.md).

### `StdoutOutput` — pipe to anything

Set `TG_DIGEST_STDOUT=1` and the Markdown is also printed to stdout. Useful
for `tg-digest | mail`, `tg-digest | gh gist create`, or just previewing.

### Disable the default local output

Set `TG_DIGEST_NO_LOCAL=1` if you only want, say, Notion — and never any local
file.

### Custom adapters

Subclass `tg_digest.outputs.base.Output` (one method: `write(payload)`),
register it via `TG_DIGEST_EXTRA_OUTPUTS=my_module:MyOutput`. The package
imports the module, instantiates the class (using its `from_env()` if defined),
and adds it to the fan-out chain.

```python
# my_outputs.py
from tg_digest.outputs.base import DigestPayload, Output

class WebhookOutput(Output):
    name = "webhook"
    @classmethod
    def from_env(cls):
        import os
        url = os.environ.get("MY_WEBHOOK_URL")
        return cls(url) if url else None
    def __init__(self, url):
        self.url = url
    def write(self, payload: DigestPayload) -> str | None:
        # POST payload.markdown to self.url; return URL for logging
        ...
```

```bash
TG_DIGEST_EXTRA_OUTPUTS=my_outputs:WebhookOutput MY_WEBHOOK_URL=... python -m tg_digest.digest
```

## Configuration

All config is via environment variables (see `.env.example`) plus the YAML
channel list.

### Channels (`channels.yaml`)

```yaml
defaults:
  hours_back: 24
  min_post_length: 150

folders:
  - News             # name of a Telegram Folder in your app

channels:            # optional explicit list
  - username: durov
    label: "Pavel Durov"
```

See `channels.example.yaml` for the full schema including `channel_overrides`
for per-channel `mode: structured` (extracts tabular data from
deal-flow-style feeds).

### Telegram session

Two options for the session:

1. **Local file** (default): `python -m tg_digest.auth` creates `./telegram.session`.
2. **String session** (recommended for CI): `python -m tg_digest.auth` prints
   a single-line `TELEGRAM_SESSION_STRING` at the end. Use that as your
   GitHub Actions secret.

⚠️ Both forms grant full read access to your Telegram account. Treat them like
a password.

### Language

The default system prompt is English (`tg_digest/prompts/system_en.md`).
Switch to Russian with `TG_DIGEST_LANG=ru`, or supply your own with
`TG_DIGEST_SYSTEM_PROMPT_FILE=/path/to/my_prompt.md`.

## Running in GitHub Actions

A ready-to-use workflow lives at `.github/workflows/digest.yml`. It runs
daily, builds the digest, and uploads `reports/` as a workflow artifact.

Set these repo secrets:

| Secret | Required | What it is |
|---|---|---|
| `TELEGRAM_API_ID` | yes | from my.telegram.org |
| `TELEGRAM_API_HASH` | yes | from my.telegram.org |
| `TELEGRAM_SESSION_STRING` | yes | printed by `python -m tg_digest.auth` |
| `OPENAI_API_KEY` | yes | OpenAI API key |
| `NOTION_TOKEN` | no | enables Notion adapter |
| `NOTION_NOTES_DB_ID` | no | enables Notion adapter |

See [`docs/github-actions.md`](docs/github-actions.md) for the full walkthrough.

## CLI reference

```bash
# Main digest
python -m tg_digest.digest [--dry-run] [--date YYYY-MM-DD] [--days N]
                           [--hours N --rolling] [--folder Name] [--channel name]
                           [--with-images] [--detect-topics]

# One-time session setup
python -m tg_digest.auth

# Manage channels.yaml
python -m tg_digest.channels list
python -m tg_digest.channels add @username "Label"
python -m tg_digest.channels remove @username
python -m tg_digest.channels run [...digest args]
```

`--dry-run` fetches and summarises but skips persistent writes (still prints
to stdout if `TG_DIGEST_STDOUT=1`).

## Cost

For ~25 channels × ~3 posts each × `gpt-4o-mini` a typical daily run costs
**a few cents** in OpenAI tokens. Set `OPENAI_CHAT_MODEL` to choose a
different model.

## Privacy

- The script runs entirely client-side under your own credentials.
- Message text is sent to OpenAI for summarisation (this is how the digest
  is generated). If you have channels with sensitive content, exclude them
  from your YAML / Folder.
- No telemetry, no analytics, no third-party servers besides Telegram,
  OpenAI, and whatever destinations you configure.

## Roadmap

- [ ] More built-in adapters (Slack, email, S3, webhook)
- [ ] Per-channel custom prompts
- [ ] Tests
- [ ] PyPI release

## License

MIT — see [`LICENSE`](LICENSE).
