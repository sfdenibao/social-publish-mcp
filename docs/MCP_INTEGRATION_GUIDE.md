# MCP integration / MCP 接入文档

本文件描述当前发布服务；配套 [README](../README.md)、[English README](../README.en.md) 和自动生成的 [MCP_TOOL_SCHEMAS.json](MCP_TOOL_SCHEMAS.json)。当前共 43 个工具，以运行中的 `tools/list` 为准。

## 连接与配置

- 地址：`http://127.0.0.1:5410/mcp`；Streamable HTTP；只绑定回环地址，当前无需 API Key。
- 协议：`initialize` → `notifications/initialized` → `tools/list` → `tools/call`，建议使用 MCP SDK。
- `SAU_MCP_PORT` 设置端口；`SAU_RUNTIME_DIR` 设置运行数据目录；`SAU_CHROME_PATH` 设置 Chrome 可执行路径；`SAU_PUBLISH_CONCURRENCY` 为 1–8，默认 3。
- 从 `config/accounts.example.json` 建立自己的 `runtime/accounts.json`；资料目录与账号身份不能混用。
- 发布不依赖模型。可选评论助手需要独立配置模型，详见评论文档。

## 发布协议 v2

各目标独立验证、独立推进，`scheduling_mode="parallel"`、`scheduling_version=2`。列表按抖音、视频号、小红书排列，仅用于展示，不保证执行次序。

1. 查询 `get_service_status`、`list_accounts`、`list_publish_channels`。
2. `prepare_publish` 保存冻结计划；不会打开上传动作。
3. 展示实际各平台标题 `titles`，执行 `preflight_publish`。`ready` 与 `any_ready` 表示至少一项就绪；`all_ready` 表示全部就绪。预检只读且只是快照。
4. 用户明确授权后，`submit_publish(plan_id,idempotency_key)` 启动真实发布。每个目标会在自己的浏览器会话中再次检查身份与发布入口。
5. 每 3–5 秒查询 `get_publish_batch`；遇到等待登录时展示服务返回的登录会话。
6. `resume_publish_batch` 只恢复原批次；已尝试提交的任务仅核验，不自动再次点击。

## prepare_publish 参数

| 字段 | 要求 |
|---|---|
| media_path | 服务机器上的本地绝对路径，非空 mp4/mov/mkv/webm；工具不负责跨机器传文件 |
| title | 清理标点、emoji、控制字符并合并空白；清理后须有文字或数字且不超过 30 字。小红书目标在冻结时截为前 20 字 |
| description | 正文；无正文传空字符串 |
| tags | 字符串数组；无话题传空数组 |
| declaration | 必须显式传入支持的声明；不声明传空字符串。不能把“原创”当作有效自主声明，不能自动替换 |
| channels | 可选平台数组；省略表示三个平台，未配置账号会报错；可以传显式子集 |
| accounts | 可选的平台到账号 ID 映射；候选账号不唯一时必须指定 |

返回 `plan_id`、`plan`、`order`、`preflight_required`、`title`、`title_changed`、`titles`。`titles` 是每个平台实际使用的标题；多平台顶层 `title` 是清理后的公共原文。调用方必须展示各平台标题，不能拿原始输入去核验。历史已提交任务不改写；小红书旧标题按实际上传的前 20 字匹配。

```json
{
  "media_path": "D:/media/example.mp4",
  "title": "示例标题",
  "description": "示例正文",
  "tags": [],
  "declaration": "",
  "channels": ["douyin"],
  "accounts": {"douyin": "douyin-main"}
}
```

## 状态与防重复

- 只有匹配平台管理页作品和状态才能记录 `PUBLISHED`。HTTP 200、按钮点击、页面跳转、视频上传完成均不是发布成功凭据。
- `UNKNOWN_RESULT`、`PENDING_REVIEW`、`SUBMITTED` 不能通过新 key 或新计划绕过。查询原 `task_id`，必要时 `recheck_publish_result`。
- 同账号同素材摘要会复用/阻止已有任务；改标题不会消除去重。
- `partial_completed` 与 `progress` 描述部分完成情况；某目标失败或未知不阻止其他目标。
- `recover_uncertain_publish` 是特殊的一次性恢复：必须单独确认可能重复发布的风险、匹配原 attempted_at、提供 request_id 与 reason，并通过新鲜管理页观察。它不证明上次没发出；原尝试永久留档，不应用作常规重试。

## 登录

`start_account_login(account_id,visible=true)` 打开服务管理的官方浏览器；`get_login_status(session_id)` 查询。成功登录还需要通过身份和上传入口检查。不会因为打开了首页就认为就绪。工具不返回 Cookie 或模型密钥。

## 删除任务与回执

用户明确要求删除时调用 `delete_publish_task(target_id)`，接受 MCP 的批次 ID、独立任务 ID 或计划 ID；产品系统自己的记录 ID 不能直接代入。批次子任务必须使用批次 ID。

- `DELETED`：原子删除尚未提交的任务及关联计划/键，保存删除快照与持久化回执。
- 重复删除同一 ID 返回原回执；查询已删除任务/批次也可取回回执，便于超时后的状态同步。
- `NOT_FOUND`：没有记录，不证明平台未发布，不允许据此解除未知结果保护。
- 正在执行、曾尝试提交、留有提交证据、已发布或跨批次引用不安全时拒绝删除。
- 调用方收到确认回执后再作废自己的本地记录及旧确认卡；不删除素材或平台作品。

