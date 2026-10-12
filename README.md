# mytnb-sdk

**Unofficial myTNB Python SDK** for the official myTNB portal
(`https://myaccount.mytnb.com.my`, login page at `https://www.mytnb.com.my`).

This project is **not affiliated with or endorsed by TNB**. It is a small,
self-contained library built against observed public portal responses. It has
one runtime dependency (`httpx`), uses only the standard library everywhere
else, and never performs unattended authentication: credentials are supplied
per call, used once, and never stored.

> The login, account-listing, and bill flows have been validated end-to-end
> against the live portal. The offline test suite uses invented fixtures only.

## Scope

Implemented:

- `MyTNBClient.login(email, password)` — the observed login/SSO hand-off,
  verified before returning.
- `MyTNBClient.list_accounts()` — owned-account listing (JSON), with bounded,
  terminating pagination.
- `MyTNBClient.get_bills(account, months=6)` — historical **bills** parsed from
  the portal's embedded payment-history model (HTML), excluding payments.

Deliberately **not** implemented (unverified or out of scope): the POST
history-filter endpoint, PDF retrieval, a CLI, a scheduler, a database, any
speculative backend abstraction, and any attempt to solve or bypass a
CAPTCHA/OTP/two-factor challenge. An MCP server is **planned but not currently
available**.

## Install

```
pip install .
```

Distribution name: `mytnb-sdk`; import name: `mytnb_sdk`. Requires Python 3.11+.

## Quick start

Never hardcode or commit credentials. Read them from the environment (or your
own secret store) at runtime:

```python
import os
from mytnb_sdk import MyTNBClient

with MyTNBClient() as client:
    client.login(os.environ["MYTNB_EMAIL"], os.environ["MYTNB_PASSWORD"])

    for account in client.list_accounts():
        for bill in client.get_bills(account, months=6):
            print(bill.date, bill.amount)  # billing_id is repr-hidden
```

The session lives only in the in-memory httpx cookie jar; the email and
password are never stored on the client, logged, or written to disk.

Alternatively, skip `login` and inject an existing browser session cookie.
Cookie values are held in memory only and sent only to the fixed official
origins:

```python
import os
from mytnb_sdk import MyTNBClient

with MyTNBClient(cookies={"<session-cookie-name>": os.environ["MYTNB_SESSION_COOKIE"]}) as client:
    accounts = client.list_accounts()
```

`Account` and `BillRecord` suppress sensitive fields from `repr()`.

## Login flow (observed)

`login(email, password)` performs these fixed-destination steps:

1. `GET https://www.mytnb.com.my/` — seeds the login-origin cookie jar.
2. `POST https://www.mytnb.com.my/api/sitecore/Account/Login` with
   `application/x-www-form-urlencoded` fields `Email` and `Password`.
3. Parse the returned HTML for the single auto-submit SSO `<form>`
   (`method="post"`, action `https://myaccount.mytnb.com.my/SSO/SSOHandler`)
   and forward its hidden fields **verbatim** in memory:
   `USERNAME`, `USERPWD`, `ACTION_KEY`, `CHANNEL_KEY`, `USERTOKEN`,
   `SIGNATURE`, `RETURN_URL`, `CTA_LINK`. No encryption/signature is
   implemented and no JavaScript is executed.
4. `POST https://myaccount.mytnb.com.my/SSO/SSOHandler` with those fields,
   which sets the portal session cookies.
5. `GET https://myaccount.mytnb.com.my/AccountManagement/IndividualDashboard` —
   mirrors the observed browser navigation.
6. Verify the session with
   `GET /AccountManagement/OwnedAccount/GetOwnedAccount?page=1&limit=1` and
   validate the `{records, total}` shape. `login` returns `None` **only** after
   this succeeds.

The read-only account probe is authoritative. A successful, valid response
means login succeeded even if the SSO page merely *mentions* CAPTCHA/OTP/2FA in
explanatory or template text. A challenge is reported only when the
dashboard/account verification fails in an auth-related way **and** the SSO page
showed challenge evidence; transport, unsafe-redirect, and malformed-response
errors are never recast as challenges.

