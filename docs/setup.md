# Setup walkthrough

End-to-end first-run guide. Assumes a fresh machine with Python 3.10+ installed.

## 1. Get Telegram API credentials

Telegram requires every MTProto client to register for an API ID / hash. Yours is
tied to your phone number and free.

1. Go to <https://my.telegram.org> and log in with your phone.
2. Click **API development tools**.
3. Fill in the form. The values don't matter much — pick something like:
   - App title: `tg-digest`
   - Short name: `tg-digest`
   - Platform: Other
4. Click **Create application**.
5. Copy `App api_id` and `App api_hash`. These are the values for
   `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`.

⚠️ Anyone with your API ID / hash plus a session can read your account. Don't
commit them or paste them anywhere shared. Use a `.env` file (gitignored).

## 2. Get an OpenAI key

1. Go to <https://platform.openai.com/api-keys>.
2. Click **Create new secret key**.
3. Copy it into `OPENAI_API_KEY`.

If you've never used OpenAI before you'll need to add a payment method. The
default model (`gpt-4o-mini`) costs cents per digest run.

## 3. Install + configure

```bash
git clone https://github.com/CHANGEME/tg-digest.git
cd tg-digest
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
$EDITOR .env                       # paste the three values from steps 1+2
```

## 4. Authenticate

```bash
set -a; source .env; set +a
python -m tg_digest.auth
```

The first time, you'll get:

1. A prompt for your phone number — same as you signed up to Telegram with,
   in international format (e.g. `+15551234567`).
2. A code via Telegram (look in the official Telegram app's "Telegram" service
   chat — it's NOT an SMS unless you have no other devices logged in).
3. If you have 2FA enabled, your Telegram cloud password.

On success the script:

- Saves `./telegram.session` for future runs.
- Prints a `TELEGRAM_SESSION_STRING` — copy it into a secure place if you plan
  to run in CI.

## 5. Set up channels

You can either use **Telegram Folders** (recommended — composition is live) or
list channels explicitly.

### Option A: Telegram Folders

1. In the Telegram app, create a folder (e.g. `News`).
2. Add the channels you want to digest to that folder.
3. In `channels.yaml`:

   ```yaml
   folders:
     - News
   ```

Adding / removing channels in the folder later requires no code change.

### Option B: Explicit list

```yaml
channels:
  - username: durov
    label: "Pavel Durov"
  - username: telegram
    label: "Telegram News"
```

## 6. First run

```bash
# Dry run: fetch + summarise, write to ./reports/, no Notion.
python -m tg_digest.digest --dry-run --hours 24 --rolling
```

Open `./reports/digest_*.md` to see the output.

If it looks good, switch to the default "calendar day" mode (digest for date D
covers D−1 00:00–24:00 in your timezone) by running without flags:

```bash
python -m tg_digest.digest --dry-run
```

## Troubleshooting

- **`Session file not found`** — you didn't run `python -m tg_digest.auth` yet, or
  the `TELEGRAM_SESSION` env var points somewhere else.
- **`FloodWaitError`** — Telegram is rate-limiting you. The script will tell you
  how long to wait. This typically happens on the first few runs when entities
  haven't been cached yet.
- **`OpenAI HTTP 401`** — your `OPENAI_API_KEY` is wrong or your account has no
  payment method.
- **Empty digest** — likely all posts were under `min_post_length` (default 150
  chars). Lower it in `channels.yaml`.
- **`Folder X: 0 channels`** — the folder name is case-sensitive and must match
  the Telegram app exactly. Whitespace counts.
