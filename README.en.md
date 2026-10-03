# Social Publish MCP

English | [简体中文](README.md)

A local MCP server for **Douyin, WeChat Channels, and Xiaohongshu** video publishing. Agents interpret requests; the server manages persistent account sessions, durable jobs, concurrent scheduling, duplicate-submission guards, and management-page verification.

A derivative of [dreammis/social-auto-upload](https://github.com/dreammis/social-auto-upload), **not an official platform SDK**. The upstream MIT license and copyright are retained; new contributions are also MIT licensed. See [third-party notices](THIRD_PARTY_NOTICES.md).

## Features

- Independent per-platform preflight and publishing, with account/profile exclusion.
- Persistent Chrome sessions, QR login recovery, identity and upload-page checks.
- SQLite idempotency, account/media SHA-256 deduplication, and a durable marker written before Submit.
- Management-page result verification: a click, redirect, or HTTP 200 is not success evidence.
- Frozen plans, status queries, deletion of unsubmitted work, and persistent deletion receipts.
- Title cleanup and a frozen 20-character Xiaohongshu title shared by upload and verification.
- Optional Douyin comments: owned videos, top-level comments only, drafts, explicit sends, and pausable auto-replies.
- Windows login startup with a console-free supervisor.

**Publishing requires no LLM. Optional comment replies do.** GLM/Qwen endpoints are supported; public comments and work context are sent to the configured provider. Auto-reply permission is disabled by default. There is no automatic private-message sender.

Other upstream CLI adapters remain in the tree; this is not a promise that all those platforms are currently supported or verified through MCP.

## Quick start (Windows / PowerShell)

Requirements: Python **3.11–3.12**, Git, Google Chrome. Default Chrome path: `C:/Program Files/Google/Chrome/Application/chrome.exe`. Set `SAU_CHROME_PATH` for another installation.

```powershell
git clone https://github.com/sfdenibao/social-publish-mcp.git
cd social-publish-mcp
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[mcp,test]"
Copy-Item conf.example.py conf.py
New-Item -ItemType Directory runtime -Force
Copy-Item config/accounts.example.json runtime/accounts.json
```

Edit `runtime/accounts.json` with exact display names and separate profile/session paths; remove unused accounts. Relative paths resolve from the project working directory. Never reuse another person's cookies. The optional `public_identity` pins your own public Douyin handle and `/user/...` path for comment tools; missing identity configuration fails closed.

```powershell
.\.venv\Scripts\python.exe publish_mcp.py
```

Connect to **`http://127.0.0.1:5410/mcp`** using **Streamable HTTP**, not legacy SSE or a stdio command.

```powershell
.\.venv\Scripts\python.exe mcp_acceptance_client.py get_service_status
.\.venv\Scripts\python.exe mcp_acceptance_client.py list_accounts
```

Call `start_account_login` and scan the QR code in the service-owned browser. Completion includes identity and upload-page checks; adding an account configuration is not a successful login.

## Agent workflow

1. Discover health, accounts, and capabilities.
2. `prepare_publish`: freeze a local absolute media path, title, description, tags, declaration, and targets.
3. Present the returned per-platform `titles`, then run `preflight_publish`.
4. After the user authorizes content and destinations, call `submit_publish` and poll `get_publish_batch`.
5. Reconcile `UNKNOWN_RESULT` on the original task; never change keys or reupload to bypass uncertainty.
6. On deletion intent, call `delete_publish_task`. Attempted/published work remains protected; remote posts and source media are never deleted.

Scheduling version 2 is parallel and independent: one unavailable target does not block ready targets. `ready/any_ready` means at least one ready target; `all_ready` means every target. Display order is not execution order.

See the [integration guide](docs/MCP_INTEGRATION_GUIDE.md) and [machine-readable schemas](docs/MCP_TOOL_SCHEMAS.json).

## Optional comment assistant

Copy `config/comment-reply-model.example.json` to `runtime/comment-reply-model.local.json` and supply your own settings. This is unnecessary for publishing. Model configuration alone does not authorize public replies.

Only verified top-level comments on owned videos are eligible. Ambiguous targets, identity mismatches, and uncertain sends stop conservatively. The model returns `reply/skip/handoff` decisions, not browser actions. Each job uses its own video's title and description. See [comment documentation](docs/DOUYIN_AUTOREPLY.md).

## Windows startup

```powershell
.\install_autostart.ps1
```

Registers `SocialAutoUpload-MCP` for the current user's interactive logon, not unattended Session 0 publishing. Failed processes are retried three times, one minute apart. Startup resumes previously authorized work; it does not create jobs or enable replies. Remove startup with:

```powershell
Unregister-ScheduledTask -TaskName SocialAutoUpload-MCP -Confirm:$false
```

Removing the task does not stop an already running service. Logs are under `runtime/`.

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe scripts/export_mcp_schema.py
```

Tests use isolated databases, controlled browser pages, and mocked uploads/model responses. Some DOM tests need installed Chrome. Passing tests is not live publishing acceptance. Windows is the primary validated environment; other operating systems have not received full acceptance testing.

## Data, limitations, and license

- Localhost only, without API-key authentication. Use trusted local clients; do not expose publicly.
- Sessions, cookies, browser profiles, model keys, local configuration, screenshots, and media are excluded from source releases.
- Platform rules, moderation, verification challenges, and anti-abuse controls still apply. No publication or account-safety guarantee is made.
- Use authorized accounts/content only; no spam, harassment, or unauthorized posting.
- Preserve evidence and request human review when results are uncertain.
- [MIT](LICENSE), retaining `Copyright (c) 2023 dreammis`. No endorsement by the upstream authors or social platforms is implied.

Contributions should include sanitized reproduction steps and tests. Never attach credentials, authenticated profiles, or private media to issues.
