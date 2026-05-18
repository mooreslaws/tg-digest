# Output adapters

Every digest run fans out to one or more **Output adapters** — small classes
that take the generated Markdown plus metadata and persist it somewhere.

Three adapters ship in the box:

- [`FileOutput`](#fileoutput) — write to a path template (covers Obsidian,
  Logseq, Hugo, Jekyll, plain folder…).
- [`NotionOutput`](#notionoutput) — create / update a page in a Notion DB.
- [`StdoutOutput`](#stdoutoutput) — print the Markdown to stdout.

Plus a [plug-in mechanism](#custom-adapters) for your own.

If no adapter is configured the script refuses to run. By default the local
`FileOutput` is always on; disable it with `TG_DIGEST_NO_LOCAL=1`.

## `FileOutput`

```python
from tg_digest.outputs.local import FileOutput

FileOutput(
    path_template="./reports/digest_{date}.md",
    frontmatter={"tags": ["digest", "telegram"]},
    name="local",
)
```

Placeholders supported in the path template:

| Placeholder | Replaced with |
|---|---|
| `{date}` / `{iso_date}` | `YYYY-MM-DD` of the digest |
| `{year}` | 4-digit year |
| `{month}` | 2-digit month |
| `{day}` | 2-digit day |
| `{title}` | slugified digest title |

`~` is expanded to the home directory. Missing parent directories are created.

### Environment

| Var | Default | Notes |
|---|---|---|
| `TG_DIGEST_OUTPUT_PATH` | `./reports/digest_{date}.md` | primary local file |
| `TG_DIGEST_FRONTMATTER` | _(none)_ | JSON object prepended as YAML frontmatter |
| `TG_DIGEST_OUTPUT_PATH_2` | _(none)_ | second file output |
| `TG_DIGEST_FRONTMATTER_2` | _(none)_ | frontmatter for it |
| `TG_DIGEST_OUTPUT_PATH_3` | _(none)_ | third file output |
| `TG_DIGEST_FRONTMATTER_3` | _(none)_ | frontmatter for it |
| `TG_DIGEST_NO_LOCAL` | _unset_ | set to `1` to skip the default local output entirely |

### Cookbook

**Obsidian vault:**

```bash
export TG_DIGEST_OUTPUT_PATH='~/Vault/Telegram/digest_{date}.md'
export TG_DIGEST_FRONTMATTER='{"type":"telegram-digest","aliases":["TG {date}"],"tags":["digest","telegram"]}'
```

**Logseq daily journal:**

```bash
export TG_DIGEST_OUTPUT_PATH='~/logseq/journals/{date}.md'
```

**Hugo blog post:**

```bash
export TG_DIGEST_OUTPUT_PATH='./content/digest/{date}/index.md'
export TG_DIGEST_FRONTMATTER='{"title":"Telegram Digest {date}","date":"{date}","draft":false}'
```

**Local archive + Obsidian vault simultaneously:**

```bash
# default TG_DIGEST_OUTPUT_PATH keeps the local archive
export TG_DIGEST_OUTPUT_PATH_2='~/Vault/Inbox/digest_{date}.md'
export TG_DIGEST_FRONTMATTER_2='{"type":"inbox","date":"{date}"}'
```

## `NotionOutput`

```python
from tg_digest.outputs.notion import NotionOutput

NotionOutput(
    token="secret_...",
    notes_db_id="abcdef1234567890abcdef1234567890",
    title_prop="Title",
    summary_prop="",                # optional rich_text column
    topics_relation_prop="",        # optional relation column
)
```

Converts the digest Markdown to native Notion blocks. Idempotent: re-running
on the same date updates the existing page rather than creating a duplicate.

See [notion.md](notion.md) for the full setup walkthrough.

## `StdoutOutput`

```python
from tg_digest.outputs.stdout import StdoutOutput

StdoutOutput()
```

Prints the Markdown to stdout. Activate with `TG_DIGEST_STDOUT=1`. Useful for:

```bash
TG_DIGEST_NO_LOCAL=1 TG_DIGEST_STDOUT=1 python -m tg_digest.digest | mail -s "TG digest" me@example.com
TG_DIGEST_STDOUT=1 python -m tg_digest.digest | gh gist create -f digest.md
```

## Custom adapters

Implement one method (`write`), optionally a classmethod (`from_env`), then
register the class via env var.

```python
# my_outputs.py
import os
from tg_digest.outputs.base import DigestPayload, Output

class WebhookOutput(Output):
    name = "webhook"

    def __init__(self, url: str):
        self.url = url

    @classmethod
    def from_env(cls):
        url = os.environ.get("MY_WEBHOOK_URL", "").strip()
        return cls(url) if url else None

    def write(self, payload: DigestPayload) -> str | None:
        import json, urllib.request
        body = json.dumps({"date": payload.date_str, "markdown": payload.markdown}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30)
        return self.url
```

```bash
export TG_DIGEST_EXTRA_OUTPUTS='my_outputs:WebhookOutput'
export MY_WEBHOOK_URL='https://hooks.example.com/abc123'
python -m tg_digest.digest
```

The runtime imports `my_outputs`, calls `WebhookOutput.from_env()`, and adds
the returned instance to the fan-out chain. Return `None` from `from_env` to
opt out at runtime (e.g. if a required env var is missing).

You can register multiple classes by comma-separating them:

```bash
TG_DIGEST_EXTRA_OUTPUTS='my_outputs:WebhookOutput,my_outputs:S3Output'
```

## Idempotency

Every adapter must be safe to re-run on the same date. The built-ins are:

- `FileOutput` overwrites the file at the same path.
- `NotionOutput` looks up an existing page by title and updates it.
- `StdoutOutput` is trivially safe.

Make your custom adapters idempotent the same way — match on `payload.date_str`
or `payload.title`, then update.

## DigestPayload reference

Every adapter's `write()` method receives a `DigestPayload`:

```python
@dataclass
class DigestPayload:
    markdown: str           # rendered Markdown
    date_str: str           # "YYYY-MM-DD" — the day the digest is FOR
    title: str              # e.g. "Telegram Digest — 2026-05-18"
    quick_summary: str      # 1-2 sentences, may be empty
    extra: dict             # implementation hints, optional
```

The `extra` dict may carry:

- `extra["image_map"]` — `{post_url: image_url}` for adapters that can embed
  images (Notion does).
- `extra["notion_topic_ids"]` — list of Notion page IDs returned by
  `--detect-topics`.

Adapters MUST tolerate unknown keys — `payload.extra.get("anything", default)`.
