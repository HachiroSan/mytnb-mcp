"""Domain models returned by the :mod:`mytnb_sdk` client.

Only the minimal surface that the client needs is modelled. Raw API payloads
are mapped into these dataclasses so callers never handle loosely typed
dictionaries.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import UnexpectedResponseError

__all__ = ["Account", "BillRecord", "bills_from_history"]


def _coerce_optional_text(value: Any) -> str | None:
    """Return a trimmed string, or ``None`` for missing/blank input."""
    if value is None:
        return None
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed or None
    return str(value)


def _coerce_decimal(value: Any) -> Decimal | None:
    """Validate and convert a monetary value to :class:`~decimal.Decimal`.

    Missing or blank values are tolerated and become ``None``. Values that are
    present but not representable as a finite decimal raise a typed error.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise UnexpectedResponseError("monetary value had an invalid type")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        # Route through str to avoid binary floating point noise.
        result = Decimal(str(value))
    elif isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return None
        try:
            result = Decimal(trimmed)
        except InvalidOperation as exc:
            raise UnexpectedResponseError("monetary value was not a valid decimal") from exc
    else:
        raise UnexpectedResponseError("monetary value had an unsupported type")

    if not result.is_finite():
        raise UnexpectedResponseError("monetary value must be finite")
    return result


def _coerce_optional_bool(value: Any) -> bool | None:
    """Tolerantly interpret an optional boolean flag from an API record."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        raise UnexpectedResponseError("bill flag had an invalid numeric value")
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "":
            return None
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
    raise UnexpectedResponseError("bill flag had an invalid value")


def _parse_date_field(value: Any) -> date:
    """Parse the 8-digit ``YYYYMMDD`` ``DateField`` value.

    The placeholder-looking ``Date`` string is intentionally never used. An
    invalid or ambiguous ``DateField`` fails explicitly rather than guessing.
    """
    if not isinstance(value, str):
        raise UnexpectedResponseError("bill DateField was not a string")
    text = value.strip()
    if len(text) != 8 or not text.isascii() or not text.isdigit():
        raise UnexpectedResponseError("bill DateField was not an 8-digit YYYYMMDD value")
    year = int(text[0:4])
    month = int(text[4:6])
    day = int(text[6:8])
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise UnexpectedResponseError("bill DateField was not a valid calendar date") from exc


@dataclass(frozen=True, slots=True)
class Account:
    """A single owned account.

    ``account_id`` is treated as an opaque identifier: it is stored verbatim
    and never parsed. All commercial and personal fields (``account_id``,
    ``account_no``, ``description`` and ``total_payable``) are excluded from
    ``repr`` so they cannot leak through logs or debug output.
    """

    account_id: str = field(repr=False)
    account_no: str | None = field(default=None, repr=False)
    description: str | None = field(default=None, repr=False)
    total_payable: Decimal | None = field(default=None, repr=False)

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> Account:
        """Build an :class:`Account` from one raw API record.

        Unknown fields are ignored. A missing or blank ``AccountID`` is a
        contract violation and raises :class:`UnexpectedResponseError`.
        """
        if not isinstance(raw, Mapping):
            raise UnexpectedResponseError("account record was not a mapping")

        raw_id = raw.get("AccountID")
        if raw_id is None:
            raise UnexpectedResponseError("account record was missing AccountID")
        account_id = str(raw_id).strip()
        if not account_id:
            raise UnexpectedResponseError("account record had a blank AccountID")

        return cls(
            account_id=account_id,
            account_no=_coerce_optional_text(raw.get("AccountNo")),
            description=_coerce_optional_text(raw.get("AccountDescription")),
            total_payable=_coerce_decimal(raw.get("TotalPayable")),
        )


@dataclass(frozen=True, slots=True)
class BillRecord:
    """A single historical **bill** (payments are excluded downstream).

    Only fields verified in the embedded payment-history model are modelled.
    ``billing_id`` (the opaque ``BillingNoEnc`` token) is excluded from
    ``repr``. Billing period, usage and due date are deliberately **not**
    modelled because they are not verified from the observed payload.
    """

    date: date
    amount: Decimal
    billing_id: str = field(repr=False)
    is_cancellation: bool | None = None
    is_reversal: bool | None = None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> BillRecord:
        """Build a :class:`BillRecord` from one ``Type == "Bill"`` entry."""
        if not isinstance(raw, Mapping):
            raise UnexpectedResponseError("bill entry was not a mapping")

        bill_date = _parse_date_field(raw.get("DateField"))
        amount = _coerce_decimal(raw.get("Amount"))
        if amount is None:
            raise UnexpectedResponseError("bill entry was missing Amount")
        billing_id = _coerce_optional_text(raw.get("BillingNoEnc"))
        if billing_id is None:
            raise UnexpectedResponseError("bill entry was missing BillingNoEnc")

        return cls(
            date=bill_date,
            amount=amount,
            billing_id=billing_id,
            is_cancellation=_coerce_optional_bool(raw.get("IsBillCancelation")),
            is_reversal=_coerce_optional_bool(raw.get("IsBillReversal")),
        )


def bills_from_history(entries: Any) -> list[BillRecord]:
    """Map ``BillingPaymentHistory`` entries to bills, excluding payments.

    ``Type == "Bill"`` becomes a :class:`BillRecord`; ``Type == "Payment"`` is
    skipped. A missing or unrecognised ``Type`` is rejected rather than
    silently dropped, so an unexpected shape never looks like empty success.
    """
    if not isinstance(entries, list):
        raise UnexpectedResponseError("billing history was not a list")

    bills: list[BillRecord] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise UnexpectedResponseError("billing history contained a non-object entry")
        raw_type = entry.get("Type")
        if not isinstance(raw_type, str) or not raw_type.strip():
            raise UnexpectedResponseError("billing history entry was missing Type")
        entry_type = raw_type.strip()
        if entry_type == "Payment":
            continue
        if entry_type != "Bill":
            raise UnexpectedResponseError("billing history contained an unsupported entry type")
        bills.append(BillRecord.from_api(entry))
    return bills
