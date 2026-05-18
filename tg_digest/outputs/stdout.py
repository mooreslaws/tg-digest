"""Stdout output — print the digest Markdown to stdout. Useful for piping."""

from __future__ import annotations

import sys

from .base import DigestPayload, Output


class StdoutOutput(Output):
    name = "stdout"

    def write(self, payload: DigestPayload) -> str | None:
        sys.stdout.write(payload.markdown)
        if not payload.markdown.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
        return None
