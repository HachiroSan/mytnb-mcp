"""Offline tests for the login/SSO flow (httpx.MockTransport, invented data).

No real credentials, tokens or portal responses are used; every value here is
fabricated and clearly non-secret.
"""

from __future__ import annotations

import html
import unittest
from typing import Any, cast
from urllib.parse import parse_qs

import httpx

from mytnb_sdk import (
    ACCOUNTS_PATH,
    INDIVIDUAL_DASHBOARD_URL,
    LOGIN_PAGE_URL,
    LOGIN_POST_URL,
    SSO_HANDLER_URL,
    MyTNBClient,
)
from mytnb_sdk.errors import (
    AuthenticationChallengeError,
    AuthenticationError,
    TransportError,
    UnexpectedResponseError,
    UnsafeRedirectError,
    ValidationError,
)

USERNAME = "user@example.test"
PASSWORD = "P@ss w&rd=1+%"
SSO_FIELDS = (
    ("USERNAME", USERNAME),
    ("USERPWD", "sso-handoff-secret"),
    ("ACTION_KEY", "ACTION-1"),
    ("CHANNEL_KEY", "WEB"),
    ("USERTOKEN", "TOKEN-ABC/1+="),
    ("SIGNATURE", "SIG-XYZ&="),
    ("RETURN_URL", "https://myaccount.mytnb.com.my/"),
    ("CTA_LINK", "/IndividualDashboard"),
)

LOGIN_PAGE = (
    '<html><body><form method="post" '
    'action="https://www.mytnb.com.my/api/sitecore/Account/Login">'
    '<input type="text" name="Email"><input type="password" name="Password">'
    "</form></body></html>"
)
CHALLENGE_PAGE = '<html><body><div class="g-recaptcha">Complete the CAPTCHA</div></body></html>'
OTP_PAGE = "<html><body>Enter the one-time password we sent you</body></html>"


def sso_form_html(
    fields: tuple[tuple[str, str], ...] | None = None,
    *,
    action: str = SSO_HANDLER_URL,
    method: str = "post",
    extra_inputs: str = "",
) -> str:
    items = SSO_FIELDS if fields is None else fields
    inputs = "".join(
        '<input type="hidden" name="{n}" value="{v}">'.format(
            n=html.escape(name, quote=True), v=html.escape(value, quote=True)
        )
        for name, value in items
    )
    return (
        "<html><body><form method={m} action={a}>{i}{e}</form></body></html>".format(
            m=html.escape(method, quote=True),
            a=html.escape(action, quote=True),
            i=inputs,
            e=extra_inputs,
        )
    )


