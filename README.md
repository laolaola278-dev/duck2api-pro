# duck2api-pro

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![Transport](https://img.shields.io/badge/Transport-pure%20%7C%20hybrid%20%7C%20browser-orange)

> 把 duck.ai 逆向为 OpenAI / Anthropic / Responses 兼容 API。纯 HTTP 握手，无需浏览器，毫秒级求解挑战。

---

## ✨ 特性

| | 原版 duck2api_proxy | 本仓库 |
|---|---|---|
| 工具调用重试 | 纯 nudge 前缀提示 | append_tool_retry：把模型非合规回复作为 Assistant 轮次追加，自我修正 |
| 身份管理 | 单一会话，限流后手动重启 | IdentityPool：N 个匿名身份自动轮换 + 熔断冷却 |
| 端点 | /v1/chat/completions | chat / responses / messages / images / models |
| 多格式 | 仅 OpenAI | OpenAI + Anthropic + Responses |
| 多模态 | 纯文本 | text + image (data URI) |
| WebSearch | ❌ | ✅ 字段透传 |
| 管理 API | ❌ | /v1/admin/pool + /v1/admin/rotate |

## 🚀 快速开始

### 依赖

```bash
pip install requests
```

还需要 pure_handshake.py 和 solve_challenge.js（来自上游 duck2api 项目）。Node.js 用于本地求解挑战。

### 启动

```bash
python duck2api_pro_proxy.py --host 127.0.0.1 --port 7750 --pool-size 3
```

### 自检

```bash
python duck2api_pro_proxy.py --selftest
```

### 调用

```bash
curl -X POST http://127.0.0.1:7750/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-6-luna","messages":[{"role":"user","content":"Hello!"}]}'
```

---

## 📋 端点一览

| 方法 | 路径 | 说明 | 兼容 |
|---|---|---|---|
| POST | /v1/chat/completions | 对话补全（含流式） | OpenAI |
| POST | /v1/responses | Responses API | Codex CLI |
| POST | /v1/messages | Messages API | Claude Code |
| POST | /v1/images/generations | 图片生成 | OpenAI |
| GET | /v1/models | 模型列表 | OpenAI |
| GET | /v1/admin/pool | 身份池状态 | — |
| POST | /v1/admin/rotate | 手动轮换身份 | — |

---

## 🏗️ 架构

```
  ┌────────────────────────────────────────────┐
  │            duck2api_pro_proxy.py           │
  │  ┌────────────┐  ┌──────────────────────┐  │
  │  │ HTTP Server│  │  IdentityPool (N=3)  │  │
  │  │  /v1/chat  │  │  idle -> ready ->    │  │
  │  │  /v1/resp  │  │  cooling -> alive    │  │
  │  │  /v1/msgs  │  └────────┬─────────────┘  │
  │  │  /v1/img   │           │               │
  │  └─────┬──────┘           ▼               │
  │  ┌─────────────────────────────────────┐   │
  │  │              agent.py               │   │
  │  │  OpenAI / Anthropic / Responses    │   │
  │  │        -> duck.ai single user msg   │   │
  │  └────────────────┬───────────────────┘   │
  └───────────────────┼──────────────────────┘
                      ▼
            pure_handshake.py
  GET /status -> solve_challenge.js (Node)
  -> X-Vqd-Hash-1 token -> POST /chat
```

## 📁 文件结构

```
duck2api_pro/
├── duck2api_pro_proxy.py   # 主网关：HTTP 服务 + 多端点
├── agent.py                # 协议层：格式转换 + 工具调用
├── identity_pool.py        # 匿名身份池：轮换 + 熔断
└── README.md
```

## 🔧 关键设计

### append_tool_retry（自我修正重试）

duck.ai 不支持原生 tool calling。原版用 nudge 在 user 消息前加提示，但模型看不到自己之前犯错的上下文。

本仓库把模型之前的非合规回复作为 Assistant 轮次插入，再追加 User 修正提示，让模型在上下文中自我纠正。

### IdentityPool（身份轮换）

duck.ai 匿名身份靠 cookie + TLS 指纹 + challenge token 三者绑定。单一身份触发限流后只能换新身份。

IdentityPool 维护 N 个 PureHandshake 实例，每个独立 cookie/token。限流时自动切换，支持手动轮换和熔断。

### 协议约束

duck.ai 后端只接受单条 user 消息。实测：

- 单条 user -> HTTP 200
- 多角色 messages -> HTTP 400
- durableStream 字段 -> HTTP 400
- 服务端会话记忆 -> 第二轮问名字答 I don't know

因此所有多轮上下文和工具调用都通过 prompt 工程模拟。

## ⚠️ 注意事项

- duck.ai 模型质量决定工具调用成功率
- 并发会把 duck.ai 打成 429，pool-size 应 >= 请求并发数
- 需要代理访问 duck.ai（部分地区 451）

## 📄 License

MIT