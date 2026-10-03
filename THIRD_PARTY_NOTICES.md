# 第三方项目声明 / Third-party notices

本项目是衍生作品，不是从零独立实现。下列声明说明代码来源、主要依赖和各自许可证，不意味着上游作者或平台对本项目的认可。
This is a derivative project. These notices identify source provenance and dependencies; they do not imply endorsement.

## 上游代码 / Upstream source

- **dreammis/social-auto-upload** — https://github.com/dreammis/social-auto-upload — MIT, Copyright (c) 2023 dreammis.
- 本仓库继承其 uploader、工具、CLI 和相关测试，并新增/修改 MCP 服务、SQLite 任务管理、会话验证、结果核验、删除回执、并发调度、评论助手和文档。
- Inherited uploader, utility, CLI, and test code is modified and extended with MCP, durable task management, session checks, verification, deletion receipts, scheduling, comment workflows, and documentation.
- 上游完整 MIT 文本保留在根目录 [LICENSE](LICENSE)。新贡献采用同一 MIT 许可证。
- 为避免公开本机运行数据及私人验收记录，首次发布采用经筛选的源码快照，不推送本地开发历史；这不改变上游版权和来源归属。The first public release is a reviewed source snapshot, not the private development history.

## 随源码包含的第三方代码 / Vendored code

| 项目 / Project | 使用位置 / Use | License / Source |
|---|---|---|
| puppeteer-extra / extract-stealth-evasions | 上游携带的 `utils/stealth.min.js`，保留原生成器标头 / inherited generated helper | MIT; https://github.com/berstend/puppeteer-extra ; [text](LICENSES/puppeteer-extra-MIT.txt) |

生成器标头标记生成时间为 2024-06-10。该文件不是本项目原创，也不代表使用它能避开平台检测。The generated helper is not original work of this project and makes no guarantee against platform detection.

## 主要 Python 依赖 / Main Python dependencies

依赖通过 pip 安装，不把其完整发行包重新打包入源码仓库。版本约束见 `pyproject.toml`；`LICENSES/` 收录了本次核验环境中主要依赖的许可证文本。传递依赖和二进制组件以实际安装发行包随附的 LICENSE/NOTICE 为准，不能被本项目 MIT 许可证覆盖。
Dependencies are installed with pip, not vendored as full distributions. Version constraints are in `pyproject.toml`. Included license copies document the reviewed environment; transitive and binary dependencies retain their own terms.

| Project | Purpose | License | Upstream |
|---|---|---|---|
| MCP Python SDK | FastMCP / Streamable HTTP | MIT | https://github.com/modelcontextprotocol/python-sdk |
| Patchright | Persistent browser automation | Apache-2.0 | https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python |
| Playwright | Inherited browser adapters / Patchright upstream | Apache-2.0 | https://github.com/microsoft/playwright-python |
| Loguru | Logging | MIT | https://github.com/Delgan/loguru |
| OpenCV Python | QR/image processing | Apache-2.0; bundled components have additional notices | https://github.com/opencv/opencv-python |
| qrcode | QR rendering | BSD | https://github.com/lincolnloop/python-qrcode |
| Segno | QR generation | BSD-3-Clause | https://github.com/heuer/segno |
| Requests | HTTP client | Apache-2.0 | https://github.com/psf/requests |
| Starlette | ASGI HTTP layer, via MCP | BSD-3-Clause | https://github.com/encode/starlette |
| Pydantic | Input models and validation, via MCP | MIT | https://github.com/pydantic/pydantic |
| HTTPX | MCP HTTP client | BSD-3-Clause | https://github.com/encode/httpx |
| AnyIO | Async concurrency, via MCP | MIT | https://github.com/agronholm/anyio |
| pytest | Development tests only | MIT | https://github.com/pytest-dev/pytest |

The inherited Bilibili CLI can download **biliup** (MIT, https://github.com/biliup/biliup). Its binary is not included in this release. Legacy optional adapters may require additional packages such as `xhs`; these are not required by the three-platform MCP path and are not bundled. Consult each upstream distribution before enabling those adapters.

## 非开源依赖与品牌 / External products and brands

Google Chrome is installed separately and is governed by its own terms. GLM/Qwen are optional remote model services, not model weights distributed here. Douyin, WeChat Channels, and Xiaohongshu are external services and trademarks; their content, accounts, APIs, and platform rules are not licensed by this repository's MIT license.

Chrome 需单独安装并遵循自身条款；GLM/Qwen 是可选远程服务，本项目没有分发模型权重。抖音、视频号、小红书的平台内容、账号和规则不属于本项目 MIT 授权范围。
