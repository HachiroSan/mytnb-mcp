"""Unit tests for bill parsing and mapping (no network)."""

from __future__ import annotations

import json
import unittest
from datetime import date
from decimal import Decimal
from typing import Any

from mytnb_sdk.errors import UnexpectedResponseError
from mytnb_sdk.models import BillRecord, bills_from_history
from mytnb_sdk.parsing import (
    extract_payment_history_model,
    is_login_path,
    looks_like_login_html,
)

from ._helpers import bill_entry, model_payload, payment_entry, profile_html


class HtmlModelExtractionTests(unittest.TestCase):
    def test_extracts_model(self) -> None:
        model = model_payload([bill_entry()])
        parsed = extract_payment_history_model(profile_html(model))
        self.assertEqual(parsed["CaNo"], "0000000")
        self.assertEqual(len(parsed["BillingPaymentHistory"]), 1)

    def test_tolerates_braces_and_semicolons_inside_strings(self) -> None:
        model = model_payload([bill_entry(Details="a;b{c}d", BillingNoEnc="enc;{x}y}")])
        parsed = extract_payment_history_model(profile_html(model))
        entry = parsed["BillingPaymentHistory"][0]
        self.assertEqual(entry["Details"], "a;b{c}d")
        self.assertEqual(entry["BillingNoEnc"], "enc;{x}y}")

    def test_model_text_inside_strings_is_not_a_duplicate(self) -> None:
        tricky = 'var model = {"nested": true};'
        model = model_payload([bill_entry(Details=tricky, BillingNoEnc="b{1}")])
        parsed = extract_payment_history_model(profile_html(model))
        entry = parsed["BillingPaymentHistory"][0]
        self.assertEqual(entry["Details"], tricky)

    def test_ignores_commented_assignments_preceding_active(self) -> None:
        active = model_payload([])
        html = (
            "<script>function refreshGrid() {\n"
            '//var model = {"old": true};\n'
            "/* var model = {\n"
            "   multiline: true\n"
            "}; var model = {\"block\": 1}; */\n"
            f"var model = {json.dumps(active)};\n"
            "var actionName = 'PaymentHistory';\n"
            "}</script>"
        )
        parsed = extract_payment_history_model(html)
        self.assertEqual(parsed["BillingPaymentHistory"], [])
        self.assertNotIn("old", parsed)
        self.assertNotIn("block", parsed)

    def test_uses_function_declaration_not_earlier_call(self) -> None:
        active = model_payload([])
        html = (
            "<script>"
            "document.addEventListener('click', function(){ refreshGrid(); });\n"
            "someWidget.refreshGrid();\n"
            "if (ready) { refreshGrid(); }\n"
            f"function refreshGrid() {{ var model = {json.dumps(active)}; var actionName='x'; }}"
            "</script>"
        )
        parsed = extract_payment_history_model(html)
        self.assertIn("BillingPaymentHistory", parsed)

    def test_ignores_block_comment_with_function_and_model(self) -> None:
        active = model_payload([])
        html = (
            "<script>/* function refreshGrid(){var model = {'fake': 1};} */"
            f"function refreshGrid(){{ var model = {json.dumps(active)}; var actionName='x'; }}"
            "</script>"
        )
        parsed = extract_payment_history_model(html)
        self.assertEqual(parsed["BillingPaymentHistory"], [])
        self.assertNotIn("fake", parsed)

    def test_tolerates_whitespace_and_newlines(self) -> None:
        html = (
            "<script>function  refreshGrid ( event )\n{\n"
            '  var\n  model\n  =\n  {"BillingPaymentHistory": []}\n  ;\n'
            "}</script>"
        )
        parsed = extract_payment_history_model(html)
        self.assertEqual(parsed["BillingPaymentHistory"], [])

    def test_requires_terminating_semicolon(self) -> None:
        html = (
            "<script>function refreshGrid(){"
            'var model = {"BillingPaymentHistory": []} var actionName="x";}</script>'
        )
        with self.assertRaises(UnexpectedResponseError):
            extract_payment_history_model(html)

    def test_ignores_unrelated_model_assignment(self) -> None:
        html = profile_html(
            model_payload([]),
            suffix="\nvar model = {'unrelated': true};\n",
        )
        parsed = extract_payment_history_model(html)
        self.assertIn("BillingPaymentHistory", parsed)
        self.assertNotIn("unrelated", parsed)

    def test_missing_model_rejected(self) -> None:
        with self.assertRaises(UnexpectedResponseError):
            extract_payment_history_model("<html><body>no script</body></html>")

    def test_no_refresh_grid_rejected(self) -> None:
        html = "<script>function somethingElse(){var model = {};}</script>"
        with self.assertRaises(UnexpectedResponseError):
            extract_payment_history_model(html)

    def test_ambiguous_active_model_rejected(self) -> None:
        html = (
            "<script>function refreshGrid(){"
            "var model = {}; var model = {}; var actionName='x';}</script>"
        )
        with self.assertRaises(UnexpectedResponseError):
            extract_payment_history_model(html)

    def test_malformed_json_rejected_without_leaking_document(self) -> None:
        secret = "SECRET-PERSONAL-DETAIL"
        html = (
            "<script>function refreshGrid(){"
            "var model = {not json " + secret + '}; var actionName="x";}'
            "</script>"
        )
        try:
            extract_payment_history_model(html)
        except UnexpectedResponseError as exc:
            self.assertIsNone(exc.__cause__)
            self.assertNotIn(secret, str(exc))
        else:
            self.fail("expected UnexpectedResponseError")

    def test_non_object_model_rejected(self) -> None:
        html = (
            "<script>function refreshGrid(){"
            "var model = [1,2,3]; var actionName='x';}</script>"
        )
        with self.assertRaises(UnexpectedResponseError):
            extract_payment_history_model(html)

    def test_multiple_refresh_grid_scripts_rejected(self) -> None:
        html = (
            "<script>function refreshGrid(){var model={};var actionName='x';}</script>"
            "<script>function refreshGrid(){var model={};var actionName='y';}</script>"
        )
        with self.assertRaises(UnexpectedResponseError):
            extract_payment_history_model(html)


