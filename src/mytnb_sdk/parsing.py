"""Safe extraction of the embedded payment-history model from profile HTML.

No JavaScript is executed and ``eval`` is never used. The inline
``refreshGrid`` script is located with :class:`html.parser.HTMLParser`, then a
small, string/comment-aware scanner locates the function *declaration* and its
body. Line and block comments are ignored, and the contents of string literals
are masked before the active ``var model = {...};`` statement is found, so
commented-out assignments or ``var model`` text inside JSON strings are never
mistaken for the real one. The JSON value itself is decoded with
:meth:`json.JSONDecoder.raw_decode`, which stops at the end of the JSON value
even when it contains braces or semicolons inside strings.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any

from .errors import UnexpectedResponseError

__all__ = [
    "extract_payment_history_model",
    "is_login_path",
    "looks_like_challenge_html",
    "looks_like_login_html",
]

# Matches the actual function declaration, not an earlier ``refreshGrid()``
# call or event reference that may appear before it.
_FUNCTION_DECL_RE = re.compile(r"\bfunction\s+refreshGrid\s*\(")
_MODEL_ASSIGN_RE = re.compile(r"\bvar\s+model\s*=\s*")

_LOGIN_PATH_MARKERS = ("login", "signin", "sign-in", "sso", "authorize")
_LOGIN_HTML_MARKERS = (
    'type="password"',
    "type='password'",
    'name="password"',
    "name='password'",
    'id="password"',
    "session expired",
    "session has expired",
)
# Recognisable interactive-challenge markers. Used only to classify a page the
# flow did not expect; the library never attempts to solve or bypass them.
_CHALLENGE_MARKERS = (
    "captcha",
    "g-recaptcha",
    "recaptcha",
    "one-time password",
    "one time password",
    "one-time pin",
    "verification code",
    "two-factor",
    "two factor authentication",
    "authentication code",
)


class _ScriptCollector(HTMLParser):
    """Collect the text content of every ``<script>`` element."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_script = False
        self._current: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._in_script = True
            self._current = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_script:
            self._in_script = False
            self.scripts.append("".join(self._current))

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._current.append(data)


def _scripts_from_html(html: str) -> list[str]:
    collector = _ScriptCollector()
    collector.feed(html)
    collector.close()
    return collector.scripts


def _mask_non_code(text: str) -> str:
    """Blank out JS string literals and comments, preserving positions.

    Every character inside a single/double/backtick string literal or a
    ``//``/``/* */`` comment is replaced with a space (newlines are kept), so
    the result has the same length and indices as ``text``. Structural
    searches are run against the masked copy and the JSON value is then read
    from the original text at the same offsets.
    """
    out = list(text)
    length = len(text)
    i = 0
    while i < length:
        char = text[i]
        nxt = text[i + 1] if i + 1 < length else ""

        if char == "/" and nxt == "/":
            out[i] = " "
            out[i + 1] = " "
            i += 2
            while i < length and text[i] not in "\r\n":
                out[i] = " "
                i += 1
            continue

        if char == "/" and nxt == "*":
            out[i] = " "
            out[i + 1] = " "
            i += 2
            while i < length and not (text[i] == "*" and i + 1 < length and text[i + 1] == "/"):
                out[i] = text[i] if text[i] in "\r\n" else " "
                i += 1
            if i < length:
                out[i] = " "
                if i + 1 < length:
                    out[i + 1] = " "
                i += 2
            continue

        if char in ("'", '"', "`"):
            quote = char
            out[i] = " "
            i += 1
            while i < length:
                current = text[i]
                if current == "\\" and i + 1 < length:
                    out[i] = " "
                    out[i + 1] = text[i + 1] if text[i + 1] in "\r\n" else " "
                    i += 2
                    continue
                if current == quote:
                    out[i] = " "
                    i += 1
                    break
                out[i] = current if current in "\r\n" else " "
                i += 1
            continue

        i += 1

    return "".join(out)


