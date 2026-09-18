"""Check whether a credential set still works, using only the standard library.

Calls ``sts:GetCallerIdentity`` — the one AWS action that every principal is
allowed to perform — and signs it with SigV4 by hand. This deliberately avoids
depending on boto3 or the ``aws`` CLI so the app keeps working when they are
missing, broken, or installed for the wrong architecture.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum

ENDPOINT = "https://sts.amazonaws.com/"
HOST = "sts.amazonaws.com"
REGION = "us-east-1"
SERVICE = "sts"
BODY = "Action=GetCallerIdentity&Version=2011-06-15"
CONTENT_TYPE = "application/x-www-form-urlencoded; charset=utf-8"


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse to replay a signed request at whatever a redirect points to.

    The request carries an Authorization header derived from the user's secret
    key. Following a redirect would resend those headers to another host, so a
    3xx is surfaced as an error instead.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


class Status(str, Enum):
    VALID = "valid"
    EXPIRED = "expired"
    INVALID = "invalid"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass
class Identity:
    status: Status
    account: str | None = None
    arn: str | None = None
    user_id: str | None = None
    error: str | None = None

    @property
    def role_name(self) -> str | None:
        """The friendly tail of the ARN: a role name, session name or user."""
        if not self.arn:
            return None
        tail = self.arn.rsplit("/", 1)[-1] if "/" in self.arn else self.arn.rsplit(":", 1)[-1]
        if "assumed-role/" in self.arn:
            parts = self.arn.split("assumed-role/", 1)[1].split("/")
            return parts[0]
        return tail or None

    @property
    def summary(self) -> str:
        if self.status is Status.VALID:
            bits = [b for b in (self.account, self.role_name) if b]
            return " · ".join(bits) if bits else "valid"
        if self.status is Status.EXPIRED:
            return "expired"
        if self.status is Status.INVALID:
            return "rejected by AWS"
        if self.status is Status.OFFLINE:
            return "offline"
        return "not checked"


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, datestamp: str) -> bytes:
    key = _sign(f"AWS4{secret}".encode("utf-8"), datestamp)
    key = _sign(key, REGION)
    key = _sign(key, SERVICE)
    return _sign(key, "aws4_request")


def _authorization_headers(access_key: str, secret_key: str, token: str | None) -> dict[str, str]:
    now = dt.datetime.now(dt.timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")

    headers = {"content-type": CONTENT_TYPE, "host": HOST, "x-amz-date": amzdate}
    if token:
        headers["x-amz-security-token"] = token

    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    payload_hash = hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    canonical_request = "\n".join(
        ["POST", "/", "", canonical_headers, signed_headers, payload_hash]
    )

    scope = f"{datestamp}/{REGION}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amzdate,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(secret_key, datestamp), string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


def _tag(xml: str, name: str) -> str | None:
    match = re.search(rf"<{name}>([^<]*)</{name}>", xml)
    return match.group(1) if match else None


_EXPIRED_CODES = {"ExpiredToken", "ExpiredTokenException", "TokenRefreshRequired"}
_INVALID_CODES = {
    "InvalidClientTokenId",
    "SignatureDoesNotMatch",
    "UnrecognizedClientException",
    "AuthFailure",
    "IncompleteSignature",
    "MissingAuthenticationToken",
    "InvalidAccessKeyId",
}


def check(access_key: str, secret_key: str, token: str | None = None, timeout: float = 8.0) -> Identity:
    """Ask AWS who these credentials belong to, and whether they still work."""
    if not access_key or not secret_key:
        return Identity(Status.INVALID, error="Missing access key id or secret access key.")
    try:
        headers = _authorization_headers(access_key, secret_key, token)
    except Exception as exc:  # malformed secret, non-ascii, etc.
        return Identity(Status.INVALID, error=str(exc))

    # ENDPOINT is a module constant; no caller-supplied value reaches the URL.
    request = urllib.request.Request(ENDPOINT, data=BODY.encode("utf-8"), headers=headers, method="POST")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            xml = response.read().decode("utf-8", "replace")
        return Identity(
            Status.VALID,
            account=_tag(xml, "Account"),
            arn=_tag(xml, "Arn"),
            user_id=_tag(xml, "UserId"),
        )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        code = _tag(body, "Code") or ""
        message = _tag(body, "Message") or f"HTTP {exc.code}"
        if code in _EXPIRED_CODES:
            return Identity(Status.EXPIRED, error=message)
        if code in _INVALID_CODES:
            return Identity(Status.INVALID, error=message)
        return Identity(Status.UNKNOWN, error=f"{code or 'error'}: {message}")
    except urllib.error.URLError as exc:
        return Identity(Status.OFFLINE, error=str(getattr(exc, "reason", exc)))
    except Exception as exc:
        return Identity(Status.UNKNOWN, error=str(exc))
