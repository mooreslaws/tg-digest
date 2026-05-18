# Running in GitHub Actions

The included workflow at `.github/workflows/digest.yml` runs the digest on a
daily cron and uploads the report as a workflow artifact. No persistent server
required.

## Prerequisites

You need:

- The repo pushed to GitHub (public or private, both work).
- All four required secrets set (see below).
- A `channels.yaml` committed to the repo (it's in `.gitignore` by default to
  protect against accidental commits; remove that line once you're happy with
  your config).

## Required secrets

Set these at **Settings → Secrets and variables → Actions → New repository secret**.

| Name | Value |
|---|---|
| `TELEGRAM_API_ID` | from my.telegram.org |
| `TELEGRAM_API_HASH` | from my.telegram.org |
| `TELEGRAM_SESSION_STRING` | the long string printed by `python -m tg_digest.auth` |
| `OPENAI_API_KEY` | OpenAI API key |

Optional (only set if you want Notion export):

| Name | Value |
|---|---|
| `NOTION_TOKEN` | integration secret |
| `NOTION_NOTES_DB_ID` | 32-char hex DB ID |

## Get a `TELEGRAM_SESSION_STRING`

Locally, after the one-time auth:

```bash
set -a; source .env; set +a
python -m tg_digest.auth
```

If you're already logged in it just re-prints the string at the end. Copy that
single line into the `TELEGRAM_SESSION_STRING` repo secret.

⚠️ Treat this string like your Telegram password. Anyone who gets it can read
your messages. If it ever leaks: terminate the session from Telegram's **Active
sessions** screen and re-run auth.

## Schedule

The default cron is `0 6 * * *` — every day at 06:00 UTC. Edit
`.github/workflows/digest.yml` to change it:

```yaml
on:
  schedule:
    - cron: "0 6 * * *"   # 06:00 UTC daily
```

Note: GitHub free tier doesn't guarantee on-time cron execution — runs may be
delayed by 5–30 minutes during peak hours.

## Finding the output

After a run completes (Actions tab → Telegram digest workflow → click the run):

- **Logs**: the `Run digest` step prints the report path and a Notion URL
  (if enabled).
- **Artifact**: at the bottom of the run page, download `digest-<run-id>.zip`.
  It contains everything under `reports/`.
- **Notion**: if configured, the page appears in your DB.

## Secret rotation

If you ever suspect a secret has leaked:

1. **Telegram session** — go to the official Telegram app → Settings → Devices
   → terminate the session. Then re-run `python -m tg_digest.auth` locally and
   update `TELEGRAM_SESSION_STRING`.
2. **OpenAI key** — revoke at <https://platform.openai.com/api-keys>, create a
   new one, update `OPENAI_API_KEY`.
3. **Notion token** — at <https://www.notion.so/profile/integrations>, rotate
   the integration's secret.
4. **Telegram API ID / hash** — these are tied to your phone number and not
   easily rotated. They're less sensitive than the session — alone they can't
   read your messages.