## 评论工具

只读 inspect/read/status 不发送；preview/prepare 可调用模型但不发送。submit、启用 watcher、send=true 必须有用户明确授权。默认关闭，使用独立账号/profile 锁；只允许本人视频的一级评论。私信、楼中楼及他人作品不在支持范围。

完整工具参数由 Schema 提供，禁止猜测工具名或参数。功能概览见下方自动生成目录。

## 工具目录（43）

| Tool | Description |
|---|---|
| `inspect_douyin_own_work_order` | Read the authenticated account's own works in visible display order. Never replies. |
| `read_douyin_owned_video_comments` | Read only top-level comments on one positively verified owned video. Never sends or enables a watcher. |
| `inspect_douyin_notifications` | Read the owned account's visible notification entry. No replies or private-message sends. |
| `read_douyin_notifications` | Read the account's current interaction notifications only; not private messages. Never replies. |
| `inspect_douyin_notification_target` | Inspect the bound reply composer, optionally fill a draft. Never submits; draft is discarded on close. |
| `prepare_douyin_owned_video_replies` | Freeze and draft replies to top-level comments on ONE explicitly requested owned video. Never posts or enables a scheduler. |
| `submit_douyin_owned_video_replies` | Submit only the frozen run explicitly authorized by the user. Keeps all standing watchers paused; uncertain sends are never repeated. |
| `get_douyin_owned_video_reply_run` | Read this one-video run's frozen targets, drafts and at-most-once submission evidence. |
| `configure_douyin_notifications` | Configure only top-level comments on the account's own videos. No nested replies or private messages; disabled by default. |
| `set_douyin_notifications_enabled` | Enable or pause user-authorized notification replies. Disables overlapping single-work watchers for this account. |
| `run_douyin_notifications_once` | Scan notifications; send only with an enabled standing permission. Unknown submits are never resent. |
| `preview_douyin_notification_reply` | Read actual original conversation and generate a draft only. Never submits or changes send state. |
| `get_douyin_notifications_status` | Read notification watcher status and recent public interactions, never private messages or credentials. |
| `configure_douyin_autoreply` | Create/update a disabled-by-default auto-reply job for one owned published video. |
| `preview_douyin_auto_reply` | Call the configured Qwen/GLM for one sample decision. Never posts to Douyin. |
| `run_douyin_autoreply_once` | Run a bounded scan. send=True can reply only if the job is explicitly enabled. |
| `set_douyin_autoreply_enabled` | Enable/pause standing auto-reply permission for the fixed video. |
| `get_douyin_autoreply_status` | Read safe model metadata, enabled jobs, counters, handoff items and recent decisions. |
| `read_douyin_comments` | Read bounded top-level comments and emoji alt text; no model or posting. Reports completeness. |
| `inspect_douyin_reply_entry` | Read one existing comment's reply composer. Does not fill or send any content. |
| `inspect_douyin_published_video_analytics` | Read overview, traffic and audience analytics for one verified owned published work. No edits or posts. |
| `inspect_douyin_comments` | Read visible DOM for one previously PUBLISHED Douyin work in its existing account. |
| `prepare_douyin_comment` | Freeze one top-level comment for a previously published own Douyin video. No posting. |
| `get_douyin_comment_status` | Read durable single-comment state. UNKNOWN_RESULT is not permission to retry. |
| `submit_douyin_comment` | Send the exact frozen top-level comment ONCE on the user's previously published work. |
| `recheck_douyin_comment` | Read the same work's comments again. Never sends or retries a comment. |
| `list_accounts` | List local account aliases without credentials. UNKNOWN is not logged-in proof. |
| `list_publish_channels` | Report implemented adapters separately from real publication acceptance. |
| `prepare_publish` | Freeze one ordered batch; default includes ALL three platforms, even logged-out ones. |
| `delete_publish_task` | Delete an unsubmitted batch, standalone task, or frozen plan by its MCP ID. |
| `preflight_publish` | Read-only checks of every account and video upload page; never uploads or submits. |
| `get_publish_batch` | Independent target status, per-account checks, login sessions, and partial completion. |
| `resume_publish_batch` | Resume unsubmitted targets independently; attempted targets only reconcile, never repeat a click. |
| `recover_uncertain_publish` | Explicit one-shot retry of an old UNKNOWN_RESULT after fresh reconciliation. |
| `submit_publish` | Authorize the frozen batch for REAL publication, independently after each target passes preflight. |
| `get_publish_status` | Return durable state, or the original DELETED receipt for an exact removed task/plan ID. |
| `get_publish_evidence` | Return only matching work evidence, never cookies or page dumps. |
| `retry_preparation` | Retry only a failure proven to have happened BEFORE any publish click. |
| `recheck_publish_result` | Read-only DOM verification. force also inspects completed work without changing its saved result. |
| `check_account_status` | Check identity using the same locked profile used for publication. |
| `start_account_login` | Open the service-owned official browser; return a fixed local session page. |
| `get_login_status` | Return login state and identity result, never QR bytes or credentials. |
| `get_service_status` | Check process-owned worker health. |