class BillMappingTests(unittest.TestCase):
    def test_maps_bill_fields(self) -> None:
        bills = bills_from_history(
            [
                bill_entry(
                    DateField="20240520",
                    Amount="123.45",
                    BillingNoEnc="ENC-ABC",
                    IsBillCancelation=True,
                    IsBillReversal=False,
                )
            ]
        )
        self.assertEqual(len(bills), 1)
        bill = bills[0]
        self.assertEqual(bill.date, date(2024, 5, 20))
        self.assertEqual(bill.amount, Decimal("123.45"))
        self.assertEqual(bill.billing_id, "ENC-ABC")
        self.assertTrue(bill.is_cancellation)
        self.assertFalse(bill.is_reversal)

    def test_live_shaped_date_is_consistent(self) -> None:
        # DateField is YYYYMMDD and was verified against Date_Year,
        # a three-letter Date_Month and a 2-digit Date_Day for live entries.
        entry = bill_entry(DateField="20240520", Date_Year=2024, Date_Month="May", Date_Day="20")
        bill = bills_from_history([entry])[0]
        self.assertEqual(bill.date, date(2024, 5, 20))

    def test_excludes_payments(self) -> None:
        bills = bills_from_history([bill_entry(), payment_entry(), bill_entry(BillingNoEnc="B2")])
        self.assertEqual([b.billing_id for b in bills], ["ENC-BILL-1", "B2"])

    def test_boolean_is_optional(self) -> None:
        entry = bill_entry()
        del entry["IsBillCancelation"]
        del entry["IsBillReversal"]
        bill = bills_from_history([entry])[0]
        self.assertIsNone(bill.is_cancellation)
        self.assertIsNone(bill.is_reversal)

    def test_boolean_tolerates_strings_and_ints(self) -> None:
        bill = bills_from_history([bill_entry(IsBillCancelation="true", IsBillReversal=1)])[0]
        self.assertTrue(bill.is_cancellation)
        self.assertTrue(bill.is_reversal)

    def test_repr_hides_billing_id(self) -> None:
        bill = bills_from_history([bill_entry(BillingNoEnc="SECRET-BILL-ID")])[0]
        self.assertNotIn("SECRET-BILL-ID", repr(bill))

    def test_invalid_date_fields_rejected(self) -> None:
        for bad in ("2024052", "202405201", "2024-05-20", "20240230", "abcdefgh", 20240520, None):
            with self.subTest(bad=bad):
                with self.assertRaises(UnexpectedResponseError):
                    bills_from_history([bill_entry(DateField=bad)])

    def test_missing_or_invalid_amount_rejected(self) -> None:
        for bad in (None, "", "not-a-number"):
            with self.subTest(bad=bad):
                with self.assertRaises(UnexpectedResponseError):
                    bills_from_history([bill_entry(Amount=bad)])

    def test_blank_billing_id_rejected(self) -> None:
        for bad in (None, "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(UnexpectedResponseError):
                    bills_from_history([bill_entry(BillingNoEnc=bad)])

    def test_unknown_type_rejected(self) -> None:
        with self.assertRaises(UnexpectedResponseError):
            bills_from_history([bill_entry(Type="Adjustment")])

    def test_missing_type_rejected(self) -> None:
        entry = bill_entry()
        del entry["Type"]
        with self.assertRaises(UnexpectedResponseError):
            bills_from_history([entry])

    def test_non_list_history_rejected(self) -> None:
        with self.assertRaises(UnexpectedResponseError):
            bills_from_history({"not": "a list"})

    def test_bill_record_type(self) -> None:
        self.assertIsInstance(bills_from_history([bill_entry()])[0], BillRecord)


class LoginDetectionTests(unittest.TestCase):
    def test_is_login_path(self) -> None:
        self.assertTrue(is_login_path("/Account/Login"))
        self.assertTrue(is_login_path("/sso/authorize"))
        self.assertFalse(is_login_path("/AccountManagement/ViewAccountProfile"))

    def test_looks_like_login_html(self) -> None:
        self.assertTrue(looks_like_login_html('<input type="password" name="password">'))
        self.assertTrue(looks_like_login_html("<p>Your session has expired</p>"))
        self.assertFalse(looks_like_login_html(profile_html(model_payload([]))))


if __name__ == "__main__":
    unittest.main()
