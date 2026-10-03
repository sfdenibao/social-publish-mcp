# Social Publish MCP

[English](README.en.md) | 简体中文

本地运行的社交平台发布 MCP，支持 **抖音、微信视频号、小红书**。Agent 负责理解需求，服务负责账号会话、任务调度、防重复提交与平台页面核验。

这是基于 [dreammis/social-auto-upload](https://github.com/dreammis/social-auto-upload) 的衍生项目，**不是平台官方 SDK**。保留上游 MIT 许可证及版权声明；新增部分同样采用 MIT。来源和依赖见 [第三方声明](THIRD_PARTY_NOTICES.md)。

## 功能

| 能力 | 范围 |
|---|---|
| 视频发布 | 三平台独立预检、独立并行推进 |
| 账号管理 | 独立持久化 Chrome 资料目录、账号/profile 互斥、扫码登录恢复 |
| 发布核验 | 管理页 DOM 作品与状态匹配；按钮点击或 HTTP 200 不代表成功 |
| 防重复 | SQLite 提交前标记、幂等键、同账号同素材 SHA-256 去重 |
| 任务管理 | 冻结计划、状态查询、删除未提交任务、持久化删除回执 |
| 标题处理 | 清理特殊字符；小红书计划阶段确定 20 字标题，与上传核验一致 |
| 可选评论助手 | 抖音本人视频一级评论的读取、草稿、显式发送与可暂停自动回复 |
| Windows 自启 | 当前用户登录后隐藏控制台运行 |

**纯发布不需要 LLM；可选评论助手会调用用户配置的模型。** 当前支持 GLM/Qwen 接口，公开评论与作品上下文会发送到所配置的提供商。默认不启用自动回复，不提供私信自动发送。

仓库保留上游 CLI 和其他平台适配代码，但 MCP 发布范围仅为上述三平台；保留代码不等于对其他平台当前可用性的承诺。

## 快速开始（Windows / PowerShell）

需要 Python **3.11–3.12**、Git、Google Chrome。默认浏览器路径为 `C:/Program Files/Google/Chrome/Application/chrome.exe`；其他位置通过 `SAU_CHROME_PATH` 指定。

```powershell
git clone https://github.com/sfdenibao/social-publish-mcp.git
cd social-publish-mcp
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[mcp,test]"
Copy-Item conf.example.py conf.py
New-Item -ItemType Directory runtime -Force
Copy-Item config/accounts.example.json runtime/accounts.json
```

编辑 `runtime/accounts.json`：填写准确昵称、独立 `profile` 与 `session_file` 路径，删除不使用的平台账号。相对路径以项目工作目录为基准。不要复制他人的 Cookie。`public_identity` 仅用于可选评论功能，填写自己的公开抖音号和 `/user/...` 路径；缺少时评论操作会拒绝执行。

```powershell
.\.venv\Scripts\python.exe publish_mcp.py
```

客户端连接 **`http://127.0.0.1:5410/mcp`**，选择 **Streamable HTTP**，不是旧版 `/sse` 或 stdio 命令。

```powershell
.\.venv\Scripts\python.exe mcp_acceptance_client.py get_service_status
.\.venv\Scripts\python.exe mcp_acceptance_client.py list_accounts
```

调用 `start_account_login` 打开服务管理的浏览器并扫码。登录完成还需通过身份和视频上传入口验证，配置账号不等于已登录。

## Agent 调用顺序

1. `get_service_status`、`list_accounts`、`list_publish_channels` 发现服务与账号。
2. `prepare_publish` 传入服务本机绝对视频路径、标题、正文、话题、声明和目标，冻结计划。
3. 展示返回的各平台 `titles`，调用 `preflight_publish`。
4. 用户明确授权内容与目标后调用 `submit_publish`，查询 `get_publish_batch`。
5. `UNKNOWN_RESULT` 只查询或核验原任务，不换 key、新建计划或重新上传绕过保护。
6. 用户要求删除时调用 `delete_publish_task`；已尝试发布或已发布的记录受保护，不删除远程作品或素材文件。

协议 `scheduling_mode=parallel`、`scheduling_version=2`：单个平台登录失败不阻止其他就绪目标。`ready/any_ready` 表示至少一项就绪，`all_ready` 表示全部就绪；列表顺序不是执行顺序。

参数、错误和状态见 [MCP 接入文档](docs/MCP_INTEGRATION_GUIDE.md)、[Schema](docs/MCP_TOOL_SCHEMAS.json)。

## 可选评论助手

复制 `config/comment-reply-model.example.json` 到 `runtime/comment-reply-model.local.json` 并填写自己的密钥。仅发布视频时不需要模型配置。配置模型不代表授权公开回复。

只处理已验证本人视频的一级评论；已回复、定位不唯一、身份不符或发送结果不明时保守停止。模型只返回 `reply/skip/handoff`，不能调用浏览器工具。每个任务使用对应作品的标题和描述，不套用其他作品的业务背景。见 [评论文档](docs/DOUYIN_AUTOREPLY.md)。

## Windows 登录自启

```powershell
.\install_autostart.ps1
```

注册当前用户的 `SocialAutoUpload-MCP` 登录任务，使用交互式会话，不在无人登录的 Session 0 中发布。异常退出最多重启 3 次，间隔 1 分钟。自启不创建任务或开启评论授权，会继续处理原有已授权队列。卸载自启：

```powershell
Unregister-ScheduledTask -TaskName SocialAutoUpload-MCP -Confirm:$false
```

删除计划任务不会终止已经运行的服务。日志保存在 `runtime/`。

## 开发与测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe scripts/export_mcp_schema.py
```

测试使用隔离 SQLite、受控浏览器页面和模拟上传/模型结果，部分 DOM 测试需要 Chrome。测试通过不等于真实发布验收。Windows 是主要验证环境，其他系统未作完整验收。

## 数据、使用边界与许可证

- 仅绑定回环地址，当前没有 API Key；只供可信本机客户端使用，不要直接暴露公网。
- 会话、浏览器资料、Cookie、密钥、本机配置、截图和视频不随源码发布。
- 浏览器自动化受平台规则、审核、验证码和风控约束。项目不保证免风控、免封禁或发布必然成功。
- 仅操作自己或获授权的账号、内容与评论；不要用于垃圾发布、骚扰或未经授权的代发。
- 结果不明时保留证据并人工处理，不靠反复重试消除不确定性。
- [MIT License](LICENSE)，保留 `Copyright (c) 2023 dreammis`。与上游和各平台没有官方合作或担保关系。

欢迎提交脱敏的复现步骤和测试；不要在 Issue、日志或附件中提交账号会话、密钥或私人素材。
