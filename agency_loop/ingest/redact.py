"""Masking of secrets and personal data (docs/ARCHITECTURE.md §3.3). One Redactor per session.

Rules run in a fixed order and never match their own placeholders, so text that went through
`Redactor.redact` gives `find_secrets(text) == []`.
Every pattern is bounded or anchored at a run start: uploads go up to 32 MiB, and an unbounded
`[...]+` tried at every position of a long base64 run would be quadratic.
"""

from __future__ import annotations

import re
from itertools import pairwise

from agency_loop.model import RedactionStats

_PRIVATE_KEY = re.compile(
    # A block cut before its END line is masked to the end of the text.
    r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END[A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_TOKEN = re.compile(
    r"\b(?:sk-ant-[\w-]{8,}|sk-[\w-]{20,}|AKIA[0-9A-Z]{16}\b|gh[pousr]_[A-Za-z0-9]{10,}|github_pat_\w{10,}"
    r"|xox[abprs]-[\w-]{8,}|AIza[\w-]{35}|eyJ[\w-]+\.eyJ[\w-]+\.[\w-]*|SG\.[\w-]{16,}\.[\w-]{16,})",
    re.ASCII,
)
# "Basic" only after an Authorization header name: as a plain word it is common English.
_AUTH = re.compile(
    r"\b(?i:bearer|authorization[\"']?[ \t]*[:=][ \t]*[\"']?basic)[ \t]+(?P<v>[\w.~+/=-]{8,})", re.ASCII
)
_WORDS = r"(?:api|access|private)[_.-]?key|secret|token|passw(?:or)?d|pwd|credential"
# key = v, key: type = v, "key": v, cfg["key"] = v, key == v (and !=, ===, :=); _secret_key then checks the name.
_KEY_VALUE = re.compile(
    rf"(?<![\w.-])(?P<k>[\w.-]{{0,40}}?(?:{_WORDS})[\w.-]{{0,40}})[\"']?\]?[ \t]*"
    r"(?::[ \t]*[A-Za-z_][\w.\[\]|, ]{0,40}?[ \t]=|[=!:]=?=|[:=])[ \t]*"
    r"(?P<v>\"[^\"\n]*\"|'[^'\n]*'|[^\s\"'`,;&()\[\]{}]+)",
    re.ASCII | re.IGNORECASE,
)
# Key name parts: split on _ - . and camelCase steps (apiKey, APIKey).
_KEY_PARTS = re.compile(r"[_.-]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SECRET_PARTS = {"secret", "token", "password", "passwd", "pwd", "credential", "credentials", "apikey"}
_SECRET_PAIRS = {("api", "key"), ("access", "key"), ("private", "key"), ("secret", "key")}
_NOT_SECRET_LAST = {
    "count", "len", "length", "size", "limit", "max", "min", "type", "name", "url", "uri", "path", "file", "dir",
    "prefix", "ttl", "expiry", "expires", "timeout", "header", "field", "param", "env", "enabled", "format", "mode",
    "version",
}
_URL_CREDENTIAL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]{0,30}://(?P<v>[^\s/@:]+:[^\s/]*)@", re.ASCII)
_EMAIL = re.compile(
    r"(?<![\w.%+-])[\w.%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24}(?![\w-])", re.ASCII
)
_IP = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9]|\.[0-9])", re.ASCII)
_PHONE = re.compile(
    r"(?<![0-9A-Za-z])(?:\+82[-. ]?0?(?:1[016789]|2|[3-6][1-5]|70)[-. ]?[0-9]{3,4}[-. ]?[0-9]{4}"
    r"|01[016789][-. ]?[0-9]{3,4}[-. ]?[0-9]{4}"
    r"|0(?:2|[3-6][1-5]|70)[-. ][0-9]{3,4}[-. ][0-9]{4})(?![0-9])",
    re.ASCII,
)
_HOME = re.compile(r"(?<![\w.-])(?:/home/|/Users/|[A-Za-z]:\\Users\\)[^\s/\\\"'`<>:;,()\[\]{}|]+", re.ASCII)

