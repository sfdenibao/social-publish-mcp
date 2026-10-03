# Douyin comment assistant / 抖音评论助手

An optional extension to video publishing. It reads and replies only to top-level comments on positively verified videos owned by the configured account. No private messages, other authors' videos, image posts, or nested reply conversations.

这是可选功能。只接纳已确认本人视频下的一级评论，不支持私信、他人视频、图文或楼中楼回复。

## Setup / 配置

1. Configure the account's exact `display_name`, isolated profile, and `public_identity` in `runtime/accounts.json`. The latter requires your public handle and `/user/...` profile path; it is checked against the visible account.
2. Copy `config/comment-reply-model.example.json` to `runtime/comment-reply-model.local.json`. Use your own provider/model/key; never commit it. Provider choices are `zai` (GLM) or `dashscope` (Qwen); custom HTTPS endpoints require explicit `allow_custom_endpoint=true`.
3. Review `config/comment_reply_prompt.txt` and the actual work context. No job inherits another video's campaign or description.
4. Configure jobs, run read-only inspection/preview, and enable sending only with explicit authorization. Model setup alone never enables sending.

配置账号身份和模型后，先只读检查及预览；需要发送时另行授权。公开评论与作品上下文会传给配置的模型服务。模型只生成结构化决策，没有浏览器工具权限。

## Modes / 模式

- `configure_douyin_notifications` + `set_douyin_notifications_enabled`: account-level owned-video notification watcher; enabling disables overlapping single-work jobs.
- `configure_douyin_autoreply` + `set_douyin_autoreply_enabled`: one published video, initial complete baseline followed by new comments.
- `prepare_douyin_owned_video_replies` + `submit_douyin_owned_video_replies`: freeze a one-video run and explicitly send its prepared replies.
- `inspect_*`, `read_*`, and `get_*`: inspect account/work/comment/status without submitting.
- `preview_*`: may call the configured model, never submits.

Avoid enabling overlapping reply workflows for the same account. Profile locking prevents simultaneous browser use but is not a global cross-workflow reply deduplication contract. Pause standing jobs before one-off runs. 配置工具更新已有任务不会自动暂停，修改前应先关闭其发送授权。

Unknown submission results retain their durable guards and are only rechecked. An absent/ambiguous comment does not authorize posting elsewhere. Scope, recipient identity, own-account identity, and enabled state are checked before a send. Default account-level notification jobs have no reply-count quota; choose polling and permissions deliberately.

## Testing boundary / 验证范围

Offline tests cover model-response validation, identity/scope checks, SQLite guards, pauses, and controlled DOM fixtures. They do not establish model response quality for every topic or continuous compatibility with live platform UI. Real sends require independent authorization and observable evidence.

See [tool schemas](MCP_TOOL_SCHEMAS.json) for the authoritative argument definitions.
