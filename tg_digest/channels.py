"""
Channels CLI — add, remove, list configured channels and trigger the digest.

Usage:
  python -m tg_digest.channels list
  python -m tg_digest.channels add @channel_name "Display Label"
  python -m tg_digest.channels remove @channel_name
  python -m tg_digest.channels run [--dry-run] [--hours N] [...digest args]

The config file lives at ./channels.yaml (override via
TELEGRAM_DIGEST_CHANNELS_CONFIG env var).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


CHANNELS_CONFIG = Path(
    os.environ.get("TELEGRAM_DIGEST_CHANNELS_CONFIG") or str(Path.cwd() / "channels.yaml")
)


# ── YAML read/write helpers (no PyYAML dependency for add/remove) ──────────────

def _read_yaml_lines() -> list[str]:
    if not CHANNELS_CONFIG.exists():
        return []
    return CHANNELS_CONFIG.read_text(encoding="utf-8").splitlines()


def _write_yaml_lines(lines: list[str]) -> None:
    CHANNELS_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CHANNELS_CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_channels(lines: list[str]) -> list[dict]:
    """Parse the `channels:` section into a list of dicts."""
    channels: list[dict] = []
    current: dict = {}
    in_channels = False

    for line in lines:
        stripped = line.strip()
        if stripped == "channels:":
            in_channels = True
        elif in_channels:
            if stripped.startswith("- username:"):
                if current:
                    channels.append(current)
                current = {"username": stripped.split(":", 1)[1].strip().strip('"')}
            elif stripped.startswith("label:") and current:
                current["label"] = stripped.split(":", 1)[1].strip().strip('"')
            elif (
                not stripped.startswith("-")
                and not stripped.startswith("#")
                and stripped
                and not stripped.startswith("username:")
                and not stripped.startswith("label:")
            ):
                # New top-level key — left the channels block.
                if current:
                    channels.append(current)
                    current = {}
                in_channels = False

    if current:
        channels.append(current)

    return channels


# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_list() -> None:
    lines = _read_yaml_lines()
    channels = _parse_channels(lines)

    if not channels:
        print("No channels configured yet.")
        print(f"\nConfig: {CHANNELS_CONFIG}")
        print("\nAdd one with:")
        print("  python -m tg_digest.channels add @channel_name \"Label\"")
        return

    print(f"Configured channels ({len(channels)}):  config={CHANNELS_CONFIG}\n")
    for i, ch in enumerate(channels, 1):
        username = ch.get("username", "")
        label = ch.get("label", username)
        print(f"  {i:2}. @{username:<30} {label}")


def cmd_add(username: str, label: str) -> None:
    username = username.lstrip("@")

    lines = _read_yaml_lines()
    channels = _parse_channels(lines)

    existing = [c.get("username", "").lstrip("@") for c in channels]
    if username in existing:
        print(f"Channel @{username} is already in the list.")
        return

    new_entry = [
        f"  - username: {username}",
        f'    label: "{label}"',
    ]

    if not any(l.strip() == "channels:" for l in lines):
        lines.append("")
        lines.append("channels:")

    # Insert after the last entry in `channels:` (or right after the header).
    insert_idx = None
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith("- username:") or stripped.startswith("label:"):
            insert_idx = i + 1
            break
        if stripped == "channels:":
            insert_idx = i + 1
            break

    if insert_idx is None:
        lines.extend(new_entry)
    else:
        for j, entry_line in enumerate(new_entry):
            lines.insert(insert_idx + j, entry_line)

    _write_yaml_lines(lines)
    print(f"✅ Added @{username} — {label}")
    print(f"   Config: {CHANNELS_CONFIG}")


def cmd_remove(username: str) -> None:
    username = username.lstrip("@")

    lines = _read_yaml_lines()
    new_lines: list[str] = []
    found = False

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped == f"- username: {username}" or stripped == f'- username: "{username}"':
            found = True
            i += 1
            while i < len(lines) and lines[i].strip().startswith("label:"):
                i += 1
            continue

        new_lines.append(line)
        i += 1

    if not found:
        print(f"Channel @{username} not found in config.")
        return

    _write_yaml_lines(new_lines)
    print(f"✅ Removed @{username}")
    print(f"   Config: {CHANNELS_CONFIG}")


def cmd_run(extra_args: list[str]) -> None:
    """Trigger `python -m tg_digest.digest` with forwarded arguments."""
    cmd = [sys.executable, "-m", "tg_digest.digest"] + extra_args
    print(f"Running: {' '.join(cmd)}\n")
    result = subprocess.run(cmd)
    sys.exit(result.returncode)


# ── Entrypoint ─────────────────────────────────────────────────────────────────

def print_help() -> None:
    print(__doc__)


def main() -> None:
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print_help()
        return

    command = args[0]

    if command == "list":
        cmd_list()
    elif command == "add":
        if len(args) < 2:
            print('Usage: python -m tg_digest.channels add @username "Label"')
            sys.exit(1)
        username = args[1]
        label = args[2] if len(args) > 2 else username.lstrip("@")
        cmd_add(username, label)
    elif command == "remove":
        if len(args) < 2:
            print("Usage: python -m tg_digest.channels remove @username")
            sys.exit(1)
        cmd_remove(args[1])
    elif command == "run":
        cmd_run(args[1:])
    else:
        print(f"Unknown command: {command!r}")
        print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
