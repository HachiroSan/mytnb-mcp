"""Read-only MCP tools. Credentials never enter tool arguments or results."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date
from functools import partial
from threading import Lock
from typing import Annotated, Any

from anyio.to_thread import run_sync
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from mytnb_sdk import MyTNBClient
from mytnb_sdk.errors import AuthenticationChallengeError, AuthenticationError, MyTNBError


class _Session:
    """Serialize the synchronous client's session-sensitive profile requests."""

    def __init__(self, client_factory: Callable[[], MyTNBClient]) -> None:
        self._factory = client_factory
        self._client: MyTNBClient | None = None
        self._authenticated = False
        self._lock = Lock()

    def execute(self, operation: Callable[[MyTNBClient], dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            try:
                if not self._authenticated:
                    email = os.environ.get("MYTNB_EMAIL", "")
                    password = os.environ.get("MYTNB_PASSWORD", "")
                    if not email.strip() or not password.strip():
                        raise ToolError("Set MYTNB_EMAIL and MYTNB_PASSWORD in the server environment.")
                    if self._client is None:
                        self._client = self._factory()
                    self._client.login(email, password)
                    self._authenticated = True
                assert self._client is not None
                return operation(self._client)
            except ToolError:
                raise
            except AuthenticationChallengeError:
                self._authenticated = False
                raise ToolError(
                    "AuthenticationChallengeError: myTNB requires CAPTCHA/OTP/2FA; "
                    "the server cannot complete this challenge."
                ) from None
            except AuthenticationError:
                self._authenticated = False
                raise ToolError(
                    "AuthenticationError: login rejected or session expired. "
                    "Check credentials before requesting another call."
                ) from None
            except MyTNBError as exc:
                raise ToolError(f"{type(exc).__name__}: myTNB request failed.") from None
            except Exception:
                # SDK/framework surprises must not print credentials, URLs or response bodies.
                raise ToolError("myTNB request failed unexpectedly.") from None

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                finally:
                    self._client = None
                    self._authenticated = False


def create_server(*, client_factory: Callable[[], MyTNBClient] = MyTNBClient) -> MCPServer:
    """Build a server without network access; authenticate on the first tool call."""
    session = _Session(client_factory)

    @asynccontextmanager
    async def lifespan(server: MCPServer):
        try:
            yield
        finally:
            await run_sync(session.close)

    server = MCPServer(
        "mytnb-mcp",
        instructions=(
            "Unofficial, read-only myTNB account and bill access. Results contain private "
            "account data. Never request credentials in tool arguments. Use account_id "
            "from list_accounts for get_bills. No payments, PDFs or challenge bypass."
        ),
        lifespan=lifespan,
    )
    annotations = ToolAnnotations(
        read_only_hint=True, destructive_hint=False, open_world_hint=True,
    )

    @server.tool(annotations=annotations)
    async def list_accounts() -> dict[str, Any]:
        """List owned accounts, including opaque account_id and MYR balances."""
        def operation(client: MyTNBClient) -> dict[str, Any]:
            return {"currency": "MYR", "accounts": [
                {
                    "account_id": account.account_id,
                    "account_no": account.account_no,
                    "description": account.description,
                    "total_payable": (
                        str(account.total_payable) if account.total_payable is not None else None
                    ),
                }
                for account in client.list_accounts()
            ]}

        return await run_sync(partial(session.execute, operation))

    @server.tool(annotations=annotations)
    async def get_bills(
        account_id: Annotated[str, Field(strict=True, min_length=1)],
        months: Annotated[int, Field(strict=True, ge=1, le=1200)] | None = 6,
        as_of: Annotated[str, Field(strict=True)] | None = None,
    ) -> dict[str, Any]:
        """Read bills for an owned account. months (1–1200, default 6) filters locally;
        null returns all available history. as_of is YYYY-MM-DD (default today).
        Amounts are exact decimal strings in MYR. Payments and billing tokens are omitted.
        """
        if not account_id.strip():
            raise ToolError("account_id must be non-blank; use list_accounts.")
        anchor = None
        if as_of is not None:
            try:
                anchor = date.fromisoformat(as_of)
                if anchor.isoformat() != as_of:
                    raise ValueError
            except ValueError:
                raise ToolError("as_of must be a valid YYYY-MM-DD date.") from None

        def operation(client: MyTNBClient) -> dict[str, Any]:
            account = next(
                (item for item in client.list_accounts() if item.account_id == account_id), None,
            )
            if account is None:
                raise ToolError("Account not found in owned accounts; call list_accounts.")
            return {"currency": "MYR", "bills": [
                {
                    "date": bill.date.isoformat(),
                    "amount": str(bill.amount),
                    "is_cancellation": bill.is_cancellation,
                    "is_reversal": bill.is_reversal,
                }
                for bill in client.get_bills(account, months=months, as_of=anchor)
            ]}

        return await run_sync(partial(session.execute, operation))

    return server


def main() -> None:
    """Run only the local stdio transport; stdout is reserved for MCP messages."""
    # httpx INFO request logs can contain opaque account identifiers in URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    create_server().run(transport="stdio")
