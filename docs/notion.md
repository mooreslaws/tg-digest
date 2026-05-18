# Notion export

Notion export is **fully optional**. Without `NOTION_NOTES_DB_ID` the script
only writes the local Markdown report. This guide walks through enabling it.

## Why bother

The Markdown report is plenty if you read it locally or commit it to a repo.
Notion makes sense if you want:

- Searchable archive across digests (`Quick search` finds any keyword in any
  past page).
- A shared family / team digest.
- To pipe digests into downstream automations that already live in Notion.

## Set up the integration

1. Go to <https://www.notion.so/profile/integrations>.
2. Click **New integration**, give it a name like `tg-digest`, pick the
   workspace, click **Save**.
3. Copy the **Internal Integration Token** — this is `NOTION_TOKEN`.

## Set up the database

The simplest possible DB works: just one column called `Title`.

1. In Notion, create a new database (full page or inline — your call).
2. Make sure it has a title column. The default name is `Title` — leave it
   that way, or set `NOTION_TITLE_PROP` to the actual name.
3. In the top-right `•••` menu → **Connections** → add your `tg-digest`
   integration.
4. Copy the database ID from the URL. The URL looks like:

   ```
   https://www.notion.so/your-workspace/abcdef1234567890abcdef1234567890?v=...
                                          ─────── this 32-char hex string ───────
   ```

   That string is `NOTION_NOTES_DB_ID`.

## Configure

In `.env`:

```ini
NOTION_TOKEN=secret_abc123...
NOTION_NOTES_DB_ID=abcdef1234567890abcdef1234567890
```

That's it. Run the digest and it should now create a page per day.

## Optional columns

If you want more than just the title:

| Env var | Notion column type | What it does |
|---|---|---|
| `NOTION_SUMMARY_PROP` | rich_text | One-line summary (≤ 2000 chars) |
| `NOTION_TOPICS_RELATION_PROP` | relation | Auto-tag via `--detect-topics` |
| `NOTION_TOPICS_DB_ID` | (separate DB) | Topics DB referenced by the relation |

Example:

```ini
NOTION_SUMMARY_PROP=Quick Summary
NOTION_TOPICS_RELATION_PROP=Topics
NOTION_TOPICS_DB_ID=abcdef1234567890abcdef1234567890
```

The Topics DB just needs a title column called `Name` listing the topics you
want the model to choose from.

## Idempotency

The script looks for an existing page with the same title (`Telegram Digest —
YYYY-MM-DD`) in your DB. If found, it **replaces the content blocks** of that
page; otherwise it creates a new page. Safe to re-run.

## Troubleshooting

- **`HTTP 404`** — your integration is not connected to the database. Open the
  DB → `•••` → Connections → add the integration.
- **`HTTP 400 properties.X is not a property that exists`** — your DB doesn't
  have a column called `X` (the default is `Title`). Either rename your column
  to match or set `NOTION_TITLE_PROP`/`NOTION_SUMMARY_PROP`/etc.
- **Long digests get truncated** — Notion limits a single API call to 100
  child blocks. The script already paginates, but if you see truncation,
  open an issue with the digest size.
