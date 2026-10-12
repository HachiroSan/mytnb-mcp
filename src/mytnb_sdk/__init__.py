"""mytnb_sdk - an independent, lightweight client for the official myTNB portal.

A bounded foundation: a synchronous client, the observed login/SSO flow
(credentials supplied per call, never stored), owned-account listing, and
historical bills parsed from the portal's embedded payment-history model. It
does not implement POST history filtering, PDF retrieval, a CLI or a
scheduler, and never performs an unattended login.
"""

from __future__ import annotations

from .client import (
    ACCOUNT_ORIGIN,
    ACCOUNTS_PATH,
    BASE_URL,
    INDIVIDUAL_DASHBOARD_URL,
    LOGIN_ORIGIN,
    LOGIN_PAGE_URL,
    LOGIN_POST_URL,
    PROFILE_REDIRECT_PATH,
    PROFILE_VIEW_PATH,
    SSO_HANDLER_URL,
    MyTNBClient,
)
from .errors import (
    AuthenticationChallengeError,
    AuthenticationError,
    MyTNBError,
    TransportError,
    UnexpectedResponseError,
    UnsafeRedirectError,
    ValidationError,
)
from .models import Account, BillRecord

__version__ = "0.2.0"

__all__ = [
    "__version__",
    "ACCOUNT_ORIGIN",
    "ACCOUNTS_PATH",
    "BASE_URL",
    "INDIVIDUAL_DASHBOARD_URL",
    "LOGIN_ORIGIN",
    "LOGIN_PAGE_URL",
    "LOGIN_POST_URL",
    "PROFILE_REDIRECT_PATH",
    "PROFILE_VIEW_PATH",
    "SSO_HANDLER_URL",
    "MyTNBClient",
    "Account",
    "BillRecord",
    "MyTNBError",
    "ValidationError",
    "TransportError",
    "AuthenticationError",
    "AuthenticationChallengeError",
    "UnsafeRedirectError",
    "UnexpectedResponseError",
]