def _function_body_bounds(masked: str, open_paren: int) -> tuple[int, int] | None:
    """Return the ``(start, end)`` offsets of the function body content.

    ``start`` is just after the opening brace and ``end`` is the position of
    the matching closing brace, so ``masked[start:end]`` is the body text.
    Braces inside strings/comments are already masked, so plain depth counting
    is safe. Returns ``None`` if the braces cannot be balanced.
    """
    length = len(masked)
    depth = 0
    closed = -1
    i = open_paren
    while i < length:
        char = masked[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                closed = i
                break
        i += 1
    if closed == -1:
        return None

    j = closed + 1
    while j < length and masked[j] != "{":
        j += 1
    if j >= length:
        return None

    body_open = j
    depth = 0
    k = body_open
    while k < length:
        char = masked[k]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return (body_open + 1, k)
        k += 1
    return None


def _model_from_script(script: str) -> Any | None:
    """Return the ``model`` object assigned inside ``function refreshGrid``.

    Returns ``None`` when this script is unrelated (no function declaration).
    Raises :class:`UnexpectedResponseError` when the active assignment is
    missing, ambiguous, unterminated, or not valid JSON.
    """
    masked = _mask_non_code(script)
    declaration = _FUNCTION_DECL_RE.search(masked)
    if declaration is None:
        return None

    bounds = _function_body_bounds(masked, declaration.end() - 1)
    if bounds is None:
        raise UnexpectedResponseError("refreshGrid function body could not be located")

    body_start, body_end = bounds
    masked_body = masked[body_start:body_end]
    original_body = script[body_start:body_end]

    matches = list(_MODEL_ASSIGN_RE.finditer(masked_body))
    if not matches:
        raise UnexpectedResponseError("refreshGrid did not contain a model assignment")
    if len(matches) > 1:
        raise UnexpectedResponseError("refreshGrid contained multiple model assignments")

    value_start = matches[0].end()
    try:
        model, value_end = json.JSONDecoder().raw_decode(original_body[value_start:])
    except json.JSONDecodeError:
        # Raised from None so the traceback cannot echo the raw (possibly
        # personal) HTML/JSON document contained in the JSONDecodeError.
        raise UnexpectedResponseError("payment history model was not valid JSON") from None

    if not original_body[value_start + value_end :].lstrip().startswith(";"):
        raise UnexpectedResponseError("refreshGrid model assignment was not terminated with ';'")
    return model


def extract_payment_history_model(html: str) -> Mapping[str, Any]:
    """Return the embedded payment-history model object from a profile page.

    Raises :class:`UnexpectedResponseError` for missing, ambiguous or malformed
    models; it never returns an empty placeholder.
    """
    if not isinstance(html, str):
        raise UnexpectedResponseError("expected an HTML document")

    found: list[Mapping[str, Any]] = []
    for script in _scripts_from_html(html):
        model = _model_from_script(script)
        if model is None:
            continue
        if not isinstance(model, Mapping):
            raise UnexpectedResponseError("payment history model was not an object")
        found.append(model)

    if not found:
        raise UnexpectedResponseError("payment history model was not found in the page")
    if len(found) > 1:
        raise UnexpectedResponseError("multiple payment history models were found in the page")
    return found[0]


def is_login_path(path: str | None) -> bool:
    """Return ``True`` only for URLs that look like a login/SSO endpoint."""
    if not path:
        return False
    lowered = path.lower()
    return any(marker in lowered for marker in _LOGIN_PATH_MARKERS)


def looks_like_login_html(html: str) -> bool:
    """Conservative check for an actual login/session-expired page.

    This is only consulted after the payment-history model fails to parse, so
    ordinary HTML (which is expected on the bill route) is not misclassified.
    """
    if not isinstance(html, str):
        return False
    lowered = html.lower()
    return any(marker in lowered for marker in _LOGIN_HTML_MARKERS)


def looks_like_challenge_html(html: str) -> bool:
    """Return ``True`` for a recognisable CAPTCHA/OTP/2FA challenge page.

    Deliberately conservative: it is consulted only where the expected
    login/SSO payload was missing, and the library never attempts to solve,
    bypass or retry the challenge.
    """
    if not isinstance(html, str):
        return False
    lowered = html.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)
