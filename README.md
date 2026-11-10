# duck2api-pro

增强版 Duck2API 网关：把 duck.ai 逆向为 OpenAI / Anthropic / Responses 兼容 API。

## 相比原版 duck2api_proxy.py 的改进

- **增强工具调用模拟**：`append_tool_retry` 把模型之前的非合规回复作为 Assistant 轮次追加，再发 User 修正提示（比纯 nudge 效果好）
- **匿名身份池轮换**：`IdentityPool` 管理 N 个匿名身份，限流后自动切换
- **多端点**：`/v1/chat/completions`、`/v1/responses`、`/v1/messages`、`/v1/images/generations`
- **多格式 agent 层**：OpenAI / Anthropic / Responses 三种格式统一转 duck.ai wire messages
- **图片支持**：data URI → duck.ai image parts
- **WebSearch / UploadFiles**：字段透传给 duck.ai 后端

## 文件结构

| 文件 | 说明 |
|---|---|
| `duck2api_pro_proxy.py` | 主网关：HTTP 服务 + 多端点 |
| `agent.py` | 协议层：格式转换 + 工具调用模拟 |
| `identity_pool.py` | 匿名身份池：多身份轮换 |

## 启动

```bash
python duck2api_pro_proxy.py --host 127.0.0.1 --port 7750 --pool-size 3
```

## 端点

| 端点 | 说明 |
|---|---|
| `POST /v1/chat/completions` | OpenAI 兼容（含流式） |
| `POST /v1/responses` | OpenAI Responses API（Codex CLI） |
| `POST /v1/messages` | Anthropic Messages API（Claude Code） |
| `POST /v1/images/generations` | 图片生成 |
| `GET /v1/models` | 模型列表 |
| `GET /v1/admin/pool` | 身份池状态 |
| `POST /v1/admin/rotate` | 手动轮换身份 |

## 依赖

- Python 3.10+
- `requests`
- `pure_handshake.py`（来自 duck2api_proxy 项目）
- Node.js（用于 `solve_challenge.js` 求解挑战）

## 验证

```bash
python duck2api_pro_proxy.py --selftest
```

## 协议说明

duck.ai 后端**只接受单条 user 消息**，不支持多角色 messages 或服务端会话记忆。
所有多轮上下文和工具调用都通过 prompt 工程模拟。