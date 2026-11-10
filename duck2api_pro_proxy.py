# -*- coding: utf-8 -*-
"""
duck2api_pro_proxy.py
增强版 Duck2API 网关。

相对原始 duck2api_proxy.py 的改进：
1. 增强工具调用模拟（append_tool_retry 自我修正）
2. 匿名身份池轮换（绕配额）
3. 多端点：/v1/chat/completions, /v1/responses, /v1/messages, /v1/images/generations
4. WebSearch / UploadFiles 支持
5. /v1/admin/pool 身份池状态

复用 duck2api_proxy.py 的 PureHandshake / PureUpstream 握手实现，
新增 agent / identity_pool 模块。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# 导入原始模块的握手和上游
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + r'\duck-apk')
import pure_handshake as ph

# 导入增强模块
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import agent as ag
from identity_pool import IdentityPool


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class Cfg:
    def __init__(self):
        self.host = "127.0.0.1"
        self.port = 7750
        self.key = ""
        self.upstream = "https://duck.ai"
        self.ua = ph.DEFAULT_UA
        self.node = None
        self.proxy = None
        self.trust_env = True
        self.insecure = False
        self.connect_timeout = 20
        self.read_timeout = 600
        self.token_ttl = 1800
        self.pool_size = 3
        self.default_model = "gpt-6-luna"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class DuckError(Exception):
    def __init__(self, etype, message, status=0):
        self.etype = etype
        self.message = message
        self.status = status
        super().__init__(message)


_ERR_MAP = {
    "ERR_BAD_REQUEST": "invalid_request_error",
    "ERR_BAN": "permission_error",
    "ERR_BN_LIMIT": "rate_limit_error",
    "ERR_CHALLENGE": "authentication_error",
    "ERR_USER_LIMIT": "rate_limit_error",
    "ERR_ACCOUNT_LIMIT": "rate_limit_error",
    "ERR_DAILY_LIMIT": "rate_limit_error",
    "ERR_WEEKLY_LIMIT": "rate_limit_error",
    "ERR_COUNTRY_NOT_SUPPORTED": "permission_error",
    "ERR_MODEL_NOT_FOUND": "not_found_error",
}


def classify_error(etype, http_status):
    return _ERR_MAP.get(etype, "server_error" if http_status >= 500 else "invalid_request_error")


def status_for(etype):
    if "rate_limit" in etype:
        return 429
    if etype in ("permission_error", "authentication_error"):
        return 403 if etype == "authentication_error" else 451
    if etype == "not_found_error":
        return 404
    return 400


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------

class SSEParser:
    def __init__(self):
        self.buf = ""
        self.event = "message"
        self.data_lines = []

    def feed(self, line: str):
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload == "[DONE]":
                return None
            try:
                return json.loads(payload)
            except:
                return None
        return None


# ---------------------------------------------------------------------------
# Model catalog (fallback)
# ---------------------------------------------------------------------------

def load_fallback_models():
    fp = os.path.join(HERE, "..", "duck-apk", "models-extracted.json")
    if os.path.exists(fp):
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    return [
        {"id": "gpt-6-luna", "provider": "openai", "entityHasAccess": True},
        {"id": "gpt-5.4-mini", "provider": "openai", "entityHasAccess": True},
    ]


# ---------------------------------------------------------------------------
# Model picking
# ---------------------------------------------------------------------------

def pick_model(requested, available, default_model):
    ids = [m["id"] for m in available if m.get("entityHasAccess", True)]
    if not ids:
        ids = [m["id"] for m in available]
    if not ids:
        ids = [default_model]
    if requested:
        for i in ids:
            if i == requested:
                return i
        for i in ids:
            if i.lower() == requested.lower():
                return i
    if default_model in ids:
        return default_model
    return ids[0]


# ---------------------------------------------------------------------------
# Image generation (duck.ai GenerateImage tool)
# ---------------------------------------------------------------------------

def build_image_body(prompt, model, **kwargs):
    """构造图片生成请求。
    duck.ai 的图片生成是服务端工具，通过 metadata.toolChoice 强制开启。
    """
    return {
        "model": model,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        "canUseTools": True,
        "canDelegateImageGeneration": True,
        "metadata": {"toolChoice": {"GenerateImage": True}},
        "reasoningEffort": "none",
        "canUseApproxLocation": False,
        "canUseWebSearch": False,
        "canUploadFiles": False,
        "canShowGreeting": False,
    }


def extract_image_from_sse(obj):
    """从 SSE 对象里提取图片（ui-component 事件）。"""
    if obj.get("role") == "ui-component":
        if obj.get("name") == "generate-image" and obj.get("state") == "data":
            data = obj.get("data", {})
            b64 = data.get("b64Image") or data.get("image")
            if b64:
                return {"b64": b64, "format": data.get("format", "png"),
                        "width": data.get("width", 0), "height": data.get("height", 0)}
    return None


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

class Gateway:
    def __init__(self, cfg):
        self.cfg = cfg
        self.pool = IdentityPool(
            size=cfg.pool_size, base=cfg.upstream, ua=cfg.ua,
            node=cfg.node, proxy=cfg.proxy, trust_env=cfg.trust_env,
            insecure=cfg.insecure, token_ttl=cfg.token_ttl)
        self._models = None
        self._models_ts = 0
        self._model_lock = threading.Lock()

    def close(self):
        self.pool.close()

    def models(self):
        """获取模型列表（带缓存）。"""
        with self._model_lock:
            if self._models and (time.time() - self._models_ts) < 3600:
                return self._models
            # 尝试从 duck.ai 拉取
            try:
                tok, ua = self.pool.solve()
                r = self.pool._identities[self.pool._current].hs.s.get(
                    self.cfg.upstream.rstrip("/") + "/duckchat/v1/models",
                    headers={"Accept": "application/json", "User-Agent": ua,
                             "Origin": self.cfg.upstream, "Referer": self.cfg.upstream + "/"},
                    timeout=15, verify=not self.cfg.insecure)
                if r.status_code == 200:
                    data = r.json()
                    models = data.get("models", data if isinstance(data, list) else [])
                    if models:
                        self._models = models
                        self._models_ts = time.time()
                        return models
            except:
                pass
            # fallback
            self._models = load_fallback_models()
            self._models_ts = time.time()
            return self._models

    def chat(self, body, on_line=None):
        """发送 chat 请求。返回 (status, raw_text)。"""
        status, raw, _ = self.pool.chat(body, on_line=on_line, read_timeout=self.cfg.read_timeout)
        return status, raw

    def image(self, body, on_line=None):
        """图片生成。返回 (status, image_dicts, raw_text)。"""
        images = []
        def img_on_line(line):
            if on_line:
                on_line(line)
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload == "[DONE]":
                    return
                try:
                    obj = json.loads(payload)
                    img = extract_image_from_sse(obj)
                    if img:
                        images.append(img)
                except:
                    pass

        status, raw, _ = self.pool.chat(body, on_line=img_on_line, read_timeout=self.cfg.read_timeout)
        return status, images, raw


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "duck2api-pro/2.0.0"
    gateway: Gateway = None

    def log_message(self, fmt, *args):
        print("[*] %s - %s" % (self.address_string(), fmt % args), file=sys.stderr)

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _error(self, code, etype, message):
        self._json(code, {"error": {"message": message, "type": etype, "code": etype}})

    def _sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True

    def _sse(self, obj):
        data = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")
        self.wfile.write(data)
        self.wfile.flush()

    def _sse_done(self):
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _read_body(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size_line = self.rfile.readline().strip()
                if not size_line:
                    break
                size = int(size_line.split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _authorized(self):
        key = self.gateway.cfg.key
        if not key:
            return True
        auth = self.headers.get("Authorization", "")
        got = auth.strip()
        if got.lower().startswith("bearer "):
            got = got[7:].strip()
        return got == key

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._authorized():
            return self._error(401, "invalid_api_key", "Missing or invalid API key.")
        if path in ("/", "/health", "/v1"):
            return self._json(200, {"status": "ok", "service": "duck2api-pro",
                                    "pool": self.gateway.pool.stats()})
        if path == "/v1/models":
            models = self.gateway.models()
            data = []
            for m in models:
                if not m.get("entityHasAccess", True):
                    continue
                data.append({"id": m["id"], "object": "model", "created": 0,
                             "owned_by": (m.get("provider") or "duck.ai") or "duck.ai"})
            return self._json(200, {"object": "list", "data": data})
        if path == "/v1/admin/pool":
            return self._json(200, self.gateway.pool.stats())
        return self._error(404, "not_found", "Unknown path: " + path)

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._authorized():
            return self._error(401, "invalid_api_key", "Missing or invalid API key.")
        try:
            if path == "/v1/chat/completions":
                return self._handle_chat(False)
            elif path == "/v1/responses":
                return self._handle_responses()
            elif path == "/v1/messages":
                return self._handle_messages()
            elif path == "/v1/images/generations":
                return self._handle_images()
            elif path == "/v1/admin/rotate":
                self.gateway.pool.rotate()
                return self._json(200, {"status": "rotated", "pool": self.gateway.pool.stats()})
            return self._error(404, "not_found", "Unknown path: " + path)
        except DuckError as e:
            return self._error(status_for(e.etype), e.etype, e.message)
        except Exception as e:
            return self._error(500, "server_error", str(e)[:300])

    # ------------------------------------------------------------------
    # Endpoint handlers
    # ------------------------------------------------------------------

    def _handle_chat(self, is_stream):
        """POST /v1/chat/completions — OpenAI 兼容。"""
        raw = self._read_body()
        if not raw:
            return self._error(400, "invalid_request_error", "Empty body.")
        try:
            body = json.loads(raw)
        except:
            return self._error(400, "invalid_request_error", "Invalid JSON.")

        model = pick_model(body.get("model"), self.gateway.models(), self.gateway.cfg.default_model)
        messages = body.get("messages", [])
        tools = body.get("tools", [])
        tool_choice = body.get("tool_choice")
        stream = body.get("stream", False)
        reasoning = ag.norm_effort(body.get("reasoning_effort") or body.get("reasoningEffort"))

        # 转换格式
        duck_msgs = ag.openai_to_duck(messages, tools, tool_choice)

        # 构造 duck.ai body
        chat_body = {
            "model": model,
            "messages": duck_msgs,
            "canUseTools": bool(tools) if not body.get("canUseTools") else body["canUseTools"],
            "reasoningEffort": reasoning or "none",
            "canUseApproxLocation": body.get("canUseApproxLocation", False),
            "canUseWebSearch": body.get("canUseWebSearch", False),
            "canUploadFiles": body.get("canUploadFiles", False),
            "canShowGreeting": False,
        }

        if not stream:
            status, raw_text = self.gateway.chat(chat_body)
            if status != 200:
                return self._error(status, classify_error("ERR_BAD_REQUEST", status),
                                    raw_text[:300])
            # 解析 SSE
            full_text = ""
            for line in raw_text.splitlines():
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        continue
                    try:
                        obj = json.loads(payload)
                        msg = obj.get("message", "")
                        if msg:
                            full_text += msg
                    except:
                        pass
            # 工具调用解析
            calls, remaining = ag.parse_tool_calls(full_text)
            resp = {
                "id": "chatcmpl-" + hashlib.sha1(full_text.encode()).hexdigest()[:24],
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": remaining or full_text},
                    "finish_reason": "tool_calls" if calls else "stop",
                }],
                "usage": {"prompt_tokens": len(raw), "completion_tokens": len(full_text),
                          "total_tokens": len(raw) + len(full_text)},
            }
            if calls:
                resp["choices"][0]["message"]["tool_calls"] = ag.openai_tool_calls(calls)
                resp["choices"][0]["message"]["content"] = ""
            return self._json(200, resp)
        else:
            # 流式
            self._sse_headers()
            def on_line(line):
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        self._sse_done()
                        return
                    try:
                        obj = json.loads(payload)
                        msg = obj.get("message", "")
                        if msg:
                            self._sse({"id": "chatcmpl-" + hashlib.sha1(msg.encode()).hexdigest()[:24],
                                       "object": "chat.completion.chunk",
                                       "created": int(time.time()),
                                       "model": model,
                                       "choices": [{"index": 0, "delta": {"content": msg}, "finish_reason": None}]})
                    except:
                        pass
            status, raw_text = self.gateway.chat(chat_body, on_line=on_line)
            if status != 200:
                self._sse({"error": {"message": raw_text[:300], "type": "server_error", "code": "server_error"}})
            self._sse_done()

    def _handle_responses(self):
        """POST /v1/responses — OpenAI Responses API（Codex CLI 用）。"""
        raw = self._read_body()
        if not raw:
            return self._error(400, "invalid_request_error", "Empty body.")
        body = json.loads(raw)
        model = pick_model(body.get("model"), self.gateway.models(), self.gateway.cfg.default_model)
        duck_msgs = ag.responses_to_duck(body)
        reasoning = ag.norm_effort(body.get("reasoning") or body.get("reasoning_effort"))
        chat_body = {
            "model": model, "messages": duck_msgs,
            "canUseTools": bool(body.get("tools")),
            "reasoningEffort": reasoning or "none",
            "canUseApproxLocation": False, "canUseWebSearch": False,
            "canUploadFiles": False, "canShowGreeting": False,
        }
        status, raw_text = self.gateway.chat(chat_body)
        if status != 200:
            return self._error(status, "server_error", raw_text[:300])
        full_text = ""
        for line in raw_text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload == "[DONE]": continue
                try:
                    obj = json.loads(payload)
                    msg = obj.get("message", "")
                    if msg: full_text += msg
                except: pass
        return self._json(200, {
            "id": "resp_" + hashlib.sha1(full_text.encode()).hexdigest()[:24],
            "object": "response", "created_at": int(time.time()),
            "status": "completed", "model": model,
            "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": full_text}]}],
        })

    def _handle_messages(self):
        """POST /v1/messages — Anthropic Messages API（Claude Code 用）。"""
        raw = self._read_body()
        if not raw:
            return self._error(400, "invalid_request_error", "Empty body.")
        body = json.loads(raw)
        model = pick_model(body.get("model"), self.gateway.models(), self.gateway.cfg.default_model)
        duck_msgs = ag.anthropic_to_duck(body)
        reasoning = ag.norm_effort(body.get("thinking") or body.get("reasoning_effort"))
        chat_body = {
            "model": model, "messages": duck_msgs,
            "canUseTools": bool(body.get("tools")),
            "reasoningEffort": reasoning or "none",
            "canUseApproxLocation": False, "canUseWebSearch": False,
            "canUploadFiles": False, "canShowGreeting": False,
        }
        status, raw_text = self.gateway.chat(chat_body)
        if status != 200:
            return self._error(status, "server_error", raw_text[:300])
        full_text = ""
        for line in raw_text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload == "[DONE]": continue
                try:
                    obj = json.loads(payload)
                    msg = obj.get("message", "")
                    if msg: full_text += msg
                except: pass
        calls, remaining = ag.parse_tool_calls(full_text)
        content = [{"type": "text", "text": remaining or full_text}]
        if calls:
            content = [{"type": "text", "text": ""}] + ag.anthropic_blocks(calls)
        stop_reason = "tool_use" if calls else "end_turn"
        return self._json(200, {
            "id": "msg_" + hashlib.sha1(full_text.encode()).hexdigest()[:24],
            "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": stop_reason,
            "usage": {"input_tokens": len(raw), "output_tokens": len(full_text)},
        })

    def _handle_images(self):
        """POST /v1/images/generations — 图片生成。"""
        raw = self._read_body()
        if not raw:
            return self._error(400, "invalid_request_error", "Empty body.")
        body = json.loads(raw)
        prompt = body.get("prompt", "")
        model = pick_model(body.get("model", "gpt-6-luna"), self.gateway.models(), self.gateway.cfg.default_model)
        chat_body = build_image_body(prompt, model)
        status, images, raw_text = self.gateway.image(chat_body)
        if status != 200:
            return self._error(status, "server_error", raw_text[:300])
        if not images:
            return self._error(400, "no_image", "No image generated.")
        out = []
        for img in images:
            out.append({"b64_json": img["b64"], "url": None})
        return self._json(200, {"created": int(time.time()), "data": out})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(description="Duck2API Pro — enhanced gateway")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7750)
    ap.add_argument("--key", default="", help="API key (empty = no auth)")
    ap.add_argument("--upstream", default="https://duck.ai")
    ap.add_argument("--node", default=None)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--trust-env", action="store_true", default=True)
    ap.add_argument("--no-trust-env", dest="trust_env", action="store_false")
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--pool-size", type=int, default=3)
    ap.add_argument("--default-model", default="gpt-6-luna")
    ap.add_argument("--selftest", action="store_true")
    return ap


def selftest(cfg):
    """离线自检。"""
    print("[*] Duck2API Pro selftest")
    # 1. 握手
    try:
        h = ph.PureHandshake(base=cfg.upstream, ua=cfg.ua, node=cfg.node,
                             proxy=cfg.proxy, trust_env=cfg.trust_env,
                             insecure=cfg.insecure)
        tok, ua = h.solve(force=True)
        print("  [OK] handshake: token=%d chars ua=%s..." % (len(tok), ua[:40]))
        h.close()
    except Exception as e:
        print("  [FAIL] handshake: %s" % e)
        return False
    # 2. agent 模块
    try:
        msgs = ag.openai_to_duck(
            [{"role": "system", "content": "You are helpful."},
             {"role": "user", "content": "Hi"}],
            tools=[{"type": "function", "function": {"name": "get_time", "description": "Get current time", "parameters": {}}}],
            tool_choice=None)
        assert len(msgs) == 1 and msgs[0]["role"] == "user"
        assert "get_time" in msgs[0]["content"][0]["text"]
        print("  [OK] agent.openai_to_duck: %d messages, tools in prompt" % len(msgs))
    except Exception as e:
        print("  [FAIL] agent: %s" % e)
        return False
    # 3. identity pool
    try:
        pool = IdentityPool(size=cfg.pool_size, base=cfg.upstream, ua=cfg.ua,
                            node=cfg.node, proxy=cfg.proxy, trust_env=cfg.trust_env,
                            insecure=cfg.insecure)
        s = pool.stats()
        assert s["size"] == cfg.pool_size
        pool.close()
        print("  [OK] identity_pool: %d identities" % cfg.pool_size)
    except Exception as e:
        print("  [FAIL] identity_pool: %s" % e)
        return False
    print("[*] All checks passed.")
    return True


def main():
    ap = build_parser()
    cfg = ap.parse_args()
    cfg.ua = ph.DEFAULT_UA
    cfg.token_ttl = 1800
    cfg.read_timeout = 600
    cfg.connect_timeout = 20
    if cfg.selftest:
        sys.exit(0 if selftest(cfg) else 1)

    gw = Gateway(cfg)
    Handler.gateway = gw
    server = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    print("[*] Duck2API Pro on http://%s:%d/v1" % (cfg.host, cfg.port))
    print("[*] Pool: %d identities" % cfg.pool_size)
    print("[*] Endpoints: /v1/chat/completions, /v1/responses, /v1/messages, /v1/images/generations")
    print("[*] Admin: /v1/admin/pool, /v1/admin/rotate")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Stopping...")
    finally:
        gw.close()
        server.server_close()


if __name__ == "__main__":
    main()
