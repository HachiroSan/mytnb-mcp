"""Shared, sanitized fixtures for the test suite.

All data here is invented; it contains no real account identifiers, tokens or
personal information. It only mirrors the *shape* of the observed payloads.
"""

from __future__ import annotations

import json
from typing import Any


def bill_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "DateField": "20240115",
        "Type": "Bill",
        "Details": "Electricity usage",
        "Amount": "88.10",
        "View": "view",
        "Date_Year": 2024,
        "Date_Month": "Jan",
        "Date_Day": "15",
        "Date": "00/00/0000 00:00:00",
        "BillingNoEnc": "ENC-BILL-1",
        "ResponseCode": "00",
        "IsBillCancelation": False,
        "IsBillReversal": False,
        "IsNegativeVal": False,
    }
    entry.update(overrides)
    return entry


def payment_entry(**overrides: Any) -> dict[str, Any]:
    entry = bill_entry(Type="Payment", BillingNoEnc="ENC-PAY-1")
    entry.update(overrides)
    return entry


def model_payload(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "CaNo": "0000000",
        "IsOwnedAccount": True,
        "BillingPaymentHistory": entries,
        "TotalCount": len(entries),
        "LearnMoreUrl": "/learn-more",
        "IsFiah": False,
    }


def profile_html(model: Any, *, prefix: str = "", suffix: str = "") -> str:
    return (
        "<html><head><title>Account</title></head><body>"
        f"{prefix}"
        "<script type='text/javascript'>\n"
        "function refreshGrid() {\n"
        f"var model = {json.dumps(model)};\n"
        "var actionName = 'PaymentHistory';\n"
        "}\n"
        f"{suffix}"
        "</script>"
        "</body></html>"
    )
