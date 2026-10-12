"""Synchronous HTTP client for the official myTNB portal.

This module intentionally does **not** implement POST history filtering, PDF
retrieval, a CLI or a scheduler, and never performs an unattended login:
credentials are supplied per call, used once, and never stored. It exposes:

* ``login`` — the observed portal login/SSO hand-off, verified afterwards;
* ``list_accounts`` — owned-account listing (JSON); and
* ``get_bills`` — historical bills from the embedded payment-history model.

Any session state lives only in the supplied/in-memory httpx cookie jar.
"""

from __future__ import annotations

import calendar
import math
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

import httpx

from .errors import (
    AuthenticationChallengeError,
    AuthenticationError,
    TransportError,
    UnexpectedResponseError,
    UnsafeRedirectError,
    ValidationError,
)
from .forms import HtmlForm, parse_forms, parse_sso_form
from .models import Account, BillRecord, bills_from_history
from .parsing import (
    extract_payment_history_model,
    is_login_path,
    looks_like_challenge_html,
    looks_like_login_html,
)

__all__ = [
    "BASE_URL",
    "ACCOUNT_ORIGIN",
    "LOGIN_ORIGIN",
    "LOGIN_PAGE_URL",
    "LOGIN_POST_URL",
    "SSO_HANDLER_URL",
    "INDIVIDUAL_DASHBOARD_URL",
    "ACCOUNTS_PATH",
    "PROFILE_REDIRECT_PATH",
    "PROFILE_VIEW_PATH",
    "MyTNBClient",
]

# Fixed HTTPS origins/paths for the official portal. Never derived from input.
BASE_URL = "https://myaccount.mytnb.com.my"
ACCOUNT_ORIGIN = BASE_URL
ACCOUNTS_PATH = "/AccountManagement/OwnedAccount/GetOwnedAccount"
PROFILE_REDIRECT_PATH = "/AccountManagement/OwnedAccount/RedirectToAccountProfile"
PROFILE_VIEW_PATH = "/AccountManagement/ViewAccountProfile"

# Login happens on the public marketing origin; the SSO hand-off lands on the
# authenticated account origin. Both are fixed and exact.
LOGIN_ORIGIN = "https://www.mytnb.com.my"
LOGIN_PAGE_URL = "https://www.mytnb.com.my/"
LOGIN_POST_URL = "https://www.mytnb.com.my/api/sitecore/Account/Login"
SSO_HANDLER_URL = "https://myaccount.mytnb.com.my/SSO/SSOHandler"
INDIVIDUAL_DASHBOARD_URL = "https://myaccount.mytnb.com.my/AccountManagement/IndividualDashboard"

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_PAGES = 100
DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_REDIRECTS = 5

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Fields the observed auto-submit SSO form carries. Missing fields, duplicate
# names, empty token/signature, or a wrong action/method are rejected.
_SSO_REQUIRED_FIELDS = (
    "USERNAME",
    "USERPWD",
    "ACTION_KEY",
    "CHANNEL_KEY",
    "USERTOKEN",
    "SIGNATURE",
    "RETURN_URL",
    "CTA_LINK",
)
_SSO_NONEMPTY_FIELDS = ("USERTOKEN", "SIGNATURE")


def _validate_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("timeout must be a positive number of seconds")
    if not math.isfinite(value) or value <= 0:
        raise ValidationError("timeout must be a positive finite number of seconds")
    return float(value)


def _validate_positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} must be an integer")
    if value < 1:
        raise ValidationError(f"{name} must be a positive integer")
    return value