### Safety properties

- Credential and SSO POSTs target exact, fixed HTTPS origins and paths.
- `follow_redirects` is forced off per request even on a caller-supplied client,
  so the library inspects redirects itself.
- Redirects on GETs are followed only to the exact expected origin and only a
  bounded number of times; an off-origin redirect raises `UnsafeRedirectError`
  **before** the redirected request is made.
- A redirect from the credential or SSO POST is rejected and never followed, so
  the original credentials are never replayed to a redirected location.
- The SSO form is parsed strictly: a wrong method (e.g. GET), wrong action
  origin/path, missing required fields, duplicate field names, or an empty
  token/signature all raise `UnexpectedResponseError`.
- A recognisable CAPTCHA/OTP/two-factor challenge is reported as
  `AuthenticationChallengeError` only after verification fails; it is never
  bypassed, solved, retried, or submitted.
- A rejected login or any verification failure raises a typed error and never
  reports success.

## Bill retrieval flow (observed)

`get_bills` follows the read-only route that was observed live:

1. `GET /AccountManagement/OwnedAccount/RedirectToAccountProfile?selectedAccountID=<opaque ID>`
   navigates to `GET /AccountManagement/ViewAccountProfile?selectedAccountID=<opaque ID>`.
2. `GET /AccountManagement/ViewAccountProfile?selectedAccountID=<opaque ID>&activate=PaymentHistory`
   returns HTML whose inline `refreshGrid()` script embeds an uncommented
   `var model = { ... };` JSON object.

The JSON is extracted safely with `json.JSONDecoder.raw_decode` (never `eval`),
ignoring JavaScript line/block comments and string-literal contents while
tolerating whitespace and braces/semicolons inside strings. Only the assignment
inside `function refreshGrid()` is used; missing, ambiguous, or malformed models
raise `UnexpectedResponseError` rather than returning an empty success.

Only `Type == "Bill"` entries become `BillRecord`s; `Type == "Payment"` entries
are excluded; any other type is rejected.

## `months` window

`months` filters **locally** relative to `as_of` (default: today) over whatever
history the profile page contains. It does not request expanded server history,
so requesting more months simply returns everything available. `months=None`
returns all bills. Only `DateField` (`YYYYMMDD`) is used for a bill's date; the
format-placeholder `Date` string is ignored. Billing period, usage, and due date
are intentionally not modelled.

## Errors

All errors derive from `MyTNBError` and never include response bodies, cookies,
credentials, tokens, or full URLs:

- `ValidationError` — bad arguments/configuration.
- `TransportError` — timeout, connection failure, or closed client.
- `AuthenticationError` — login rejected, session expired, or login page.
- `AuthenticationChallengeError` — CAPTCHA/OTP/two-factor presented.
- `UnsafeRedirectError` — a redirect left the fixed official origin.
- `UnexpectedResponseError` — a response did not match the known contract.

The account JSON endpoint is strict: any HTML response is treated as an expired
session. The bill route expects HTML, so it only reports `AuthenticationError`
for an actual login URL/path or a login/session-expired page; other HTML without
a valid model is reported as `UnexpectedResponseError`.

Parser and transport wrappers raise with `from None`, so upstream exceptions
that might embed request URLs, bodies, or tokens never appear in the traceback
chain.

## Tests

Mocked, offline (no live traffic, no real credentials), standard-library
`unittest` with `httpx.MockTransport`:

```
python -m unittest discover -s tests -t .
```

## Limitations

- The client is validated against the current live portal behavior; portal
  changes may still require updates.
- Login may present a CAPTCHA/OTP challenge; this library stops and raises
  `AuthenticationChallengeError` rather than attempting to bypass it.
- Session cookies expire; re-run `login` or inject fresh cookies.
- Endpoints beyond login, account listing, and bill reading are private and
  unsupported; this library does not implement them.
- Payload shapes are validated strictly; portal changes may require updates.
