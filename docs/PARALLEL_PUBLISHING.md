# 多平台独立发布调度（2026-09-14）

当前用户已授权各目标独立推进，覆盖旧文档的全账号屏障和平台顺序规则。显示顺序仍为抖音、视频号、小红书；此顺序不再是上传、点击发布或结果核验的依赖。

## 客户端协议

`get_service_status`、`preflight_publish`、`get_publish_batch` 和 `submit_publish` 均返回 `scheduling_mode="parallel"`、`scheduling_version=2`。客户端只在明确识别该协议时采用独立目标语义，旧服务仍按原全账号规则处理。

`preflight_publish.ready` / `any_ready` 表示至少一个目标的账号与发布页验证完成；`all_ready` 表示全部完成。`checks` 始终保留每个目标的结果。它是只读快照，不是发布成功凭据。提交冻结批次代表授权该批次所有目标，尚未登录的目标在完成自身预检后再推进。

批次保留既有 `state`，新增 `partial_completed` 与 `progress`（total、published、failed、waiting_result、waiting_login、active、pending）。单个平台等待扫码、准备失败或结果未知，不阻止其他可执行目标。只有全部目标有已发布记录且引用结果核验完成，批次才是 `PUBLISHED`。

## 调度与防重

- 默认最多 3 条平台流水线；后台环境变量 `SAU_PUBLISH_CONCURRENCY` 可设为 1–8。上传、预检及结果复查共享此上限，运行中空槽会持续补入待执行任务。
- 同账号或同一规范化 profile 路径互斥。SessionManager 仍保留跨进程 profile 文件锁；不同配置账号指向相同 profile 时也不能并发打开。
- 调度只在原子领取时短暂持有 mutation_lock，随后持久化的 `PREPARING` 阻止删除和整批恢复干扰在途动作。浏览器等待不长期占用全局锁。
- 每个目标先校验声明、媒体摘要、账号与发布输入，再由上传适配器在同一锁定 profile 中复查。`before_click` 验证该目标绑定的 READY 预检和计划指纹，且原子要求 `attempted_at IS NULL`。
- `UNKNOWN_RESULT` / `SUBMITTED` / `PENDING_REVIEW` 只读复查；不会因重启、恢复批次或其他平台失败自动再点击。`PUBLISHED` 不重发。现有显式、独立授权的一次性未知结果恢复仍保留原尝试日志，只重置被授权目标。
- 旧批次里的未开始目标也可推进，不能将旧 UNKNOWN 视为失败后重传。已失败且未提交的目标需显式恢复批次才能重试；PROFILE_BUSY / 有限 CHECK_FAILED 按目标退避。

## 验证边界

调度测试使用真实 asyncio 并发、临时 SQLite、隔离 HTTP MCP 和受控上传/DOM回执夹具；验证慢预检、慢上传、慢结果复查、同账号/profile 互斥、填槽、并发上限、防重、重启与部分失败。它们不是一次真实平台发布验收；真实成功仍须平台管理页的可匹配 DOM 证据。
