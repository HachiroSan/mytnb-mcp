# myTNB SDK & MCP server

Read your myTNB electricity accounts and bill history from Python or an AI
assistant. The MCP server works with Claude, OpenCode, and other clients that
support local stdio servers.

Unofficial project; **not affiliated with or endorsed by TNB**.

## What it does

- Lists your owned accounts and current payable balances.
- Retrieves bill history, with an optional month filter. Amounts are in MYR.

Read-only: no payments, PDF downloads, or CAPTCHA/OTP bypass.

## Install

Requires **Python 3.11+**. From a checkout of this repository:

```sh
python -m pip install ".[mcp]"
```

For the Python SDK only, use `python -m pip install .`.
See the [SDK quick start](TECHNICAL.md#quick-start).

## Connect your AI client

The server needs `MYTNB_EMAIL` and `MYTNB_PASSWORD`. It doesn't load `.env`
automatically. Except for the Claude Desktop example below, set these variables
in the environment **before launching your client**.

Use the Python interpreter where you installed the package. If `python` points
elsewhere, replace it with its absolute path (for example,
`C:\\path\\to\\venv\\Scripts\\python.exe` in JSON on Windows, or
`/path/to/venv/bin/python` on macOS/Linux).

Merge the examples into your existing configuration; don't replace other servers.

<details>
<summary><strong>Claude Desktop</strong></summary>

Open **Settings → Developer → Edit Config** and add:

```json
{
  "mcpServers": {
    "mytnb-mcp": {
      "command": "python",
      "args": ["-m", "mytnb_mcp"],
      "env": {
        "MYTNB_EMAIL": "your-mytnb-email",
        "MYTNB_PASSWORD": "your-mytnb-password"
      }
    }
  }
}
```

Replace the placeholder values locally. This stores credentials in the config
file; keep it private and never commit or share it. If Claude already inherits
both variables from your OS environment, omit `env` instead.

Config locations:
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

Fully quit and reopen Claude Desktop, then check **Manage connectors** for
`mytnb-mcp`.
[Claude Desktop setup guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers)

</details>

<details>
<summary><strong>Claude Code</strong></summary>

With the credential variables set in your shell, register the server for your user:

```sh
claude mcp add --scope user --transport stdio mytnb-mcp -- python -m mytnb_mcp
```

Start Claude Code from that environment and run `/mcp` to check the connection.
Replace `python` in the command with an absolute interpreter path if needed.
[Claude Code MCP guide](https://code.claude.com/docs/en/mcp)

</details>

<details>
<summary><strong>OpenCode</strong></summary>

Add this to `~/.config/opencode/opencode.json` (user-wide) or your project's
`opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "mytnb-mcp": {
      "type": "local",
      "command": ["python", "-m", "mytnb_mcp"],
      "enabled": true,
      "environment": {
        "MYTNB_EMAIL": "{env:MYTNB_EMAIL}",
        "MYTNB_PASSWORD": "{env:MYTNB_PASSWORD}"
      }
    }
  }
}
```

Restart OpenCode after saving. The `{env:…}` references read your environment,
so you don't need to put credentials in the file.
[OpenCode MCP guide](https://opencode.ai/docs/mcp-servers/)

</details>

<details>
<summary><strong>VS Code / GitHub Copilot</strong></summary>

Add this to `.vscode/mcp.json`:

```json
{
  "servers": {
    "mytnb-mcp": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "mytnb_mcp"],
      "env": {
        "MYTNB_EMAIL": "${env:MYTNB_EMAIL}",
        "MYTNB_PASSWORD": "${env:MYTNB_PASSWORD}"
      }
    }
  }
}
```

Launch VS Code with the credential variables available. Use **MCP: List Servers**
in the Command Palette to start `mytnb-mcp`, approve the server when prompted,
and enable its tools in Copilot Chat.
[VS Code MCP guide](https://code.visualstudio.com/docs/agent-customization/mcp-servers)

</details>

## Limit access to selected accounts

Optionally set this in the **server's environment**, using the account numbers
printed on your TNB bills (not billing IDs):

```text
MYTNB_ALLOWED_ACCOUNT_NUMBERS=000123456789,000987654321
```

One number limits the assistant to one account; comma-separated numbers allow
several. For Claude Desktop or VS Code, add it under the server's `env`; for
OpenCode, use `environment`. Claude Code can inherit it from your shell.

Only these accounts appear in `list_accounts`, and `get_bills` rejects all
others. Restart the server after changing the list. Leave the variable **unset**
to allow all owned accounts; an empty or malformed value stops the server.
This restriction applies only to MCP tools, not the Python SDK or your TNB login.

## Try it

Ask your assistant:

> List my TNB accounts and their payable balances.

> Show my electricity bills for the last six months.

The two tools are `list_accounts` and `get_bills`. The server signs in when a
tool first needs access and reuses the session. If myTNB requires CAPTCHA or OTP,
the call stops with an error.

**Privacy:** account details and bill history go to your AI client and may reach
its model provider. Only connect clients you trust; don't paste credentials into
chat or commit them to the repository.

For SDK examples, tool arguments, authentication details, limitations, and tests,
read the [technical guide](TECHNICAL.md).
