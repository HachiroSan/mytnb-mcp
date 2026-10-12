"""Offline MCP tests with invented account data and no credential/portal traffic."""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import httpx

from mytnb_sdk import Account, BillRecord, MyTNBClient
from mytnb_sdk.client import ACCOUNTS_PATH, PROFILE_REDIRECT_PATH, PROFILE_VIEW_PATH
from mytnb_sdk.errors import AuthenticationChallengeError, AuthenticationError, TransportError
from tests._helpers import bill_entry, model_payload, payment_entry, profile_html

MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None
if MCP_AVAILABLE or TYPE_CHECKING:
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters
    from mytnb_mcp.server import create_server


@unittest.skipUnless(MCP_AVAILABLE, "install .[mcp] to run MCP tests")
class MCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "MYTNB_EMAIL": "invented@example.invalid", "MYTNB_PASSWORD": "invented-secret",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop("MYTNB_ALLOWED_ACCOUNT_NUMBERS", None)
        self.sdk = Mock(spec=MyTNBClient)
        self.sdk.list_accounts.return_value = [Account("owned-id", "000123", "Home", Decimal("0"))]
        self.sdk.get_bills.return_value = [BillRecord(
            date(2024, 1, 15), Decimal("88.10"), "private-billing-token", False, None,
        )]
        self.factory = Mock(return_value=self.sdk)
        self.server = create_server(client_factory=self.factory)

    async def test_tools_and_serialization_and_cleanup(self):
        async with Client(self.server) as client:
            tools = (await client.list_tools()).tools
            self.assertEqual({tool.name for tool in tools}, {"list_accounts", "get_bills"})
            for tool in tools:
                assert tool.annotations is not None
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.destructive_hint)
                self.assertNotIn("password", json.dumps(tool.input_schema))
            self.factory.assert_not_called()
            result = await client.call_tool("list_accounts")
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["accounts"][0]["total_payable"], "0")
            result = await client.call_tool("get_bills", {
                "account_id": "owned-id", "months": None, "as_of": "2024-02-01",
            })
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["bills"], [{
                "date": "2024-01-15", "amount": "88.10",
                "is_cancellation": False, "is_reversal": None,
            }])
            self.assertNotIn("private-billing-token", result.model_dump_json())
            self.sdk.login.assert_called_once_with("invented@example.invalid", "invented-secret")
            self.sdk.get_bills.assert_called_once_with(
                self.sdk.list_accounts.return_value[0], months=None, as_of=date(2024, 2, 1),
            )
        self.sdk.close.assert_called_once()

    async def test_invalid_arguments_do_not_authenticate(self):
        async with Client(self.server) as client:
            for arguments in (
                {"account_id": " "}, {"account_id": 42},
                {"account_id": "owned-id", "months": 0},
                {"account_id": "owned-id", "months": True},
                {"account_id": "owned-id", "months": "6"},
                {"account_id": "owned-id", "months": 1201},
                {"account_id": "owned-id", "as_of": "20240101"},
                {"account_id": "owned-id", "as_of": "2024-02-30"},
            ):
                with self.subTest(arguments=arguments):
                    result = await client.call_tool("get_bills", arguments)
                    self.assertTrue(result.is_error)
        self.factory.assert_not_called()

    async def test_unknown_account_never_requests_profile(self):
        async with Client(self.server) as client:
            result = await client.call_tool("get_bills", {"account_id": "unowned-id"})
            self.assertTrue(result.is_error)
        self.sdk.get_bills.assert_not_called()

    async def test_unset_allowlist_preserves_all_owned_accounts(self):
        self.sdk.list_accounts.return_value += [Account("other-id", "000456"), Account("no-number")]
        async with Client(self.server) as client:
            result = await client.call_tool("list_accounts")
            self.assertFalse(result.is_error)
            self.assertEqual(
                [item["account_id"] for item in result.structured_content["accounts"]],
                ["owned-id", "other-id", "no-number"],
            )

    async def test_single_allowlisted_account_can_list_and_read_bills(self):
        self.sdk.list_accounts.return_value += [Account("hidden-id", "000456", "private-home")]
        with patch.dict(os.environ, {"MYTNB_ALLOWED_ACCOUNT_NUMBERS": "000123"}):
            server = create_server(client_factory=self.factory)
        async with Client(server) as client:
            result = await client.call_tool("list_accounts")
            self.assertFalse(result.is_error)
            self.assertEqual(len(result.structured_content["accounts"]), 1)
            self.assertEqual(result.structured_content["accounts"][0]["account_no"], "000123")
            self.assertNotIn("hidden-id", result.model_dump_json())
            self.assertNotIn("private-home", result.model_dump_json())
            result = await client.call_tool("get_bills", {"account_id": "owned-id"})
            self.assertFalse(result.is_error)
        self.sdk.get_bills.assert_called_once_with(
            self.sdk.list_accounts.return_value[0], months=6, as_of=None,
        )

    async def test_multiple_allowlisted_numbers_trim_spaces_and_preserve_zeros(self):
        self.sdk.list_accounts.return_value += [
            Account("other-id", "000456"), Account("different-number", "123"),
            Account("no-number"), Account("hidden-id", "000789"),
        ]
        with patch.dict(os.environ, {"MYTNB_ALLOWED_ACCOUNT_NUMBERS": " 000123, 000456 ,000123 "}):
            server = create_server(client_factory=self.factory)
        async with Client(server) as client:
            result = await client.call_tool("list_accounts")
            self.assertFalse(result.is_error)
            self.assertEqual(
                [item["account_id"] for item in result.structured_content["accounts"]],
                ["owned-id", "other-id"],
            )
            self.assertFalse((await client.call_tool("get_bills", {"account_id": "other-id"})).is_error)
        self.sdk.get_bills.assert_called_once_with(
            self.sdk.list_accounts.return_value[1], months=6, as_of=None,
        )

    async def test_allowlist_blocks_owned_missing_number_and_unknown_ids_without_disclosure(self):
        self.sdk.list_accounts.return_value += [Account("hidden-id", "000456"), Account("no-number")]
        with patch.dict(os.environ, {"MYTNB_ALLOWED_ACCOUNT_NUMBERS": "000123"}):
            server = create_server(client_factory=self.factory)
        async with Client(server) as client:
            errors = []
            for account_id in ("hidden-id", "no-number", "unowned-id"):
                result = await client.call_tool("get_bills", {"account_id": account_id})
                self.assertTrue(result.is_error)
                self.assertNotIn(account_id, result.model_dump_json())
                errors.append(result.content)
            self.assertEqual(errors[0], errors[1])
            self.assertEqual(errors[1], errors[2])
        self.sdk.get_bills.assert_not_called()

    async def test_unmatched_allowlist_returns_empty_and_never_fetches_bills(self):
        with patch.dict(os.environ, {"MYTNB_ALLOWED_ACCOUNT_NUMBERS": "999999"}):
            server = create_server(client_factory=self.factory)
        async with Client(server) as client:
            result = await client.call_tool("list_accounts")
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["accounts"], [])
            self.assertTrue((await client.call_tool("get_bills", {"account_id": "owned-id"})).is_error)
        self.sdk.get_bills.assert_not_called()

    async def test_allowlist_is_fixed_at_startup_not_changed_by_later_environment(self):
        self.sdk.list_accounts.return_value += [Account("hidden-id", "000456")]
        with patch.dict(os.environ, {"MYTNB_ALLOWED_ACCOUNT_NUMBERS": "000123"}):
            server = create_server(client_factory=self.factory)
        with patch.dict(os.environ, {"MYTNB_ALLOWED_ACCOUNT_NUMBERS": "000456"}):
            async with Client(server) as client:
                result = await client.call_tool("list_accounts")
                self.assertEqual(
                    [item["account_id"] for item in result.structured_content["accounts"]],
                    ["owned-id"],
                )
                self.assertTrue((await client.call_tool("get_bills", {"account_id": "hidden-id"})).is_error)
        self.sdk.get_bills.assert_not_called()

    async def test_invalid_allowlist_fails_before_client_creation(self):
        for value in ("", " ", ",", "000123,", ",000123", "000123,,000456", "*", "000123,bad",
                      "000 123", "-123", "123.0", "１２３", "١٢٣"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"MYTNB_ALLOWED_ACCOUNT_NUMBERS": value}):
                    with self.assertRaises(ValueError) as error:
                        create_server(client_factory=self.factory)
                self.assertIn("MYTNB_ALLOWED_ACCOUNT_NUMBERS", str(error.exception))
                self.assertNotIn("000123", str(error.exception))
        self.factory.assert_not_called()

    async def test_invalid_allowlist_subprocess_exits_safely_without_stdout(self):
        environment = dict(os.environ, MYTNB_ALLOWED_ACCOUNT_NUMBERS="invented-private-value")
        result = await asyncio.to_thread(
            subprocess.run, [sys.executable, "-m", "mytnb_mcp"], env=environment,
            input="", capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("MYTNB_ALLOWED_ACCOUNT_NUMBERS", result.stderr)
        self.assertNotIn("invented-private-value", result.stderr)
        self.assertNotIn("invented-secret", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    async def test_blocked_account_makes_no_profile_http_requests(self):
        requests = []

        def handler(request):
            requests.append(request.url.path)
            if request.url.path == ACCOUNTS_PATH:
                return httpx.Response(200, json={"records": [
                    {"AccountID": "owned-id", "AccountNo": "000123"},
                    {"AccountID": "hidden-id", "AccountNo": "000456"},
                ], "total": 2})
            raise AssertionError("A blocked account must never request its profile")

        sdk = MyTNBClient(transport=httpx.MockTransport(handler))
        with patch.dict(os.environ, {"MYTNB_ALLOWED_ACCOUNT_NUMBERS": "000123"}):
            with patch.object(sdk, "login"):
                async with Client(create_server(client_factory=lambda: sdk)) as client:
                    listed = await client.call_tool("list_accounts")
                    self.assertEqual(len(listed.structured_content["accounts"]), 1)
                    result = await client.call_tool("get_bills", {"account_id": "hidden-id"})
                    self.assertTrue(result.is_error)
        self.assertEqual(requests, [ACCOUNTS_PATH, ACCOUNTS_PATH])

    async def test_missing_credentials_are_safe_and_lazy(self):
        with patch.dict(os.environ, {}, clear=True):
            async with Client(self.server) as client:
                self.assertEqual(len((await client.list_tools()).tools), 2)
                result = await client.call_tool("list_accounts")
                self.assertTrue(result.is_error)
                self.assertIn("MYTNB_EMAIL", result.model_dump_json())
        self.factory.assert_not_called()

    async def test_errors_are_sanitized_and_never_retried_within_call(self):
        for error in (
            AuthenticationChallengeError("invented-secret"),
            AuthenticationError("invented-secret"), TransportError("invented-secret"),
            RuntimeError("invented-secret"),
        ):
            with self.subTest(error=type(error).__name__):
                sdk = Mock(spec=MyTNBClient)
                sdk.login.side_effect = error
                async with Client(create_server(client_factory=lambda: sdk)) as client:
                    result = await client.call_tool("list_accounts")
                    self.assertTrue(result.is_error)
                    self.assertNotIn("invented-secret", result.model_dump_json())
                    sdk.login.assert_called_once()
                sdk.close.assert_called_once()

    async def test_expired_session_reports_error_then_next_call_can_authenticate(self):
        self.sdk.list_accounts.side_effect = [AuthenticationError("expired"), []]
        async with Client(self.server) as client:
            self.assertTrue((await client.call_tool("list_accounts")).is_error)
            self.assertFalse((await client.call_tool("list_accounts")).is_error)
        self.assertEqual(self.sdk.login.call_count, 2)

    async def test_parallel_calls_are_serialized(self):
        import threading
        import time

        entered = threading.Event()
        overlap = []

        def read_accounts():
            if entered.is_set():
                overlap.append(True)
            entered.set()
            time.sleep(0.01)
            entered.clear()
            return []

        self.sdk.list_accounts.side_effect = read_accounts
        async with Client(self.server) as client:
            results = await asyncio.gather(*(client.call_tool("list_accounts") for _ in range(4)))
            self.assertTrue(all(not result.is_error for result in results))
        self.assertEqual(overlap, [])
        self.sdk.login.assert_called_once()

    async def test_mock_http_sdk_integration_excludes_payments(self):
        paths = []

        def handler(request):
            paths.append(request.url.path)
            if request.url.path == ACCOUNTS_PATH:
                return httpx.Response(200, json={"records": [{"AccountID": "owned-id"}], "total": 1})
            if request.url.path == PROFILE_REDIRECT_PATH:
                return httpx.Response(302, headers={
                    "Location": PROFILE_VIEW_PATH + "?selectedAccountID=owned-id",
                })
            if request.url.path == PROFILE_VIEW_PATH:
                return httpx.Response(200, text=profile_html(model_payload([
                    bill_entry(), payment_entry(),
                ])))
            raise AssertionError("Unexpected offline request")

        sdk = MyTNBClient(transport=httpx.MockTransport(handler))
        with patch.object(sdk, "login"):
            async with Client(create_server(client_factory=lambda: sdk)) as client:
                result = await client.call_tool("get_bills", {"account_id": "owned-id", "months": None})
                self.assertFalse(result.is_error)
                self.assertEqual(len(result.structured_content["bills"]), 1)
                self.assertEqual(result.structured_content["bills"][0]["amount"], "88.10")
        self.assertIn(PROFILE_REDIRECT_PATH, paths)

    async def test_stdio_subprocess_legacy_handshake_and_error(self):
        commands = [(sys.executable, ["-m", "mytnb_mcp"])]
        entry_point = Path(sys.executable).with_name(
            "mytnb-mcp.exe" if os.name == "nt" else "mytnb-mcp",
        )
        if entry_point.is_file():
            commands.append((str(entry_point), []))
        for command, arguments in commands:
            with self.subTest(command=command):
                parameters = StdioServerParameters(
                    command=command, args=arguments,
                    env={"MYTNB_EMAIL": "", "MYTNB_PASSWORD": ""},
                )
                async with Client(parameters, mode="legacy", read_timeout_seconds=10) as client:
                    self.assertEqual(len((await client.list_tools()).tools), 2)
                    result = await client.call_tool("list_accounts")
                    self.assertTrue(result.is_error)
                    self.assertIn("MYTNB_EMAIL", result.model_dump_json())