# Values that are not secrets: placeholders, references to the environment, and our own masks.
_PLACEHOLDER = re.compile(
    r"x{3,}|\*{3,}|<.*|your[_-].*|\$\{.*|(?:os\.environ|os\.getenv|process\.env).*"
    r"|\[(?:SECRET|EMAIL|IP|PHONE)_\d+\]|\[(?:PRIVATE_KEY|CREDENTIAL)\]",
    re.IGNORECASE | re.DOTALL,
)

# (category, pattern) in masking order. A pattern with group "v" masks only that group.
_RULES = (
    ("private_key", _PRIVATE_KEY),
    ("secret", _TOKEN),
    ("secret", _AUTH),
    ("secret", _KEY_VALUE),
    ("url_credential", _URL_CREDENTIAL),
    ("email", _EMAIL),
    ("ip", _IP),
    ("phone", _PHONE),
    ("home_path", _HOME),
)
_SECRET_CATEGORIES = ("private_key", "secret", "url_credential")
_FIXED = {"private_key": "[PRIVATE_KEY]", "url_credential": "[CREDENTIAL]", "home_path": "<HOME>"}


def _secret_key(name: str) -> bool:
    """§3.3: access_token, API_KEY, apiKey, SECRET_KEY_BASE, AWS_SECRET_ACCESS_KEY, PGPASSWORD, GITHUBTOKEN are secret
    names; max_tokens, token_count, tokenizer, token_url are not."""
    if name.lower().endswith(("password", "passwd", "secret", "token")):
        return True
    parts = [part.lower() for part in _KEY_PARTS.split(name) if part]
    named = any(part in _SECRET_PARTS for part in parts) or any(pair in _SECRET_PAIRS for pair in pairwise(parts))
    return named and parts[-1] not in _NOT_SECRET_LAST


def _span(category: str, match: re.Match[str]) -> tuple[int, int] | None:
    """The part of a match to mask, or None when the match is not sensitive."""
    if category == "ip":
        value = match.group()
        if value.startswith("127.") or value == "0.0.0.0" or any(int(part) > 255 for part in value.split(".")):  # noqa: S104
            return None
        return match.span()
    if "v" not in match.re.groupindex:
        return match.span()
    start, end = match.span("v")
    value = match.group("v")
    if category == "secret":  # Bearer/Basic token or key=value: keep quotes, skip short values and placeholders
        quoted = value[:1] in ('"', "'")
        if quoted:
            start, end, value = start + 1, end - 1, value[1:-1]
        if len(value) < 6 or _PLACEHOLDER.fullmatch(value):  # None/True/False/null are all shorter than 6
            return None
        if "k" in match.re.groupindex and (
            not _secret_key(match.group("k"))
            # Unquoted code (settings.x, getenv("X"), cfg["k"]) refers to a secret; it is not one.
            or not quoted and ("." in value or match.string[end : end + 1] in ("(", "["))
        ):
            return None
    return start, end


class Redactor:
    """Masks text of one session. Same value -> same placeholder; counts split by origin."""

    def __init__(self) -> None:
        self.stats = RedactionStats()
        self._names: dict[str, dict[str, str]] = {}

    def redact(self, text: str, *, origin: str) -> str:
        counts = {"text": self.stats.in_text, "output": self.stats.in_outputs}[origin]
        for category, pattern in _RULES:
            text = pattern.sub(lambda match, category=category: self._replace(match, category, counts), text)
        return text

    def _replace(self, match: re.Match[str], category: str, counts: dict[str, int]) -> str:
        span = _span(category, match)
        if span is None:
            return match.group()
        counts[category] = counts.get(category, 0) + 1
        offset, whole = match.start(), match.group()
        masked = self._placeholder(category, match.string[span[0] : span[1]])
        return whole[: span[0] - offset] + masked + whole[span[1] - offset :]

    def _placeholder(self, category: str, value: str) -> str:
        if category in _FIXED:
            return _FIXED[category]
        names = self._names.setdefault(category, {})
        key = value.lower() if category == "email" else value
        if key not in names:
            names[key] = f"[{category.upper()}_{len(names) + 1}]"
        return names[key]


def find_secrets(text: str) -> list[str]:
    """Secret categories still present in text (private_key, secret, url_credential); [] means safe to send."""
    found: list[str] = []
    for category, pattern in _RULES:
        if category in _SECRET_CATEGORIES and category not in found:
            if any(_span(category, match) for match in pattern.finditer(text)):
                found.append(category)
    return found
