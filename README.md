# OpenCode Go 本地转发代理

这个代理用于让只支持自定义 Base URL、但不能发送自定义请求头的客户端接入
OpenCode Go。它在请求链路中补上 `x-opencode-session`，并支持 OpenAI 与
Anthropic 两种接口路径和 SSE 流式响应。

> 本项目是非官方社区工具，与 WorkBuddy、OpenCode 没有隶属、合作或授权关系。
>
> 接入思路参考了[《在 WorkBuddy 中接入 OpenCode Go 订阅》](https://www.cnblogs.com/augustine0654/p/23673222)。

## 环境要求

- Windows
- Python 3.10 或更高版本
- 只需要 Python 标准库，无需安装第三方包

## 启动

双击 `start-proxy.bat`，或在当前目录运行：

```powershell
python opencode_go_proxy.py
```

默认监听 `127.0.0.1:8787`，默认 Session 为 `ses_workbuddy`。
请求耗时会自动追加到同目录的 `opencode-go-proxy.log`。

可选参数：

```powershell
# 更换端口
python opencode_go_proxy.py 8788

# 更换端口和 Session
python opencode_go_proxy.py 8788 ses_my_project

# 指定日志文件
python opencode_go_proxy.py --log-file D:\logs\opencode-go-proxy.log
```

也可以通过环境变量配置：

```powershell
$env:OPENCODE_GO_PROXY_PORT = "8788"
$env:OPENCODE_GO_SESSION_ID = "ses_my_project"
$env:OPENCODE_GO_PROXY_LOG_FILE = "D:\logs\opencode-go-proxy.log"
python opencode_go_proxy.py
```

## 客户端配置

OpenAI Chat Completions 格式：

```text
Base URL: http://127.0.0.1:8787/v1
API Key:  oc_sk_ 开头的 OpenCode Console 密钥
模型 ID:  保持原值
```

Anthropic Messages 格式：

```text
Base URL: http://127.0.0.1:8787
API Key:  oc_sk_ 开头的 OpenCode Console 密钥
模型 ID:  保持原值
```

代理不保存 API Key，只把客户端传入的 `Authorization` 原样转发给
`opencode.ai`。

## 路径映射

| 客户端请求 | 转发目标 |
| --- | --- |
| `/v1/chat/completions` | `/inference/go/openai/v1/chat/completions` |
| `/v1/responses` | `/inference/go/openai/v1/responses` |
| `/v1/messages` | `/inference/go/anthropic/v1/messages` |
| `/chat/completions` | 自动补 `/v1` 后转发 |
| `/v1/v1/messages` | 自动纠正后转发 |

## 常用检查

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8787/health
```

运行测试：

```powershell
python -m unittest -v
```

每次请求结束后，代理窗口和 `opencode-go-proxy.log` 都会输出一行耗时日志：

```text
POST /v1/chat/completions -> ... 200 (body-bytes=52, stream=True, accept=application/json, content-type=application/json, model=deepseek-v4.1-flash, messages=8, tools=12, body=0 ms, connect=255 ms, request-sent=257 ms, headers=680 ms, first-byte=1850 ms, total=68240 ms, chunks=214, bytes=23581, upstream-read=67900 ms, client-write=12 ms, response-type=text/event-stream)
```

- `body`：读完客户端请求体的时间。
- `connect`：DNS、TCP 和 TLS 建连完成的时间。
- `request-sent`：请求头和请求体完整发给上游的时间。
- `headers`：连接 OpenCode 并拿到响应头的时间。
- `first-byte`：从请求开始到收到上游第一块响应内容的时间。
- `total`：整个响应接收并转发完成的时间。
- `upstream-read`：代理等待 OpenCode 返回内容累计花的时间。
- `client-write`：代理把内容写给 WorkBuddy 累计花的时间。

如果 `first-byte` 很长，主要延迟在 OpenCode 排队或模型开始推理；如果
`first-byte` 很短但 `total` 很长且 `upstream-read` 占大部分，则是模型仍在
逐步生成；如果 `client-write` 占大部分，则是 WorkBuddy 接收或显示过慢；
如果日志里的 `total` 很短但 WorkBuddy 过了很久才显示，问题在客户端缓冲环节。

## 排错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `missing_session_id` | 客户端仍直连 OpenCode | Base URL 改为本机代理地址 |
| `Connection refused` | 代理未启动 | 双击 `start-proxy.bat` |
| 启动时提示端口被占用 | 已有代理实例或别的程序占用 | 关闭旧实例或换端口 |
| `401` | API Key 错误或没有 Go 订阅 | 在 OpenCode Console 重新生成密钥 |
| `403` 且使用免费模型 | 免费模型仅限 OpenCode 官方客户端 | 改用 Go 套餐内模型 |
| 请求时好时坏 | 有多个代理实例在抢端口 | 全部关闭，只保留一个 |

## 安全说明

- 代理只绑定 `127.0.0.1`，局域网和公网不能直接访问。
- 代理不解析模型请求体，也不修改其中的 JSON。
- 代理不持久化 API Key 或会话内容。
- `x-opencode-session` 不是凭据，只影响上游路由和缓存复用。

如果 OpenCode 调整网关地址、请求头或接口规则，需要同步更新
`opencode_go_proxy.py`。
