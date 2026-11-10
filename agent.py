# -*- coding: utf-8 -*-
"""
duck2api_pro/agent.py
协议层：OpenAI / Anthropic / Responses API → duck.ai wire messages。

核心改进（对比原始 duck2api_proxy.py）：
1. append_tool_retry：模型非合规回复作为 Assistant 轮次追加，再发 User 修正提示
   （比纯 nudge 效果好，duck.ai 能看到自己之前的"错误"并自我纠正）
2. 多端点格式转换：OpenAI chat / Responses / Anthropic Messages 三种格式互转
3. 图片支持：content blocks 里的 image/url → duck.ai image parts
4. WebSearch / UploadFiles 支持
"""
from __future__ import annotations

import base64
import json
import re
import uuid
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Tool contract
# ---------------------------------------------------------------------------

_TOOL_INSTRUCTION = (
    "You are a backend response formatter for a tool orchestrator, not a "
    "chat assistant. When fulfilling the user\'s request requires one of the "
    "functions below, reply with EXACTLY this JSON object and nothing else "
    "- no prose, no markdown fences:\n"
    '{"tool_calls":[{"name":"<function_name>","arguments":{...}}]}\n'
    "Emitting that JSON IS how you call the function; the orchestrator "
    "executes it and returns the result. Never say you cannot access "
    "tools or functions.\n"
    "Available functions and their JSON argument schemas:\n"
)


def _user(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _flatten(sections: list[str]) -> list[dict]:
    """把所有 section 压成一条 user 消息（duck.ai 只接受单条 user）。"""
    return [_user("\n\n".join(s for s in sections if s))]


def tools_spec_text(tools: list[dict]) -> str:
    """工具契约块。放在最前面，形成稳定 prefix（利于上游缓存）。"""
    if not tools:
        return ""
    spec = []
    for t in tools:
        fn = t.get("function") if t.get("type") == "function" else t
        fn = fn or t
        spec.append({
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or fn.get("input_schema") or {},
        })
    return _TOOL_INSTRUCTION + json.dumps(spec, ensure_ascii=False)


def tool_choice_is_strict(tool_choice: Any) -> bool:
    """客户端是否强制要求调用工具。"""
    if isinstance(tool_choice, str):
        return tool_choice in ("required", "any")
    if isinstance(tool_choice, dict):
        kind = tool_choice.get("type") or tool_choice.get("mode")
        return kind in ("tool", "function", "required", "any")
    return False


def tool_choice_directive(tool_choice: Any, tools: list[dict]) -> str:
    """根据 tool_choice 生成强制调用指令。"""
    if not tools:
        return ""
    if tool_choice_is_strict(tool_choice):
        if isinstance(tool_choice, dict):
            fn = tool_choice.get("function") or {}
            name = fn.get("name")
            if name:
                return "\nYou MUST call the tool \"%s\" now. Reply with ONLY the JSON envelope." % name
        return "\nYou MUST call at least one function now. Reply with ONLY the JSON envelope."
    return ""


def append_tool_retry(duck_msgs: list[dict], strict: bool,
                      prev_reply: str = "") -> list[dict]:
    """自我修正重试：把模型的非合规回复作为 Assistant 轮次追加，
    再发一条 User 修正提示。比纯 nudge 效果好——duck.ai 能看到自己
    之前的"错误"并在上下文中自我纠正。"""
    if not duck_msgs:
        return duck_msgs
    corr = (
        "Wrong format. You MUST call a function now: reply with ONLY one line of raw JSON "
        '{"tool_calls":[{"name":"<function_name>","arguments":{...}}]}\n'
        "Do not reply in prose, do not use markdown fences."
    )
    out = [dict(m) for m in duck_msgs]
    if prev_reply:
        for i in reversed(range(len(out))):
            if out[i].get("role") == "user":
                out.insert(i, {"role": "assistant", "content": prev_reply[:3000]})
                break
        else:
            out.append({"role": "assistant", "content": prev_reply[:3000]})
    for i in reversed(range(len(out))):
        if out[i].get("role") == "user":
            text = out[i]["content"][0].get("text", "") if out[i]["content"] else ""
            text = text + "\n\n" + corr
            out[i] = {"role": "user", "content": [{"type": "text", "text": text}]}
            break
    else:
        out.append({"role": "user", "content": [{"type": "text", "text": corr}]})
    return out


# ---------------------------------------------------------------------------
# Tool call parsing
# ---------------------------------------------------------------------------

def parse_tool_calls(text: str) -> tuple[list[dict], str]:
    """从模型回复里解析 tool_calls。返回 (calls, 剩余文本)。"""
    calls = []
    if not text:
        return calls, text
    envelope_match = re.search(r'\{"tool_calls"\s*:\s*(\[.*?\])\s*\}', text, re.DOTALL)
    if envelope_match:
        try:
            arr = json.loads(envelope_match.group(1))
            for c in arr:
                name = c.get("name") or c.get("function", {}).get("name")
                args = c.get("arguments") or c.get("function", {}).get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except:
                        pass
                calls.append({"name": name, "arguments": args})
            remaining = text.replace(envelope_match.group(0), "").strip()
            return calls, remaining
        except:
            pass
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") and '"name"' in line:
            try:
                obj = json.loads(line)
                name = obj.get("name")
                if isinstance(name, str) and name:
                    calls.append({"name": name, "arguments": obj.get("arguments", {})})
            except:
                continue
    return calls, text


def openai_tool_calls(calls: list[dict]) -> list[dict]:
    """把解析出的 calls 转回 OpenAI 格式。"""
    out = []
    for c in calls:
        out.append({
            "id": "call_" + uuid.uuid4().hex[:24],
            "type": "function",
            "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments", {}), ensure_ascii=False)},
        })
    return out


