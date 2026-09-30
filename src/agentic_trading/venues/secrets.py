"""Broker credentials: loaded locally, never shown whole anywhere.

Keys come from ``config/secrets.toml`` (git-ignored, and refused unless only its
owner can read it) or from the environment. A file entry wins over an
environment entry for the same account. Nothing here ever prints a secret, and
``redact`` scrubs any that reaches a message, a log or a state file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

KINDS = ("alpaca_paper", "alpaca_live", "coinbase")
ENV = {
    "alpaca_paper": ("ALPACA_PAPER_KEY", "ALPACA_PAPER_SECRET"),
    "alpaca_live": ("ALPACA_LIVE_KEY", "ALPACA_LIVE_SECRET"),
    "coinbase": ("COINBASE_API_KEY", "COINBASE_API_SECRET"),
}
_REDACTED = "[redacted]"


class CredentialsError(RuntimeError):
    """The credentials could not be loaded safely."""


@dataclass(frozen=True)
class Credentials:
    kind: str
    key: str = field(repr=False)
    secret: str = field(repr=False)

    @property
    def hint(self) -> str:
        return "…" + self.key[-4:]

    def __repr__(self) -> str:
        return f"Credentials({self.kind}, key={self.hint})"

    __str__ = __repr__


def _clean_secret(kind: str, value: str) -> str:
    text = str(value).strip()
    if kind == "coinbase" and "\\n" in text:
        # A PEM pasted onto one TOML line keeps its newlines as "\n" text.
        text = text.replace("\\n", "\n")
    if kind == "coinbase" and text.startswith("-----BEGIN"):
        text = text.strip() + "\n"
    return text


def load_credentials(
    path: Path, *, env: Optional[Mapping[str, str]] = None
) -> dict[str, Credentials]:
    env = os.environ if env is None else env
    found: dict[str, Credentials] = {}
    path = Path(path)
    if path.is_file():
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise CredentialsError(
                f"{path} can be read by other users; run: chmod 600 {path}"
            )
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            raise CredentialsError(f"{path} is not valid TOML; check its quotes") from None
        for kind in KINDS:
            table = raw.get(kind)
            if isinstance(table, dict) and table.get("key") and table.get("secret"):
                found[kind] = Credentials(
                    kind, str(table["key"]).strip(), _clean_secret(kind, table["secret"])
                )
    for kind, (key_name, secret_name) in ENV.items():
        if kind not in found and env.get(key_name) and env.get(secret_name):
            found[kind] = Credentials(
                kind, str(env[key_name]).strip(), _clean_secret(kind, env[secret_name])
            )
    return found


def redact(text: str, credentials: Iterable[Credentials]) -> str:
    """Replace every key, secret and PEM line of ``credentials`` in ``text``."""
    clean = str(text)
    for cred in credentials:
        pieces = [cred.secret, cred.key]
        pieces += [line.strip() for line in cred.secret.splitlines()]
        for piece in sorted(set(pieces), key=len, reverse=True):
            if piece and len(piece) >= 8 and not piece.startswith("-----"):
                clean = clean.replace(piece, _REDACTED)
    return clean
