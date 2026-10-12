"""Unit tests for :mod:`mytnb_sdk.client` using httpx.MockTransport.

No live traffic is generated: every request is served by an in-process mock
transport.
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from mytnb_sdk import (
    ACCOUNTS_PATH,
    BASE_URL,
    PROFILE_REDIRECT_PATH,
    PROFILE_VIEW_PATH,
    Account,
    MyTNBClient,
)
from mytnb_sdk.errors import (
    AuthenticationError,
    TransportError,
    UnexpectedResponseError,
    UnsafeRedirectError,
    ValidationError,
)

from ._helpers import bill_entry, model_payload, payment_entry, profile_html


def _mock_client(handler: Any, **kwargs: Any) -> MyTNBClient:
    return MyTNBClient(transport=httpx.MockTransport(handler), **kwargs)


def _record(account_id: str, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "AccountID": account_id,
        "AccountNo": "1001",
        "AccountDescription": "Home",
        "TotalPayable": "12.34",
    }
    record.update(overrides)
    return record


def _bills_handler(entries: list[dict[str, Any]], captured: list[httpx.Request] | None = None):
    model = model_payload(entries)

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        path = request.url.path
        if path == PROFILE_REDIRECT_PATH:
            account_id = request.url.params.get("selectedAccountID", "")
            location = f"{PROFILE_VIEW_PATH}?selectedAccountID={quote(account_id, safe='')}"
            return httpx.Response(302, headers={"location": location})
        if path == PROFILE_VIEW_PATH:
            return httpx.Response(
                200,
                text=profile_html(model),
                headers={"content-type": "text/html; charset=utf-8"},
            )
        raise AssertionError(f"unexpected path {path}")

    return handler


class PaginationTests(unittest.TestCase):
    def test_maps_and_paginates_until_total(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            self.assertEqual(request.url.scheme, "https")
            self.assertEqual(request.url.host, "myaccount.mytnb.com.my")
            self.assertEqual(request.url.path, ACCOUNTS_PATH)
            page = int(request.url.params["page"])
            if page == 1:
                return httpx.Response(
                    200,
                    json={"records": [_record("A1"), _record("A2")], "total": 3},
                )
            if page == 2:
                return httpx.Response(200, json={"records": [_record("A3")], "total": 3})
            raise AssertionError(f"unexpected page {page}")

        with _mock_client(handler) as client:
            accounts = client.list_accounts(page_size=2)
            self.assertEqual([a.account_id for a in accounts], ["A1", "A2", "A3"])
            self.assertEqual(accounts[0].total_payable, Decimal("12.34"))
        self.assertEqual(len(seen), 2)
        self.assertEqual(int(seen[0].url.params["limit"]), 2)

    def test_stops_on_short_page_without_total(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params["page"])
            if page == 1:
                return httpx.Response(200, json={"records": [_record("A1")]})
            raise AssertionError("should not request a second page")

        with _mock_client(handler) as client:
            accounts = client.list_accounts(page_size=5)
            self.assertEqual([a.account_id for a in accounts], ["A1"])

    def test_stops_on_empty_page(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"records": [], "total": 0})

        with _mock_client(handler) as client:
            self.assertEqual(client.list_accounts(), [])

    def test_pagination_is_bounded(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            # Always a full page, never a total: would loop forever unbounded.
            return httpx.Response(200, json={"records": [_record(f"A{calls['n']}")]})

        with _mock_client(handler, max_pages=3) as client:
            with self.assertRaises(UnexpectedResponseError):
                client.list_accounts(page_size=1)
        self.assertEqual(calls["n"], 3)


class ResponseShapeTests(unittest.TestCase):
    def _client_returning(self, payload: Any, status: int = 200) -> MyTNBClient:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json=payload)

        return _mock_client(handler)

    def test_non_mapping_payload_rejected(self) -> None:
        with self._client_returning(["not", "a", "mapping"]) as client:
            with self.assertRaises(UnexpectedResponseError):
                client.list_accounts()

    def test_missing_records_rejected(self) -> None:
        with self._client_returning({"total": 0}) as client:
            with self.assertRaises(UnexpectedResponseError):
                client.list_accounts()

    def test_records_not_a_list_rejected(self) -> None:
        with self._client_returning({"records": {"not": "a list"}}) as client:
            with self.assertRaises(UnexpectedResponseError):
                client.list_accounts()

    def test_invalid_total_rejected(self) -> None:
        with self._client_returning({"records": [], "total": "many"}) as client:
            with self.assertRaises(UnexpectedResponseError):
                client.list_accounts()

    def test_bad_record_rejected(self) -> None:
        with self._client_returning({"records": [{"AccountNo": "1001"}]}) as client:
            with self.assertRaises(UnexpectedResponseError):
                client.list_accounts()


class SessionExpiryTests(unittest.TestCase):
    def test_html_content_type_raises_authentication_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<html><body>Please log in SUPERSECRETTOKEN</body></html>",
                headers={"content-type": "text/html; charset=utf-8"},
            )

        with _mock_client(handler) as client:
            try:
                client.list_accounts()
            except AuthenticationError as exc:
                self.assertNotIn("SUPERSECRETTOKEN", str(exc))
            else:
                self.fail("expected AuthenticationError")

    def test_html_body_without_content_type_raises_authentication_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>login</html>")

        with _mock_client(handler) as client:
            with self.assertRaises(AuthenticationError):
                client.list_accounts()

    def test_redirect_raises_authentication_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://sso.example/login"})

        with _mock_client(handler) as client:
            with self.assertRaises(AuthenticationError):
                client.list_accounts()

    def test_unauthorized_status_raises_authentication_error(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                def handler(request: httpx.Request, status: int = status) -> httpx.Response:
                    return httpx.Response(status, text="denied")

                with _mock_client(handler) as client:
                    with self.assertRaises(AuthenticationError):
                        client.list_accounts()


class SafeErrorTests(unittest.TestCase):
    def test_error_message_excludes_body_url_and_cookie(self) -> None:
        secret_body = "SUPER-SECRET-BODY"
        secret_cookie = "SUPER-SECRET-COOKIE"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text=secret_body)

        with _mock_client(handler, cookies={"SessionToken": secret_cookie}) as client:
            try:
                client.list_accounts()
            except UnexpectedResponseError as exc:
                message = str(exc)
                self.assertNotIn(secret_body, message)
                self.assertNotIn(secret_cookie, message)
                self.assertNotIn(ACCOUNTS_PATH, message)
            else:
                self.fail("expected UnexpectedResponseError")

    def test_transport_error_is_typed_and_safe(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused at https://internal/secret")

        with _mock_client(handler) as client:
            try:
                client.list_accounts()
            except TransportError as exc:
                self.assertNotIn("internal/secret", str(exc))
            else:
                self.fail("expected TransportError")


class BillsClientTests(unittest.TestCase):
    def _entries(self) -> list[dict[str, Any]]:
        return [
            bill_entry(DateField="20240520", Amount="10.00", BillingNoEnc="B1"),
            bill_entry(DateField="20240210", Amount="20.00", BillingNoEnc="B3"),
            bill_entry(DateField="20231231", Amount="30.00", BillingNoEnc="B2"),
            payment_entry(DateField="20240601", Amount="5.00", BillingNoEnc="P1"),
        ]

    def test_fetches_filters_and_follows_observed_sequence(self) -> None:
        captured: list[httpx.Request] = []
        with _mock_client(_bills_handler(self._entries(), captured)) as client:
            bills = client.get_bills(Account(account_id="opaque-1"), months=6, as_of=date(2024, 7, 1))
            self.assertEqual([b.billing_id for b in bills], ["B1", "B3"])
            self.assertEqual(bills[0].amount, Decimal("10.00"))
            self.assertEqual(bills[0].date, date(2024, 5, 20))

        self.assertEqual(captured[0].url.path, PROFILE_REDIRECT_PATH)
        self.assertEqual(captured[0].url.params["selectedAccountID"], "opaque-1")
        self.assertEqual(captured[1].url.path, PROFILE_VIEW_PATH)
        self.assertEqual(captured[2].url.path, PROFILE_VIEW_PATH)
        self.assertEqual(captured[2].url.params["activate"], "PaymentHistory")

    def test_months_window_variants(self) -> None:
        for months, expected in ((3, ["B1"]), (6, ["B1", "B3"]), (None, ["B1", "B3", "B2"])):
            with self.subTest(months=months):
                with _mock_client(_bills_handler(self._entries())) as client:
                    bills = client.get_bills(
                        Account(account_id="opaque-1"), months=months, as_of=date(2024, 7, 1)
                    )
                    self.assertEqual([b.billing_id for b in bills], expected)

    def test_account_parameter_is_encoded(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == PROFILE_REDIRECT_PATH:
                seen["raw_query"] = request.url.query.decode("ascii")
                seen["decoded"] = request.url.params["selectedAccountID"]
                return httpx.Response(
                    302, headers={"location": f"{PROFILE_VIEW_PATH}?selectedAccountID=opaque"}
                )
            return httpx.Response(
                200,
                text=profile_html(model_payload([])),
                headers={"content-type": "text/html"},
            )

        account_id = "id with space&query=1/slash"
        with _mock_client(handler) as client:
            client.get_bills(Account(account_id=account_id))
        self.assertEqual(seen["decoded"], account_id)
        self.assertIn("%", seen["raw_query"])

    def test_off_origin_redirect_is_rejected_before_contact(self) -> None:
        contacted: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            contacted.append(request.url.host)
            return httpx.Response(302, headers={"location": "https://evil.example/steal"})

        with _mock_client(handler) as client:
            with self.assertRaises(UnsafeRedirectError):
                client.get_bills(Account(account_id="opaque-1"))
        self.assertEqual(contacted, ["myaccount.mytnb.com.my"])

    def test_off_origin_redirect_rejected_despite_follow_redirects_client(self) -> None:
        contacted: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            contacted.append(request.url.host)
            return httpx.Response(302, headers={"location": "https://evil.example/steal"})

        mock = httpx.MockTransport(handler)
        with httpx.Client(transport=mock, follow_redirects=True) as shared:
            with MyTNBClient(http_client=shared) as client:
                with self.assertRaises(UnsafeRedirectError):
                    client.get_bills(Account(account_id="opaque-1"))
        self.assertEqual(contacted, ["myaccount.mytnb.com.my"])

    def test_login_redirect_raises_authentication_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "/Account/Login"})

        with _mock_client(handler) as client:
            with self.assertRaises(AuthenticationError):
                client.get_bills(Account(account_id="opaque-1"))

    def test_login_html_raises_authentication_error(self) -> None:
        login_html = '<html><body><input type="password" name="password"></body></html>'

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == PROFILE_REDIRECT_PATH:
                return httpx.Response(
                    302, headers={"location": f"{PROFILE_VIEW_PATH}?selectedAccountID=x"}
                )
            return httpx.Response(200, text=login_html, headers={"content-type": "text/html"})

        with _mock_client(handler) as client:
            with self.assertRaises(AuthenticationError):
                client.get_bills(Account(account_id="opaque-1"))

    def test_plain_html_without_model_is_unexpected_not_expiry(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == PROFILE_REDIRECT_PATH:
                return httpx.Response(
                    302, headers={"location": f"{PROFILE_VIEW_PATH}?selectedAccountID=x"}
                )
            return httpx.Response(
                200, text="<html><body>maintenance</body></html>", headers={"content-type": "text/html"}
            )

        with _mock_client(handler) as client:
            with self.assertRaises(UnexpectedResponseError):
                client.get_bills(Account(account_id="opaque-1"))

    def test_redirect_endpoint_must_resolve_to_profile(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>nope</html>", headers={"content-type": "text/html"})

        with _mock_client(handler) as client:
            with self.assertRaises(UnexpectedResponseError):
                client.get_bills(Account(account_id="opaque-1"))

    def test_redirect_loop_is_bounded(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": PROFILE_REDIRECT_PATH})

        with _mock_client(handler, max_redirects=3) as client:
            with self.assertRaises(UnexpectedResponseError):
                client.get_bills(Account(account_id="opaque-1"))

    def test_closed_client_raises_transport_error(self) -> None:
        client = _mock_client(_bills_handler([]))
        with client:
            client.get_bills(Account(account_id="opaque-1"))
        with self.assertRaises(TransportError):
            client.get_bills(Account(account_id="opaque-1"))

    def test_account_type_is_validated(self) -> None:
        with _mock_client(_bills_handler([])) as client:
            with self.assertRaises(ValidationError):
                client.get_bills("not-an-account")  # type: ignore[arg-type]
            with self.assertRaises(ValidationError):
                client.get_bills(Account(account_id="x"), months=0)
            with self.assertRaises(ValidationError):
                client.get_bills(Account(account_id="x"), as_of="2024-01-01")  # type: ignore[arg-type]


class LifecycleTests(unittest.TestCase):
    def test_closed_client_raises_transport_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"records": [], "total": 0})

        client = _mock_client(handler)
        with client:
            self.assertEqual(client.list_accounts(), [])
        with self.assertRaises(TransportError):
            client.list_accounts()

    def test_cookies_are_injected_on_requests(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["cookie"] = request.headers.get("cookie", "")
            return httpx.Response(200, json={"records": [], "total": 0})

        with _mock_client(handler, cookies={"SessionToken": "abc123"}) as client:
            client.list_accounts()
        self.assertIn("SessionToken=abc123", captured["cookie"])


class InputValidationTests(unittest.TestCase):
    def _handler(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"records": [], "total": 0})

    def test_invalid_pagination_arguments(self) -> None:
        cases: list[dict[str, Any]] = [
            {"page_size": 0},
            {"page_size": -1},
            {"page_size": "5"},
            {"page_size": True},
            {"start_page": 0},
            {"max_pages": 0},
        ]
        with _mock_client(self._handler) as client:
            for kwargs in cases:
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(ValidationError):
                        client.list_accounts(**kwargs)

    def test_invalid_constructor_arguments(self) -> None:
        with self.assertRaises(ValidationError):
            MyTNBClient(timeout=0)
        with self.assertRaises(ValidationError):
            MyTNBClient(timeout=None)  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            MyTNBClient(max_pages=0)
        with self.assertRaises(ValidationError):
            MyTNBClient(max_redirects=0)

    def test_http_client_and_transport_are_mutually_exclusive(self) -> None:
        with httpx.Client() as shared:
            with self.assertRaises(ValidationError):
                MyTNBClient(http_client=shared, transport=httpx.MockTransport(self._handler))

    def test_invalid_cookies_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            MyTNBClient(cookies={"name": 123})  # type: ignore[dict-item]
        with self.assertRaises(ValidationError):
            MyTNBClient(
                transport=httpx.MockTransport(self._handler),
                cookies="not-a-mapping",  # type: ignore[arg-type]
            )

    def test_base_url_is_fixed_https(self) -> None:
        self.assertEqual(BASE_URL, "https://myaccount.mytnb.com.my")
        self.assertTrue(BASE_URL.startswith("https://"))


if __name__ == "__main__":
    unittest.main()
