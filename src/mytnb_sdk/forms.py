"""Minimal, strict HTML form parsing for the login SSO hand-off.

Standard library only; no JavaScript is executed and no HTML engine is used.
The parser is intentionally narrow: it understands ``<form>``/``<input>``
enough to reject malformed or ambiguous SSO hand-off pages rather than
guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .errors import UnexpectedResponseError

__all__ = [
    "FormField",
    "HtmlForm",
    "parse_forms",
    "parse_sso_form",
]

# Input types that are controls rather than forwarded form data.
_NON_DATA_INPUT_TYPES = frozenset({"submit", "button", "reset", "image"})


@dataclass(frozen=True, slots=True)
class FormField:
    """A single ``<input>`` with a ``name`` attribute."""

    name: str
    value: str
    input_type: str | None = None


@dataclass(frozen=True, slots=True)
class HtmlForm:
    """A parsed ``<form>`` element and its named inputs, in document order."""

    action: str | None
    method: str | None
    fields: tuple[FormField, ...]


class _FormCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[HtmlForm] = []
        self._stack: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): (value if value is not None else "") for name, value in attrs}
        if tag == "form":
            self._stack.append(
                {
                    "action": attributes.get("action"),
                    "method": attributes.get("method"),
                    "fields": [],
                }
            )
        elif tag == "input" and self._stack:
            name = attributes.get("name")
            if not name:
                return
            fields = self._stack[-1]["fields"]
            assert isinstance(fields, list)
            fields.append(
                FormField(
                    name=name,
                    value=attributes.get("value", ""),
                    input_type=attributes.get("type"),
                )
            )

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Self-closing tags such as ``<input ... />``.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._stack:
            form = self._stack.pop()
            fields = form["fields"]
            assert isinstance(fields, list)
            self.forms.append(
                HtmlForm(
                    action=form["action"] if isinstance(form["action"], str) else None,
                    method=form["method"] if isinstance(form["method"], str) else None,
                    fields=tuple(fields),
                )
            )


def parse_forms(html: str) -> tuple[HtmlForm, ...]:
    """Parse every ``<form>`` and its named ``<input>`` children."""
    if not isinstance(html, str):
        raise UnexpectedResponseError("expected an HTML document")
    collector = _FormCollector()
    collector.feed(html)
    collector.close()
    return tuple(collector.forms)


def _effective_port(parts) -> int | None:
    if parts.port is not None:
        return parts.port
    if parts.scheme == "https":
        return 443
    if parts.scheme == "http":
        return 80
    return None


def _require_exact_url(actual: str, expected: str) -> None:
    actual_parts = urlsplit(actual)
    expected_parts = urlsplit(expected)
    if actual_parts.query or actual_parts.fragment:
        raise UnexpectedResponseError("SSO form action had an unexpected query or fragment")
    if (
        actual_parts.scheme.lower() != expected_parts.scheme.lower()
        or (actual_parts.hostname or "").lower() != (expected_parts.hostname or "").lower()
        or _effective_port(actual_parts) != _effective_port(expected_parts)
        or actual_parts.path != expected_parts.path
    ):
        raise UnexpectedResponseError("SSO form action did not match the fixed endpoint")


def parse_sso_form(
    form: HtmlForm,
    *,
    expected_url: str,
    required_fields: tuple[str, ...],
    nonempty_fields: tuple[str, ...] = (),
) -> dict[str, str]:
    """Validate a single SSO hand-off form and return its fields verbatim.

    Raises :class:`UnexpectedResponseError` (with a static, non-leaking
    message) for a wrong method, wrong action origin/path, missing required
    fields, duplicate field names, or empty token/signature values. The
    returned mapping preserves document order and original values.
    """
    if (form.method or "").strip().lower() != "post":
        raise UnexpectedResponseError("SSO form method was not POST")
    if not form.action:
        raise UnexpectedResponseError("SSO form was missing an action")

    _require_exact_url(form.action, expected_url)

    forwarded: dict[str, str] = {}
    for field in form.fields:
        if (field.input_type or "").lower() in _NON_DATA_INPUT_TYPES:
            continue
        if field.name in forwarded:
            raise UnexpectedResponseError("SSO form contained a duplicate field name")
        forwarded[field.name] = field.value

    for name in required_fields:
        if name not in forwarded:
            raise UnexpectedResponseError("SSO form was missing a required field")

    for name in nonempty_fields:
        if name in forwarded and not forwarded[name].strip():
            raise UnexpectedResponseError("SSO form token or signature was empty")

    return forwarded
