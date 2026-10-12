"""Unit tests for :mod:`mytnb_sdk.models`."""

from __future__ import annotations

import unittest
from decimal import Decimal

from mytnb_sdk import Account
from mytnb_sdk.errors import UnexpectedResponseError


class AccountMappingTests(unittest.TestCase):
    def test_maps_minimal_fields(self) -> None:
        account = Account.from_api(
            {
                "AccountID": "opaque-id-1",
                "AccountNo": "1001",
                "AccountDescription": "Home",
                "TotalPayable": "12.34",
            }
        )
        self.assertEqual(account.account_id, "opaque-id-1")
        self.assertEqual(account.account_no, "1001")
        self.assertEqual(account.description, "Home")
        self.assertEqual(account.total_payable, Decimal("12.34"))

    def test_tolerant_optional_fields(self) -> None:
        account = Account.from_api({"AccountID": "opaque-id-2"})
        self.assertIsNone(account.account_no)
        self.assertIsNone(account.description)
        self.assertIsNone(account.total_payable)

        blank = Account.from_api(
            {"AccountID": "opaque-id-3", "AccountNo": "  ", "AccountDescription": "", "TotalPayable": ""}
        )
        self.assertIsNone(blank.account_no)
        self.assertIsNone(blank.description)
        self.assertIsNone(blank.total_payable)

    def test_ignores_unknown_fields(self) -> None:
        account = Account.from_api({"AccountID": "opaque-id-4", "SomethingElse": "ignored"})
        self.assertEqual(account.account_id, "opaque-id-4")

    def test_account_id_is_opaque_and_trimmed(self) -> None:
        account = Account.from_api({"AccountID": "  spaced id  "})
        self.assertEqual(account.account_id, "spaced id")

    def test_decimal_accepts_strings_ints_and_floats(self) -> None:
        self.assertEqual(Account.from_api({"AccountID": "a", "TotalPayable": "10.00"}).total_payable, Decimal("10.00"))
        self.assertEqual(Account.from_api({"AccountID": "a", "TotalPayable": 0}).total_payable, Decimal("0"))
        self.assertEqual(Account.from_api({"AccountID": "a", "TotalPayable": 7.5}).total_payable, Decimal("7.5"))

    def test_decimal_rejects_invalid_values(self) -> None:
        for bad in ("abc", "1.2.3", float("nan"), float("inf"), True, ["1"]):
            with self.subTest(bad=bad):
                with self.assertRaises(UnexpectedResponseError):
                    Account.from_api({"AccountID": "a", "TotalPayable": bad})

    def test_record_must_be_mapping(self) -> None:
        with self.assertRaises(UnexpectedResponseError):
            Account.from_api(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_missing_or_blank_account_id_rejected(self) -> None:
        with self.assertRaises(UnexpectedResponseError):
            Account.from_api({"AccountNo": "1001"})
        with self.assertRaises(UnexpectedResponseError):
            Account.from_api({"AccountID": "   "})

    def test_repr_suppresses_sensitive_fields(self) -> None:
        account = Account.from_api(
            {
                "AccountID": "SECRET-ACCOUNT-ID",
                "AccountNo": "SECRET-NUMBER",
                "AccountDescription": "Home",
                "TotalPayable": "1234.56",
            }
        )
        text = repr(account)
        self.assertNotIn("SECRET-ACCOUNT-ID", text)
        self.assertNotIn("SECRET-NUMBER", text)
        self.assertNotIn("Home", text)
        self.assertNotIn("1234.56", text)


if __name__ == "__main__":
    unittest.main()