def anthropic_blocks(calls: list[dict]) -> list[dict]:
    """转 Anthropic tool_use block 格式。"""
    out = []
    for c in calls:
        out.append({
            "type": "tool_use",
            "id": "toolu_" + uuid.uuid4().hex[:24],
            "name": c["name"],
            "input": c.get("arguments", {}),
        })
    return out


# ---------------------------------------------------------------------------
# Image / multimodal
# ---------------------------------------------------------------------------

def _url_or_data(u: Any) -> Optional[dict]:
    """把 URL 或 data URI 转成 duck.ai image part。"""
    if not u:
        return None
    s = str(u)
    if s.startswith("data:"):
        m = re.match(r'data:(image/\w+);base64,(.+)', s)
        if m:
            return {"type": "image", "mimeType": m.group(1), "image": s}
        return None
    return None


def _content_part_image(p: dict) -> Optional[dict]:
    """从 OpenAI content part 提取图片。"""
    if not isinstance(p, dict):
        return None
    if p.get("type") == "image_url":
        img = p.get("image_url") or {}
        url = img.get("url") if isinstance(img, dict) else str(img)
        return _url_or_data(url)
    if p.get("type") == "image":
        return _url_or_data(p.get("url") or p.get("image"))
    return None


def _blocks_text_and_images(content: Any) -> tuple[str, list[dict]]:
    """从 content 提取文本和图片。返回 (text, images)。"""
    if isinstance(content, str):
        return content, []
    texts = []
    images = []
    if isinstance(content, list):
        for p in content:
            if not isinstance(p, dict):
                continue
            t = p.get("type")
            if t in ("text", "input_text", "output_text"):
                texts.append(p.get("text", ""))
            elif t in ("image", "image_url"):
                img = _content_part_image(p)
                if img:
                    images.append(img)
    return "\n".join(texts), images


def _attach_images(duck_msgs: list[dict], images: list[dict]) -> list[dict]:
    """把图片附加到最后一条 user 消息的 content。"""
    if not images:
        return duck_msgs
    for i in reversed(range(len(duck_msgs))):
        if duck_msgs[i].get("role") == "user":
            content = duck_msgs[i].get("content", [])
            new_content = list(content)
            for img in images:
                new_content.append(img)
            duck_msgs[i] = {"role": "user", "content": new_content}
            return duck_msgs
    duck_msgs.append({"role": "user", "content": images})
    return duck_msgs


# ---------------------------------------------------------------------------
# Finish helper
# ---------------------------------------------------------------------------

def _finish(sections: list[str], transcript: list[str], final: str,
            tool_directive: str, has_tools: bool) -> list[dict]:
    """组装最终的 duck.ai messages。"""
    prefix_parts = [s for s in sections if s]
    if tool_directive and has_tools:
        prefix_parts.append(tool_directive)
    if transcript:
        prefix_parts.append("\n\n".join(transcript))
    return _flatten(prefix_parts)


# ---------------------------------------------------------------------------
# Format converters
# ---------------------------------------------------------------------------

