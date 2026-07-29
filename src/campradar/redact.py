"""Keep credentials out of anything we write down.

This module exists because of a specific, nearly-shipped accident. The Active
Network API takes its key as a query parameter — there is no header form — so
every request URL contains a secret. Meanwhile httpx logs full URLs at INFO,
`scripts/update.sh` tees the run log to `data/refresh.log`, and that file is
tracked in git. Wiring up the API without this module would have committed a
live API key to a public repository on the first refresh.

The lesson generalises, so the fix is not "special-case Active": anything that
looks like a credential is scrubbed everywhere text leaves the process — logs,
stdout, cache metadata, and exception messages.

Redaction happens at the *boundary*, never at the source. The real URL is what
gets fetched; only its written-down form is scrubbed. Sanitising earlier would
mean sending a request to a literal "REDACTED" key.
"""

from __future__ import annotations

import logging
import re

__all__ = ["PLACEHOLDER", "RedactingFilter", "install_redaction", "redact"]

PLACEHOLDER = "***REDACTED***"

# Query parameters whose values must never be written down. Compared
# case-insensitively. Deliberately broad: a false positive costs a slightly
# less readable log line, a false negative costs a leaked credential.
SENSITIVE_PARAMS = (
    "api_key",
    "apikey",
    "api_secret",
    "access_token",
    "auth",
    "authorization",
    "client_secret",
    "key",
    "passwd",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "session",
    "sig",
    "signature",
    "token",
)

# Matches `param=value` in free text, not just in a parsed query string. That
# matters because most of what we redact is prose we don't control: httpx log
# records, httpx exception messages ("Client error '403' for url '...'"), and
# tracebacks. Parsing those as URLs would be unreliable; a text substitution
# catches the credential wherever it appears.
#
# The value ends at the first character that cannot appear in a query value, so
# a trailing quote, angle bracket or space terminates the match rather than
# being swallowed into it.
# The leading group is a *captured* prefix rather than a lookbehind because the
# separator may be one character ("?", "&") or three ("%3F", "%26"), and Python
# requires lookbehinds to be fixed width. It also can't be `\b`: in
# "...%3Fapi_key%3D..." the character before "api_key" is "F", a word character,
# so no boundary exists there at all. That gap was found by a test, not by
# reading the pattern.
_PARAM_RE = re.compile(
    r"(?i)(^|[?&;,\s\"'<>\[{(]|%3F|%26)"
    r"(" + "|".join(SENSITIVE_PARAMS) + r")"
    r"(=|%3D)"
    r"([^&\s\"'<>\]}),]+)",
)

# Bare `Authorization: Bearer xyz` style headers, which have no `=`.
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._~+/=-]{8,})")


def redact(value: object) -> str:
    """Return `value` as text with any credential-looking substring removed.

    Accepts any object so that callers can pass exceptions directly without
    remembering to stringify first — the easy path has to be the safe one.

        >>> redact("https://api.example.com/v2/search?near=Decatur&api_key=abc123")
        'https://api.example.com/v2/search?near=Decatur&api_key=***REDACTED***'
        >>> redact("no secrets here")
        'no secrets here'

    The parameter name is kept so logs stay debuggable — knowing a request
    carried an `api_key` is useful; knowing its value is not.

        >>> redact("GET /v2/search?api_key=k1&token=k2 failed")
        'GET /v2/search?api_key=***REDACTED***&token=***REDACTED*** failed'

    Percent-encoded separators are caught too, since URLs get encoded before
    they reach a log line:

        >>> redact("...%3Fapi_key%3Dsupersecret&x=1")
        '...%3Fapi_key%3D***REDACTED***&x=1'
    """
    text = str(value)
    text = _PARAM_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{PLACEHOLDER}", text)
    return _BEARER_RE.sub(lambda m: f"{m.group(1)} {PLACEHOLDER}", text)


class RedactingFilter(logging.Filter):
    """Scrubs credentials from every log record passing through a handler.

    Attached to the root handler rather than to our own loggers, because the
    record that leaks the key is emitted by httpx, not by us. Filtering only
    `campradar.*` would miss the actual problem.

    The formatted message replaces `msg` and `args` is cleared: composing early
    is what makes a single substitution sufficient regardless of how the caller
    split the message across format arguments.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            composed = record.getMessage()
        except Exception:  # pragma: no cover - malformed record, keep it moving
            return True
        redacted = redact(composed)
        if redacted != composed:
            record.msg = redacted
            record.args = ()
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install_redaction() -> None:
    """Attach the filter to every root handler. Safe to call more than once."""
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
