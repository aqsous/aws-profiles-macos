"""Recognise AWS credentials in whatever shape they were copied from.

The AWS access portal, ``aws sts assume-role``, ``credential_process`` helpers
and assorted internal tools all emit the same three secrets in four different
layouts. Users should not have to care which one is on the clipboard.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

# Access key ids are uppercase alphanumerics with a type prefix. ASIA marks a
# temporary key (which must be accompanied by a session token); AKIA a
# long-lived IAM user key.
ACCESS_KEY_RE = re.compile(r"\b((?:AKIA|ASIA|AIDA|AROA|AGPA|ANPA|ANVA|ABIA|ACCA)[A-Z0-9]{12,})\b")
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9._@:+=,-]{1,128}$")

_KEY_ALIASES = {
    "aws_access_key_id": "access_key_id",
    "access_key_id": "access_key_id",
    "accesskeyid": "access_key_id",
    "aws_secret_access_key": "secret_access_key",
    "secret_access_key": "secret_access_key",
    "secretaccesskey": "secret_access_key",
    "aws_session_token": "session_token",
    "session_token": "session_token",
    "sessiontoken": "session_token",
    "aws_security_token": "session_token",
    "aws_region": "region",
    "region": "region",
    "expiration": "expiration",
    "aws_credential_expiration": "expiration",
    "x_security_token_expires": "expiration",
}


class ParseError(ValueError):
    """The clipboard did not contain a usable, self-consistent credential set."""


@dataclass
class ParsedCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str | None = None
    region: str | None = None
    expiration: datetime | None = None
    profile_name: str | None = None
    source_format: str = "unknown"

    @property
    def is_temporary(self) -> bool:
        return bool(self.session_token)

    @property
    def masked_key(self) -> str:
        return mask(self.access_key_id)

    def to_ini_values(self) -> dict[str, str]:
        values = {
            "aws_access_key_id": self.access_key_id,
            "aws_secret_access_key": self.secret_access_key,
        }
        if self.session_token:
            values["aws_session_token"] = self.session_token
        return values


def mask(secret: str | None, keep: int = 4) -> str:
    """Render a secret as a recognisable stub: ``ASIA••••••••4XYZ``."""
    if not secret:
        return "—"
    if len(secret) <= keep * 2:
        return "•" * len(secret)
    return f"{secret[:keep]}{'•' * 8}{secret[-keep:]}"


def _parse_expiration(raw: str) -> datetime | None:
    raw = raw.strip().strip("\"'")
    if not raw:
        return None
    try:
        # Accept both "2026-09-17T18:04:11Z" and "+00:00" offsets.
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _clean(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip()


def _build(fields: dict[str, str], fmt: str, profile_name: str | None = None) -> ParsedCredentials:
    access = fields.get("access_key_id", "")
    secret = fields.get("secret_access_key", "")
    if not access and not secret:
        raise ParseError("No AWS credentials found in the clipboard.")
    if not access:
        raise ParseError("Found a secret access key but no access key id — the paste looks incomplete.")
    if not secret:
        raise ParseError("Found an access key id but no secret access key — the paste looks incomplete.")
    if not ACCESS_KEY_RE.fullmatch(access):
        raise ParseError(f"'{mask(access)}' is not a valid AWS access key id.")

    token = fields.get("session_token") or None
    # A truncated copy of a portal block is the classic failure: the token is
    # the longest line and the easiest to lose. Catch it before it is written.
    if access.startswith("ASIA") and not token:
        raise ParseError(
            "This is a temporary key (ASIA…) but no session token came with it.\n"
            "Copy the whole block again — the session token was left behind."
        )
    if token and len(token) < 100:
        raise ParseError("The session token looks truncated — copy the whole block again.")

    return ParsedCredentials(
        access_key_id=access,
        secret_access_key=secret,
        session_token=token,
        region=fields.get("region") or None,
        expiration=_parse_expiration(fields.get("expiration", "")),
        profile_name=profile_name,
        source_format=fmt,
    )


def _parse_json(text: str) -> ParsedCredentials | None:
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    # `aws sts assume-role` nests under "Credentials"; credential_process and
    # `aws configure export-credentials` return the fields at the top level.
    body = data.get("Credentials") if isinstance(data.get("Credentials"), dict) else data
    fields: dict[str, str] = {}
    for key, value in body.items():
        canonical = _KEY_ALIASES.get(str(key).lower().replace(" ", ""))
        if canonical and isinstance(value, (str, int)):
            fields[canonical] = str(value)
    if not fields:
        return None
    name = data.get("ProfileName") or data.get("profile")
    return _build(fields, "json", name if isinstance(name, str) else None)


def _parse_key_values(text: str) -> tuple[dict[str, str], str | None, str]:
    """Pull key/value pairs out of ini, shell-export or PowerShell text."""
    fields: dict[str, str] = {}
    profile_name: str | None = None
    fmt = "ini"
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        section = SECTION_LINE.match(line)
        if section:
            candidate = section.group(1).strip()
            # Portal blocks say "[profile foo]" in config, "[foo]" in credentials.
            if candidate.startswith("profile "):
                candidate = candidate[len("profile ") :].strip()
            if PROFILE_NAME_RE.match(candidate):
                profile_name = candidate
            continue
        stripped = line
        for prefix in ("export ", "set ", "SET ", "$Env:", "$env:"):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix) :].strip()
                fmt = "env"
                break
        if "=" not in stripped:
            continue
        raw_key, _, raw_value = stripped.partition("=")
        canonical = _KEY_ALIASES.get(raw_key.strip().lower())
        if canonical:
            fields[canonical] = _clean(raw_value)
    return fields, profile_name, fmt


SECTION_LINE = re.compile(r"^\[([^\]]+)\]$")


def parse(text: str) -> ParsedCredentials:
    """Parse ``text`` into one credential set, or raise :class:`ParseError`."""
    if not text or not text.strip():
        raise ParseError("The clipboard is empty.")
    if len(text) > 100_000:
        raise ParseError("That is far too much text to be a credential block.")

    parsed = _parse_json(text)
    if parsed is not None:
        return parsed

    fields, profile_name, fmt = _parse_key_values(text)
    if fields:
        return _build(fields, fmt, profile_name)

    # Last resort: someone pasted loose text that still contains the secrets.
    match = ACCESS_KEY_RE.search(text)
    if match:
        raise ParseError(
            "Found an access key id but could not tell which value is the secret key.\n"
            "Paste the whole block from the AWS access portal instead."
        )
    raise ParseError("No AWS credentials found in the clipboard.")


def parse_all(text: str) -> list[ParsedCredentials]:
    """Parse a paste that may hold several ``[profile]`` blocks at once."""
    blocks: list[tuple[str | None, list[str]]] = []
    current: tuple[str | None, list[str]] | None = None
    for line in text.splitlines():
        section = SECTION_LINE.match(line.strip())
        if section:
            current = (line, [line])
            blocks.append(current)
        elif current is not None:
            current[1].append(line)
    if len(blocks) <= 1:
        return [parse(text)]

    results: list[ParsedCredentials] = []
    errors: list[str] = []
    for _, lines in blocks:
        try:
            results.append(parse("\n".join(lines)))
        except ParseError as exc:
            errors.append(str(exc))
    if not results:
        raise ParseError(errors[0] if errors else "No AWS credentials found in the clipboard.")
    return results


def plan_import(found: list[ParsedCredentials], target: str | None) -> tuple[str, object]:
    """Decide what an import of ``found`` should do, without any UI.

    Returns one of:
      ``("write", (name, creds))``  one block whose destination is known
      ``("many", found)``           several named blocks, imported together
      ``("ask", found)``            the user has to supply or confirm a name
    """
    if len(found) == 1:
        name = target or found[0].profile_name
        if name:
            return "write", (name, found[0])
        return "ask", found
    if len(found) > 1 and target is None:
        return "many", found
    return "ask", found