def _validate_credential(value: Any, name: str) -> str:
    """Validate a credential without trimming or altering it.

    The value is only ever checked for being a non-blank string; it is never
    logged and never included in an error message.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a non-blank string")
    return value


def _validate_as_of(value: Any) -> date:
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise ValidationError("as_of must be a date")


def _months_before(anchor: date, months: int) -> date:
    """Return the date ``months`` calendar months before ``anchor``.

    Used only for the client-side window; the day is clamped to the target
    month's last day when necessary.
    """
    month_index = anchor.month - 1 - months
    year = anchor.year + month_index // 12
    month = month_index % 12 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _effective_port(url: httpx.URL) -> int | None:
    if url.port is not None:
        return url.port
    if url.scheme == "https":
        return 443
    if url.scheme == "http":
        return 80
    return None


def _same_origin(url: httpx.URL, origin: httpx.URL) -> bool:
    return (
        url.scheme == origin.scheme
        and url.host == origin.host
        and _effective_port(url) == _effective_port(origin)
    )


def _is_profile_view_path(path: str) -> bool:
    return path.rstrip("/").lower() == PROFILE_VIEW_PATH.lower()


def _coerce_total(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise UnexpectedResponseError("accounts response had an invalid total")
    if isinstance(value, int):
        if value < 0:
            raise UnexpectedResponseError("accounts response had a negative total")
        return value
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return None
        try:
            total = int(trimmed)
        except ValueError:
            raise UnexpectedResponseError("accounts response had an invalid total") from None
        if total < 0:
            raise UnexpectedResponseError("accounts response had a negative total")
        return total
    raise UnexpectedResponseError("accounts response had an unsupported total type")


def _parse_accounts_payload(payload: Any) -> tuple[list[Account], int | None]:
    if not isinstance(payload, Mapping):
        raise UnexpectedResponseError("unexpected accounts response shape")

    if "records" in payload:
        raw_records = payload["records"]
    else:
        raw_records = payload.get("Records")

    if raw_records is None:
        raise UnexpectedResponseError("accounts response was missing records")
    if not isinstance(raw_records, list):
        raise UnexpectedResponseError("accounts response records was not a list")

    accounts: list[Account] = []
    for record in raw_records:
        accounts.append(Account.from_api(record))

    if "total" in payload:
        raw_total = payload["total"]
    else:
        raw_total = payload.get("Total")
    return accounts, _coerce_total(raw_total)


def _looks_like_html(response: httpx.Response) -> bool:
    try:
        text = response.text
    except (httpx.DecodingError, UnicodeDecodeError):
        return False
    return text.lstrip().startswith("<")


class MyTNBClient:
    """Synchronous myTNB client.

    Session state lives in the httpx cookie jar. Credentials passed to
    :meth:`login` are used once and never stored on the instance, logged, or
    written to disk.

    Use as a context manager to guarantee the underlying client is closed::

        with MyTNBClient() as client:
            client.login(os.environ["MYTNB_EMAIL"], os.environ["MYTNB_PASSWORD"])
            accounts = client.list_accounts()
    """

    def __init__(
        self,
        *,
        cookies: Mapping[str, str] | None = None,
        http_client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
    ) -> None:
        if http_client is not None and transport is not None:
            raise ValidationError("provide either http_client or transport, not both")

        self._timeout = _validate_timeout(timeout)
        self._max_pages = _validate_positive_int(max_pages, "max_pages")
        self._max_redirects = _validate_positive_int(max_redirects, "max_redirects")

        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            client_kwargs: dict[str, Any] = {
                "timeout": self._timeout,
                "follow_redirects": False,
            }
            if transport is not None:
                client_kwargs["transport"] = transport
            self._client = httpx.Client(**client_kwargs)
            self._owns_client = True

        if cookies is not None:
            self._apply_cookies(cookies)

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> MyTNBClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self.close()
        return False

    def close(self) -> None:
        """Close the underlying client if this instance created it."""
        if self._owns_client and not self._client.is_closed:
            self._client.close()

    # -- public API --------------------------------------------------------

    def login(self, email: str, password: str) -> None:
        """Log in using the observed portal flow and verify the session.

        Steps (all fixed destinations): GET the login page, POST
        ``Email``/``Password`` to the credential endpoint, parse the returned
        auto-submit SSO form, forward its hidden fields **verbatim** to the
        fixed SSO handler, GET the individual dashboard, then confirm the
        session by reading ``page=1&limit=1`` from the account list and
        validating its ``{records, total}`` shape.

        The read-only account probe is authoritative: a successful, valid
        response means login succeeded even if the SSO page merely *mentions*
        CAPTCHA/OTP/2FA in explanatory text. A challenge is only reported when
        the dashboard/account verification fails in an auth-related way **and**
        the SSO page showed challenge evidence; transport, unsafe-redirect and
        malformed-response errors are never recast as challenges.

        Returns ``None`` only after verification succeeds; any failure raises a
        typed error and never claims to be authenticated. The password is never
        stored on the instance or included in errors.
        """
        self._ensure_open()
        email = _validate_credential(email, "email")
        password = _validate_credential(password, "password")

        # 1. Seed the login-origin cookie jar.
        login_page = self._get_following_redirects(
            httpx.URL(LOGIN_PAGE_URL),
            httpx.URL(LOGIN_ORIGIN),
            self._max_redirects,
            expiry_on_login_path=False,
        )
        self._check_status(login_page)

        # 2. Submit credentials to the fixed endpoint (never followed on redirect).
        credential_response = self._post_form(
            LOGIN_POST_URL,
            {"Email": email, "Password": password},
            redirect_error="credential submission was unexpectedly redirected",
        )
        self._check_status(credential_response)
        sso_fields = self._extract_sso_fields(self._response_text(credential_response))

        # 3. Forward the returned hidden fields verbatim to the fixed SSO handler.
        sso_response = self._post_form(
            SSO_HANDLER_URL,
            sso_fields,
            redirect_error="SSO hand-off was unexpectedly redirected",
        )
        # Record challenge *evidence* only; it is never authoritative on its own.
        challenge_hint = looks_like_challenge_html(self._response_text(sso_response))

        # 4-5. The read-only account probe decides authentication. Only an
        # auth-related failure here, combined with challenge evidence on the SSO
        # page, is reported as a challenge. Other error classes propagate as-is.
        try:
            self._check_status(sso_response)

            # Mirror the observed browser navigation to the dashboard.
            dashboard = self._get_following_redirects(
                httpx.URL(INDIVIDUAL_DASHBOARD_URL),
                httpx.URL(ACCOUNT_ORIGIN),
                self._max_redirects,
            )
            self._check_status(dashboard)

            # Verify the session before reporting success.
            payload = self._request_json(ACCOUNTS_PATH, {"page": 1, "limit": 1})
            _parse_accounts_payload(payload)
        except AuthenticationError:
            if challenge_hint:
                raise AuthenticationChallengeError(
                    "an authentication challenge (CAPTCHA/OTP) was presented and the "
                    "session could not be verified"
                ) from None
            raise
        return None

    def list_accounts(
        self,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        start_page: int = 1,
        max_pages: int | None = None,
    ) -> list[Account]:
        """List all owned accounts, paging until the contract says to stop.

        Paging terminates when a page is empty, when the reported total is
        reached, when a short page is returned, or when ``max_pages`` is
        exhausted (which raises rather than looping forever).
        """
        page_size = _validate_positive_int(page_size, "page_size")
        start_page = _validate_positive_int(start_page, "start_page")
        if max_pages is None:
            limit_pages = self._max_pages
        else:
            limit_pages = _validate_positive_int(max_pages, "max_pages")

        accounts: list[Account] = []
        last_page = start_page + limit_pages - 1
        page = start_page
        while page <= last_page:
            payload = self._request_json(ACCOUNTS_PATH, {"page": page, "limit": page_size})
            page_accounts, total = _parse_accounts_payload(payload)
            accounts.extend(page_accounts)

            if not page_accounts:
                return accounts
            if total is not None and len(accounts) >= total:
                return accounts
            if len(page_accounts) < page_size:
                return accounts
            page += 1

        raise UnexpectedResponseError("pagination exceeded configured maximum pages")

    def get_bills(
        self,
        account: Account,
        *,
        months: int | None = 6,
        as_of: date | None = None,
        max_redirects: int | None = None,
    ) -> list[BillRecord]:
        """Return historical bills for ``account`` from the embedded model.

        The observed live sequence is: ``RedirectToAccountProfile`` (which
        navigates to ``ViewAccountProfile``), then ``ViewAccountProfile`` with
        ``activate=PaymentHistory``, whose inline ``refreshGrid`` script embeds
        the payment-history JSON. The redirect flow is followed manually and
        only within the fixed official origin.

        ``months`` filters **locally** relative to ``as_of`` (default: today).
        This is a client-side window over whatever history the page happens to
        contain; it does not request expanded server history. ``months=None``
        returns every available bill.
        """
        self._ensure_open()
        if not isinstance(account, Account):
            raise ValidationError("account must be an Account instance")

        window = None if months is None else _validate_positive_int(months, "months")
        anchor = _validate_as_of(as_of)
        redirects = (
            self._max_redirects
            if max_redirects is None
            else _validate_positive_int(max_redirects, "max_redirects")
        )

        profile_url = self._resolve_profile_url(account.account_id, redirects)
        history_url = profile_url.copy_set_param("activate", "PaymentHistory")
        response = self._get_following_redirects(history_url, httpx.URL(BASE_URL), redirects)
        self._check_status(response)

        if is_login_path(response.url.path):
            raise AuthenticationError("session expired or login required")

        html = self._response_text(response)
        try:
            model = extract_payment_history_model(html)
        except UnexpectedResponseError:
            if looks_like_login_html(html):
                raise AuthenticationError("session expired or login required")
            raise

        if "BillingPaymentHistory" not in model:
            raise UnexpectedResponseError("payment history model was missing BillingPaymentHistory")
        bills = bills_from_history(model["BillingPaymentHistory"])

        if window is not None:
            cutoff = _months_before(anchor, window)
            bills = [bill for bill in bills if bill.date >= cutoff]
        return bills

    # -- internals ---------------------------------------------------------

    def _apply_cookies(self, cookies: Mapping[str, str]) -> None:
        if not isinstance(cookies, Mapping):
            raise ValidationError("cookies must be a mapping of names to values")
        clean: dict[str, str] = {}
        for name, value in cookies.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValidationError("cookie names and values must be strings")
            clean[name] = value
        # In-memory only; never written to disk or logged.
        self._client.cookies.update(clean)

    def _ensure_open(self) -> None:
        if self._client.is_closed:
            raise TransportError("client is closed")

    def _response_text(self, response: httpx.Response) -> str:
        try:
            return response.text
        except (httpx.DecodingError, UnicodeDecodeError):
            raise UnexpectedResponseError("response body could not be decoded") from None

    def _raw_get(self, url: httpx.URL) -> httpx.Response:
        self._ensure_open()
        try:
            # follow_redirects is forced off per request so redirects are
            # inspected here even when the caller injected a client with
            # follow_redirects=True.
            return self._client.get(url, timeout=self._timeout, follow_redirects=False)
        except httpx.TimeoutException:
            raise TransportError("request timed out") from None
        except httpx.TransportError:
            raise TransportError("request failed") from None

    def _raw_post(self, url: str, data: Mapping[str, str]) -> httpx.Response:
        self._ensure_open()
        try:
            return self._client.post(url, data=data, timeout=self._timeout, follow_redirects=False)
        except httpx.TimeoutException:
            raise TransportError("request timed out") from None
        except httpx.TransportError:
            raise TransportError("request failed") from None

    def _post_form(
        self,
        url: str,
        fields: Mapping[str, str],
        *,
        redirect_error: str,
    ) -> httpx.Response:
        """POST form fields to a fixed destination, rejecting any redirect.

        Redirects from credential/SSO submissions are never followed, so the
        original credentials are never replayed to a redirected location.
        """
        response = self._raw_post(url, fields)
        if response.is_redirect or response.status_code in _REDIRECT_STATUSES:
            raise UnexpectedResponseError(redirect_error)
        return response

    def _check_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status in (401, 403):
            raise AuthenticationError("session expired or login required")
        if status >= 400:
            raise UnexpectedResponseError(f"unexpected HTTP status {status}")

    def _get_following_redirects(
        self,
        url: httpx.URL,
        origin: httpx.URL,
        max_redirects: int,
        *,
        expiry_on_login_path: bool = True,
    ) -> httpx.Response:
        """GET ``url``, following only bounded redirects to ``origin``.

        The exact origin is enforced **before** the redirected request is made,
        so cookies are never sent off-origin.
        """
        current = url
        for _ in range(max_redirects + 1):
            response = self._raw_get(current)
            if not (response.is_redirect or response.status_code in _REDIRECT_STATUSES):
                return response

            location = response.headers.get("location")
            if not location:
                raise UnexpectedResponseError("redirect response was missing a Location header")
            target = current.join(location)
            if not _same_origin(target, origin):
                raise UnsafeRedirectError("refused to follow a redirect to a different origin")
            if expiry_on_login_path and is_login_path(target.path):
                raise AuthenticationError("session expired or login required")
            current = target

        raise UnexpectedResponseError("too many redirects")

    def _extract_sso_fields(self, html: str) -> dict[str, str]:
        """Find and strictly validate the single SSO hand-off form."""
        forms = parse_forms(html)
        candidates = [
            form
            for form in forms
            if any(field.name in _SSO_REQUIRED_FIELDS for field in form.fields)
        ]
        if not candidates:
            if looks_like_challenge_html(html):
                raise AuthenticationChallengeError(
                    "a login challenge (CAPTCHA/OTP) was presented"
                )
            raise AuthenticationError("login was rejected or the login flow changed")
        if len(candidates) > 1:
            raise UnexpectedResponseError("multiple candidate SSO forms were present")
        return parse_sso_form(
            candidates[0],
            expected_url=SSO_HANDLER_URL,
            required_fields=_SSO_REQUIRED_FIELDS,
            nonempty_fields=_SSO_NONEMPTY_FIELDS,
        )

    def _resolve_profile_url(self, account_id: str, max_redirects: int) -> httpx.URL:
        start = httpx.URL(BASE_URL + PROFILE_REDIRECT_PATH).copy_set_param(
            "selectedAccountID", account_id
        )
        response = self._get_following_redirects(start, httpx.URL(BASE_URL), max_redirects)
        self._check_status(response)
        if not _is_profile_view_path(response.url.path):
            raise UnexpectedResponseError(
                "account redirect did not resolve to the account profile view"
            )
        return response.url

    def _request_json(self, path: str, params: Mapping[str, Any]) -> Any:
        # Absolute URL from the fixed HTTPS base; params are passed to httpx so
        # it handles encoding without us interpolating untrusted values.
        url = httpx.URL(BASE_URL + path).copy_merge_params(params)
        response = self._raw_get(url)

        status = response.status_code
        if response.is_redirect or status in _REDIRECT_STATUSES:
            raise AuthenticationError("session expired or login required")
        self._check_status(response)

        content_type = (response.headers.get("content-type") or "").lower()
        if "text/html" in content_type:
            raise AuthenticationError("received an HTML login or session page")

        try:
            return response.json()
        except ValueError:
            # from None so the JSONDecodeError (which embeds a body snippet)
            # never appears in the traceback chain.
            if _looks_like_html(response):
                raise AuthenticationError("received an HTML login or session page") from None
            raise UnexpectedResponseError("response was not valid JSON") from None
