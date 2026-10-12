"""Typed errors for the :mod:`mytnb_sdk` client.

Error messages are deliberately static. They must never embed response
bodies, cookies, credentials, tokens, or full request URLs, so that a raised
exception is safe to surface in logs and tracebacks.
"""

from __future__ import annotations


class MyTNBError(Exception):
    """Base class for every error raised by :mod:`mytnb_sdk`."""


class ValidationError(MyTNBError, ValueError):
    """Client-side input or configuration failed validation.

    Inherits from :class:`ValueError` as well, so callers that treat bad
    arguments idiomatically still catch it.
    """


class TransportError(MyTNBError):
    """The HTTP transport failed (timeout, connection error, closed client)."""


class AuthenticationError(MyTNBError):
    """The session is missing or expired, a login was rejected, or an
    unexpected login/HTML page was returned."""


class AuthenticationChallengeError(AuthenticationError):
    """A CAPTCHA / OTP / two-factor challenge was presented.

    This library never attempts to bypass or retry such a challenge; the error
    is raised so the caller can complete it in a real browser session.
    """


class UnexpectedResponseError(MyTNBError):
    """The server returned a response that does not match the known contract."""


class UnsafeRedirectError(MyTNBError):
    """A redirect pointed outside the fixed official origin and was refused.

    Raised before any request (and therefore any credential) is sent to the
    untrusted location.
    """
