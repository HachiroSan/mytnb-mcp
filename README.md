# mytnb-sdk

**Unofficial myTNB Python SDK** for the official myTNB portal
(`https://myaccount.mytnb.com.my`, login page at `https://www.mytnb.com.my`).

This project is **not affiliated with or endorsed by TNB**. It is a small,
self-contained library built against observed public portal responses. It has
one core runtime dependency (`httpx`); the optional MCP server adds the official
MCP Python SDK. SDK callers supply credentials per login call. The MCP server
reads credentials from its environment when a tool first needs authentication;
neither stores credentials on the client or writes them to disk.

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
- `mytnb-mcp` — optional local stdio MCP server with `list_accounts` and
  `get_bills` tools.

Deliberately **not** implemented (unverified or out of scope): the POST
history-filter endpoint, PDF retrieval, a general-purpose CLI, a scheduler, a database, any
speculative backend abstraction, and any attempt to solve or bypass a
CAPTCHA/OTP/two-factor challenge. The MCP server doesn't expose payments or an
HTTP listener.

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

## MCP server

Install the optional server dependency (official MCP Python SDK 2.2.x or later,
below 3):

```sh
python -m pip install ".[mcp]"
```

Set `MYTNB_EMAIL` and `MYTNB_PASSWORD` in the environment of the process that
launches your MCP client. Don't send them in chat or tool arguments, and don't
commit them to configuration files. The server doesn't load `.env` automatically.

Launch with `mytnb-mcp`, or use the module command if your Python scripts
directory isn't on `PATH`:

```sh
python -m mytnb_mcp
```

This command waits for an MCP client on stdin; it isn't an interactive shell.
stdout carries only protocol messages, while diagnostics go to stderr.

For MCP clients that use the common `mcpServers` configuration format:

```json
{
  "mcpServers": {
    "mytnb-mcp": {
      "command": "python",
      "args": ["-m", "mytnb_mcp"]
    }
  }
}
```

Use the absolute path to the Python interpreter where you installed the package
if your MCP client uses a different environment. The launcher must pass the two
credential environment variables to its child process; client-specific settings
may be necessary for GUI launchers.

| Tool | Arguments | Result |
| --- | --- | --- |
| `list_accounts` | None | `currency: "MYR"` and `accounts`, each with `account_id`, `account_no`, `description`, `total_payable` |
| `get_bills` | `account_id` from `list_accounts`; optional `months` (1–1200, default 6, or null for all), `as_of` (`YYYY-MM-DD`, default today) | `currency: "MYR"` and `bills`, each with `date`, `amount`, `is_cancellation`, `is_reversal` |

Amounts use decimal strings to preserve precision. `total_payable` can be null;
bill flags can also be null when the portal doesn't supply them. The month
window filters available history locally, not through a server-side history
request. Bill results omit opaque billing tokens and exclude payments.

Tool discovery doesn't contact myTNB. The first valid tool call logs in once;
later calls reuse the in-memory session until shutdown or an authentication
failure. The server serializes calls to protect the portal's account-profile
session state and closes its HTTP client on shutdown. It checks that a bill's
account belongs to the logged-in user before requesting its profile.

Failures return MCP tool errors with safe messages. No call retries login or
bypasses a challenge. After a session-expired error, a subsequent call can try
authentication again; restart the server if you need to change its credentials.

Account numbers, descriptions, balances and bill history are private data that
the tools intentionally return to your MCP client and model provider. Only
connect clients you trust. stdio keeps the transport local; it doesn't prevent
the client from recording or forwarding tool results.

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

For MCP coverage, install `python -m pip install ".[dev]"` before running the
same command. Without the MCP extra, the suite skips MCP tests. These tests
cover in-process tools, mocked SDK HTTP responses, and a real stdio subprocess
handshake with no credentials or live portal requests.

## Limitations

- The client is validated against the current live portal behavior; portal
  changes may still require updates.
- Login may present a CAPTCHA/OTP challenge; this library stops and raises
  `AuthenticationChallengeError` rather than attempting to bypass it.
- Session cookies expire; re-run `login` or inject fresh cookies.
- Endpoints beyond login, account listing, and bill reading are private and
  unsupported; this library does not implement them.
- Payload shapes are validated strictly; portal changes may require updates.