def _body(request: httpx.Request) -> dict[str, str]:
    parsed = parse_qs(request.content.decode("utf-8"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items()}


class _Recorder:
    """Dispatches the observed flow and records every request."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.login_page: Any = httpx.Response(
            200, text=LOGIN_PAGE, headers=[("set-cookie", "LoginSession=abc; Path=/")]
        )
        self.credential: Any = httpx.Response(
            200, text=sso_form_html(), headers=[("set-cookie", "LoginStage=1; Path=/")]
        )
        self.sso: Any = httpx.Response(
            200, text="<html>ok</html>", headers=[("set-cookie", "PortalAuth=xyz; Path=/")]
        )
        self.dashboard: Any = httpx.Response(200, text="<html>dashboard</html>")
        self.accounts: Any = httpx.Response(200, json={"records": [], "total": 0})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        method = request.method
        if method == "GET" and request.url.host == "www.mytnb.com.my":
            return _resolve(self.login_page, request)
        if method == "POST" and url == LOGIN_POST_URL:
            return _resolve(self.credential, request)
        if method == "POST" and url == SSO_HANDLER_URL:
            return _resolve(self.sso, request)
        if method == "GET" and url == INDIVIDUAL_DASHBOARD_URL:
            return _resolve(self.dashboard, request)
        if method == "GET" and request.url.path == ACCOUNTS_PATH:
            return _resolve(self.accounts, request)
        raise AssertionError(f"unexpected request: {method} {url}")


def _resolve(value: Any, request: httpx.Request) -> httpx.Response:
    if callable(value):
        return cast(httpx.Response, value(request))
    assert isinstance(value, httpx.Response)
    return value


def _client(recorder: _Recorder) -> MyTNBClient:
    return MyTNBClient(transport=httpx.MockTransport(recorder))


class SuccessfulLoginTests(unittest.TestCase):
    def test_request_order_bodies_and_cookies(self) -> None:
        rec = _Recorder()
        with _client(rec) as client:
            result = client.login(USERNAME, PASSWORD)
            self.assertIsNone(result)

        methods_urls = [(c.method, str(c.url)) for c in rec.calls]
        self.assertEqual(
            methods_urls,
            [
                ("GET", LOGIN_PAGE_URL),
                ("POST", LOGIN_POST_URL),
                ("POST", SSO_HANDLER_URL),
                ("GET", INDIVIDUAL_DASHBOARD_URL),
                ("GET", f"https://myaccount.mytnb.com.my{ACCOUNTS_PATH}?page=1&limit=1"),
            ],
        )

        credential = rec.calls[1]
        self.assertTrue(
            credential.headers["content-type"].startswith("application/x-www-form-urlencoded")
        )
        self.assertEqual(_body(credential), {"Email": USERNAME, "Password": PASSWORD})
        self.assertIn("LoginSession=abc", credential.headers.get("cookie", ""))

        sso = rec.calls[2]
        self.assertNotIn("LoginSession=abc", sso.headers.get("cookie", ""))
        self.assertEqual(_body(sso), dict(SSO_FIELDS))

        dashboard = rec.calls[3]
        self.assertIn("PortalAuth=xyz", dashboard.headers.get("cookie", ""))

        accounts = rec.calls[4]
        self.assertIn("PortalAuth=xyz", accounts.headers.get("cookie", ""))
        self.assertEqual(accounts.url.params["page"], "1")
        self.assertEqual(accounts.url.params["limit"], "1")

    def test_password_special_characters_are_form_encoded_verbatim(self) -> None:
        tricky = "a&b=c+d%#e f/g?"
        rec = _Recorder()
        with _client(rec) as client:
            client.login(USERNAME, tricky)
        body = rec.calls[1].content.decode("utf-8")
        self.assertIn("%", body)
        self.assertEqual(_body(rec.calls[1])["Password"], tricky)

    def test_sso_fields_forwarded_verbatim(self) -> None:
        rec = _Recorder()
        with _client(rec) as client:
            client.login(USERNAME, PASSWORD)
        self.assertEqual(_body(rec.calls[2]), dict(SSO_FIELDS))

    def test_sso_otp_description_with_valid_accounts_succeeds(self) -> None:
        # The SSO page merely *mentions* OTP in explanatory text; the
        # authoritative account probe succeeds, so login must succeed.
        rec = _Recorder()
        rec.sso = httpx.Response(200, text=OTP_PAGE)
        with _client(rec) as client:
            self.assertIsNone(client.login(USERNAME, PASSWORD))
        self.assertEqual(len(rec.calls), 5)

    def test_instance_stores_no_credentials_or_tokens(self) -> None:
        rec = _Recorder()
        with _client(rec) as client:
            client.login(USERNAME, PASSWORD)
            state = repr(vars(client))
            self.assertNotIn(PASSWORD, state)
            self.assertNotIn(dict(SSO_FIELDS)["USERTOKEN"], state)


class FlowRejectionTests(unittest.TestCase):
    def test_credential_redirect_is_not_followed(self) -> None:
        rec = _Recorder()
        rec.credential = httpx.Response(302, headers={"location": "https://evil.example/steal"})
        with _client(rec) as client:
            with self.assertRaises(UnexpectedResponseError):
                client.login(USERNAME, PASSWORD)
        self.assertEqual([str(c.url) for c in rec.calls], [LOGIN_PAGE_URL, LOGIN_POST_URL])

    def test_injected_follow_redirects_client_still_no_cross_origin(self) -> None:
        rec = _Recorder()
        rec.credential = httpx.Response(302, headers={"location": "https://evil.example/steal"})
        with httpx.Client(transport=httpx.MockTransport(rec), follow_redirects=True) as shared:
            with MyTNBClient(http_client=shared) as client:
                with self.assertRaises(UnexpectedResponseError):
                    client.login(USERNAME, PASSWORD)
        self.assertEqual([str(c.url) for c in rec.calls], [LOGIN_PAGE_URL, LOGIN_POST_URL])

    def test_login_page_off_origin_redirect_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://evil.example/"})

        with MyTNBClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(UnsafeRedirectError):
                client.login(USERNAME, PASSWORD)

    def test_login_page_same_origin_redirect_followed(self) -> None:
        seen: list[str] = []

        def login_page(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if str(request.url) == LOGIN_PAGE_URL:
                return httpx.Response(302, headers={"location": "/login"})
            return httpx.Response(200, text=LOGIN_PAGE)

        rec = _Recorder()
        rec.login_page = login_page
        with _client(rec) as client:
            client.login(USERNAME, PASSWORD)
        self.assertEqual(seen, [LOGIN_PAGE_URL, "https://www.mytnb.com.my/login"])

    def test_sso_form_wrong_origin_rejected_before_post(self) -> None:
        rec = _Recorder()
        rec.credential = httpx.Response(
            200, text=sso_form_html(action="https://evil.example/SSO/SSOHandler")
        )
        with _client(rec) as client:
            with self.assertRaises(UnexpectedResponseError):
                client.login(USERNAME, PASSWORD)
        self.assertEqual([str(c.url) for c in rec.calls], [LOGIN_PAGE_URL, LOGIN_POST_URL])

    def test_sso_form_wrong_path_rejected(self) -> None:
        rec = _Recorder()
        rec.credential = httpx.Response(
            200, text=sso_form_html(action="https://myaccount.mytnb.com.my/SSO/Other")
        )
        with _client(rec) as client:
            with self.assertRaises(UnexpectedResponseError):
                client.login(USERNAME, PASSWORD)

    def test_sso_form_get_method_rejected(self) -> None:
        rec = _Recorder()
        rec.credential = httpx.Response(200, text=sso_form_html(method="get"))
        with _client(rec) as client:
            with self.assertRaises(UnexpectedResponseError):
                client.login(USERNAME, PASSWORD)

    def test_missing_required_field_rejected(self) -> None:
        rec = _Recorder()
        rec.credential = httpx.Response(
            200, text=sso_form_html(tuple(f for f in SSO_FIELDS if f[0] != "USERTOKEN"))
        )
        with _client(rec) as client:
            with self.assertRaises(UnexpectedResponseError):
                client.login(USERNAME, PASSWORD)

    def test_duplicate_field_rejected(self) -> None:
        rec = _Recorder()
        rec.credential = httpx.Response(
            200,
            text=sso_form_html(extra_inputs='<input type="hidden" name="USERTOKEN" value="dup">'),
        )
        with _client(rec) as client:
            with self.assertRaises(UnexpectedResponseError):
                client.login(USERNAME, PASSWORD)

    def test_empty_token_rejected(self) -> None:
        rec = _Recorder()
        rec.credential = httpx.Response(
            200,
            text=sso_form_html(
                tuple((name, "" if name == "USERTOKEN" else value) for name, value in SSO_FIELDS)
            ),
        )
        with _client(rec) as client:
            with self.assertRaises(UnexpectedResponseError):
                client.login(USERNAME, PASSWORD)

    def test_ambiguous_sso_forms_rejected(self) -> None:
        rec = _Recorder()
        rec.credential = httpx.Response(200, text=sso_form_html() + sso_form_html())
        with _client(rec) as client:
            with self.assertRaises(UnexpectedResponseError):
                client.login(USERNAME, PASSWORD)

    def test_login_rejection_raises_authentication_error(self) -> None:
        rec = _Recorder()
        rec.credential = httpx.Response(200, text=LOGIN_PAGE)
        with _client(rec) as client:
            with self.assertRaises(AuthenticationError):
                client.login(USERNAME, PASSWORD)

    def test_challenge_on_credential_raises_challenge_error(self) -> None:
        rec = _Recorder()
        rec.credential = httpx.Response(200, text=CHALLENGE_PAGE)
        with _client(rec) as client:
            with self.assertRaises(AuthenticationChallengeError):
                client.login(USERNAME, PASSWORD)

    def test_sso_challenge_with_denied_account_probe_is_challenge(self) -> None:
        # Challenge evidence on the SSO page + an auth-related verification
        # failure => classified as a challenge (no solving/bypass/retry).
        rec = _Recorder()
        rec.sso = httpx.Response(200, text=OTP_PAGE)
        rec.accounts = httpx.Response(401, text="denied")
        with _client(rec) as client:
            with self.assertRaises(AuthenticationChallengeError):
                client.login(USERNAME, PASSWORD)

    def test_sso_challenge_with_dashboard_401_is_challenge(self) -> None:
        rec = _Recorder()
        rec.sso = httpx.Response(200, text=CHALLENGE_PAGE)
        rec.dashboard = httpx.Response(401, text="denied")
        with _client(rec) as client:
            with self.assertRaises(AuthenticationChallengeError):
                client.login(USERNAME, PASSWORD)

    def test_sso_challenge_hint_with_transport_error_is_transport(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        rec = _Recorder()
        rec.sso = httpx.Response(200, text=OTP_PAGE)
        rec.accounts = boom
        with _client(rec) as client:
            try:
                client.login(USERNAME, PASSWORD)
            except TransportError:
                pass
            else:
                self.fail("expected TransportError, not a recast challenge")

    def test_sso_challenge_hint_with_malformed_json_is_unexpected(self) -> None:
        rec = _Recorder()
        rec.sso = httpx.Response(200, text=OTP_PAGE)
        rec.accounts = httpx.Response(
            200, text="{not json", headers={"content-type": "application/json"}
        )
        with _client(rec) as client:
            try:
                client.login(USERNAME, PASSWORD)
            except UnexpectedResponseError:
                pass
            else:
                self.fail("expected UnexpectedResponseError, not a recast challenge")

    def test_sso_challenge_hint_with_unsafe_dashboard_redirect(self) -> None:
        rec = _Recorder()
        rec.sso = httpx.Response(200, text=OTP_PAGE)
        rec.dashboard = httpx.Response(302, headers={"location": "https://evil.example/dash"})
        with _client(rec) as client:
            try:
                client.login(USERNAME, PASSWORD)
            except UnsafeRedirectError:
                pass
            else:
                self.fail("expected UnsafeRedirectError, not a recast challenge")

    def test_sso_403_no_hint_raises_authentication_error(self) -> None:
        rec = _Recorder()
        rec.sso = httpx.Response(403, text="denied")
        with _client(rec) as client:
            try:
                client.login(USERNAME, PASSWORD)
            except AuthenticationError as exc:
                self.assertNotIsInstance(exc, AuthenticationChallengeError)
            else:
                self.fail("expected AuthenticationError")

    def test_credential_401_raises_authentication_error(self) -> None:
        rec = _Recorder()
        rec.credential = httpx.Response(401, text="denied")
        with _client(rec) as client:
            with self.assertRaises(AuthenticationError):
                client.login(USERNAME, PASSWORD)

    def test_sso_403_raises_authentication_error(self) -> None:
        rec = _Recorder()
        rec.sso = httpx.Response(403, text="denied")
        with _client(rec) as client:
            with self.assertRaises(AuthenticationError):
                client.login(USERNAME, PASSWORD)

    def test_challenge_error_is_authentication_error(self) -> None:
        self.assertTrue(issubclass(AuthenticationChallengeError, AuthenticationError))


class VerificationFailureTests(unittest.TestCase):
    def test_account_verification_401_does_not_claim_success(self) -> None:
        rec = _Recorder()
        rec.accounts = httpx.Response(401, text="no session")
        with _client(rec) as client:
            with self.assertRaises(AuthenticationError):
                client.login(USERNAME, PASSWORD)

    def test_account_verification_bad_shape_raises(self) -> None:
        rec = _Recorder()
        rec.accounts = httpx.Response(200, json={"foo": 1})
        with _client(rec) as client:
            with self.assertRaises(UnexpectedResponseError):
                client.login(USERNAME, PASSWORD)

    def test_account_verification_html_raises_authentication_error(self) -> None:
        rec = _Recorder()
        rec.accounts = httpx.Response(
            200, text="<html>log in</html>", headers={"content-type": "text/html"}
        )
        with _client(rec) as client:
            with self.assertRaises(AuthenticationError):
                client.login(USERNAME, PASSWORD)


class CredentialValidationTests(unittest.TestCase):
    def _no_requests(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made for invalid credentials")

    def test_blank_or_non_string_credentials_rejected(self) -> None:
        cases: list[tuple[Any, Any]] = [
            ("", PASSWORD),
            ("   ", PASSWORD),
            (None, PASSWORD),
            (USERNAME, ""),
            (USERNAME, "   "),
            (USERNAME, None),
            (123, PASSWORD),
            (USERNAME, 123),
        ]
        with MyTNBClient(transport=httpx.MockTransport(self._no_requests)) as client:
            for email, password in cases:
                with self.subTest(email=email, password=password):
                    with self.assertRaises(ValidationError):
                        client.login(email, password)


class LeakRegressionTests(unittest.TestCase):
    def _assert_no_leak(self, exc: BaseException, *secrets: str) -> None:
        self.assertIsNone(exc.__cause__)
        message = str(exc)
        for secret in secrets:
            self.assertTrue(secret not in message, "leak regression: secret reflected in error")

    def test_transport_error_has_no_cause_or_secret(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused https://evil.example/secret-token")

        with MyTNBClient(transport=httpx.MockTransport(handler)) as client:
            try:
                client.login(USERNAME, PASSWORD)
            except TransportError as exc:
                self._assert_no_leak(exc, PASSWORD, "secret-token")
            else:
                self.fail("expected TransportError")

    def test_malformed_json_error_has_no_cause_or_body(self) -> None:
        rec = _Recorder()
        rec.accounts = httpx.Response(
            200,
            text="{not json SECRET-PERSONAL-BODY",
            headers={"content-type": "application/json"},
        )
        with _client(rec) as client:
            try:
                client.login(USERNAME, PASSWORD)
            except UnexpectedResponseError as exc:
                self._assert_no_leak(exc, PASSWORD, "SECRET-PERSONAL-BODY")
            else:
                self.fail("expected UnexpectedResponseError")

    def test_sso_validation_error_has_no_cause_or_secrets(self) -> None:
        rec = _Recorder()
        rec.credential = httpx.Response(200, text=sso_form_html(method="get"))
        with _client(rec) as client:
            try:
                client.login(USERNAME, PASSWORD)
            except UnexpectedResponseError as exc:
                self._assert_no_leak(exc, PASSWORD, dict(SSO_FIELDS)["USERTOKEN"])
            else:
                self.fail("expected UnexpectedResponseError")


if __name__ == "__main__":
    unittest.main()