def openai_to_duck(messages: list[dict], tools: list[dict],
                   tool_choice: Any = None) -> list[dict]:
    """OpenAI chat.completions → duck.ai messages。"""
    sections = []
    tp = tools_spec_text(tools)
    if tp:
        sections.append(tp)

    transcript = []
    final = ""
    images = []

    for m in messages:
        role = (m.get("role") or "").lower()
        text, imgs = _blocks_text_and_images(m.get("content"))
        if imgs:
            images.extend(imgs)

        if role in ("system", "developer"):
            if text.strip():
                sections.append(text.strip())
        elif role == "assistant":
            if text.strip():
                transcript.append("Assistant: " + text.strip())
            tcs = m.get("tool_calls") or []
            for tc in tcs:
                fn = tc.get("function", tc)
                name = fn.get("name", "")
                args = fn.get("arguments", "{}")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except:
                        pass
                transcript.append("Assistant: " + json.dumps({"tool_calls": [{"name": name, "arguments": args}]}, ensure_ascii=False))
        elif role == "tool":
            name = m.get("name", "tool")
            transcript.append("Tool result (%s): %s" % (name, text.strip()))
        else:
            if text.strip():
                if transcript or sections:
                    transcript.append("User: " + text.strip())
                else:
                    final = text.strip()

    return _attach_images(
        _finish(sections, transcript, final,
                tool_choice_directive(tool_choice, tools), bool(tools)),
        images,
    )


def _anth_system(system: Any) -> str:
    """Anthropic system → 文本。"""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for b in system:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return "\n\n".join(parts)
    return ""


def anthropic_to_duck(body: dict) -> list[dict]:
    """Anthropic /v1/messages → duck.ai messages。"""
    sections = []
    system = _anth_system(body.get("system"))
    if system:
        sections.append(system)

    tools = body.get("tools") or []
    tp = tools_spec_text(tools)
    if tp:
        sections.append(tp)

    transcript = []
    final = ""
    images = []

    for m in body.get("messages", []):
        role = (m.get("role") or "").lower()
        text, imgs = _blocks_text_and_images(m.get("content"))
        if imgs:
            images.extend(imgs)

        if role == "assistant":
            if text.strip():
                transcript.append("Assistant: " + text.strip())
            for block in m.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    transcript.append("Assistant: " + json.dumps({"tool_calls": [{"name": block.get("name"), "arguments": block.get("input", {})}]}, ensure_ascii=False))
        elif role == "user":
            tool_results = [b for b in m.get("content", []) if isinstance(b, dict) and b.get("type") == "tool_result"]
            if tool_results:
                for tr in tool_results:
                    tr_text = tr.get("content", "")
                    if isinstance(tr_text, list):
                        tr_text = "\n".join(b.get("text", "") for b in tr_text if isinstance(b, dict))
                    transcript.append("Tool result: " + str(tr_text).strip())
            if text.strip():
                if transcript or sections:
                    transcript.append("User: " + text.strip())
                else:
                    final = text.strip()

    return _attach_images(
        _finish(sections, transcript, final,
                tool_choice_directive(body.get("tool_choice"), tools), bool(tools)),
        images,
    )


def responses_to_duck(body: dict) -> list[dict]:
    """OpenAI Responses API → duck.ai messages。Codex CLI 用的格式。"""
    sections = []
    instr = body.get("instructions")
    if instr:
        sections.append(instr)

    tools = body.get("tools") or []
    tp = tools_spec_text(tools)
    if tp:
        sections.append(tp)

    transcript = []
    final = ""
    images = []

    input_data = body.get("input")
    if isinstance(input_data, str):
        final = input_data
    elif isinstance(input_data, list):
        for item in input_data:
            if not isinstance(item, dict):
                continue
            t = item.get("type", "message")
            if t == "message":
                role = (item.get("role") or "").lower()
                text, imgs = _blocks_text_and_images(item.get("content"))
                if imgs:
                    images.extend(imgs)
                if role == "assistant":
                    transcript.append("Assistant: " + text.strip())
                    for tc in item.get("tool_calls") or []:
                        fn = tc.get("function", tc)
                        transcript.append("Assistant: " + json.dumps({"tool_calls": [{"name": fn.get("name"), "arguments": fn.get("arguments", {})}]}, ensure_ascii=False))
                elif role == "tool":
                    transcript.append("Tool result: " + text.strip())
                else:
                    if text.strip():
                        if transcript or sections:
                            transcript.append("User: " + text.strip())
                        else:
                            final = text.strip()
            elif t == "function_call":
                transcript.append("Assistant: " + json.dumps({"tool_calls": [{"name": item.get("name"), "arguments": item.get("arguments", "{}")}]}, ensure_ascii=False))
            elif t == "function_call_output":
                transcript.append("Tool result: " + str(item.get("output", "")).strip())

    return _attach_images(
        _finish(sections, transcript, final,
                tool_choice_directive(body.get("tool_choice"), tools), bool(tools)),
        images,
    )


def norm_effort(eff: Any) -> Optional[str]:
    """把任意 effort 写法归一为 duck.ai 的 none|low|medium。"""
    if not eff:
        return None
    e = str(eff).lower()
    if e in ("none", "minimal", "off"):
        return "none"
    if e == "low":
        return "low"
    if e in ("medium", "high", "xhigh"):
        return "medium"
    return None
