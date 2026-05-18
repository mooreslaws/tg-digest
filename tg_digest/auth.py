"""
Telegram Auth — one-time session setup for tg_digest.

Run this script once to authenticate with your Telegram account. It creates a
.session file that the digest script uses for all subsequent runs (no phone /
code needed again).

Usage:
  python -m tg_digest.auth

Env vars required:
  TELEGRAM_API_ID   — from https://my.telegram.org → API development tools
  TELEGRAM_API_HASH — from https://my.telegram.org → API development tools

Optional:
  TELEGRAM_SESSION  — path to save session file (default: ./telegram.session)

After login, the script prints a TELEGRAM_SESSION_STRING — paste it into a
GitHub Actions secret (or any CI / serverless platform) for cloud runs.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

try:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession
except ImportError:
    print("ERROR: Telethon is not installed.")
    print("Run: pip install telethon")
    sys.exit(1)

# ── Config ─────────────────────────────────────────────────────────────────────

SESSION_FILE = Path(
    os.environ.get("TELEGRAM_SESSION") or str(Path.cwd() / "telegram.session")
)


def get_credentials() -> tuple[int, str]:
    api_id_str = os.environ.get("TELEGRAM_API_ID", "")
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")

    if not api_id_str or not api_hash:
        print("\nTELEGRAM_API_ID and TELEGRAM_API_HASH are not set in env.")
        print("Get them at: https://my.telegram.org → API development tools\n")

        if not api_id_str:
            api_id_str = input("Enter your TELEGRAM_API_ID (number): ").strip()
        if not api_hash:
            api_hash = input("Enter your TELEGRAM_API_HASH: ").strip()

    try:
        api_id = int(api_id_str)
    except ValueError:
        print(f"ERROR: TELEGRAM_API_ID must be a number, got: {api_id_str!r}")
        sys.exit(1)

    return api_id, api_hash


async def auth() -> None:
    api_id, api_hash = get_credentials()

    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    session_path = str(SESSION_FILE)

    print(f"\nSession will be saved to: {session_path}")
    print("You will receive a login code via Telegram (or SMS).\n")

    async with TelegramClient(session_path, api_id, api_hash) as client:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"\n✅ Already authenticated as: {me.first_name} (@{me.username})")
            print(f"   Session file: {SESSION_FILE}")
            _print_string_session_hint(client)
            return

        phone = input("Enter your phone number (e.g. +1234567890): ").strip()
        await client.send_code_request(phone)

        code = input("Enter the verification code you received: ").strip()

        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            # 2FA is enabled
            password = input(
                "Two-factor authentication is enabled. Enter your password: "
            ).strip()
            await client.sign_in(password=password)

        me = await client.get_me()
        print(f"\n✅ Successfully authenticated as: {me.first_name} (@{me.username})")
        print(f"   Session saved to: {SESSION_FILE}")
        _print_string_session_hint(client)
        print("\nYou can now run:")
        print("  python -m tg_digest.digest --dry-run")


def _print_string_session_hint(client: "TelegramClient") -> None:
    """One-line session for GitHub Actions / TELEGRAM_SESSION_STRING."""
    try:
        s = StringSession.save(client.session)
        print("\n--- GitHub Actions (optional) ---")
        print(
            "Create repo secret TELEGRAM_SESSION_STRING with this single line "
            "(treat as a password — anyone with this can read your messages):"
        )
        print(s)
    except Exception as e:
        print(f"\n(could not export string session: {e})")


def main() -> None:
    asyncio.run(auth())


if __name__ == "__main__":
    main()
