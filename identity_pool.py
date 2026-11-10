# -*- coding: utf-8 -*-
"""
duck2api_pro/identity_pool.py
匿名身份池：多 profile 轮换绕配额。

原理：
- duck.ai 的匿名身份靠 cookie + TLS 指纹 + challenge 解 token 三者绑定
- 单一身份触发 ERR_*_LIMIT 后，只能换新身份（新 profile）才能恢复
- 这里维护多个 PureHandshake 实例，每个实例独立 cookie/token
- 当前身份被限流时，自动切到下一个

用法：
    pool = IdentityPool(size=3)
    pool.solve()        # 选一个健康身份解 token
    pool.chat(body)     # 用当前身份发请求
    pool.rotate()       # 手动轮换
    pool.close()
"""
from __future__ import annotations

import os
import random
import threading
import time
from typing import Optional

import pure_handshake as ph


class Identity:
    """一个匿名身份 = 一个 PureHandshake 实例。"""

    __slots__ = ("idx", "hs", "token", "token_ts", "state", "cool_until",
                 "requests", "errors", "limits")

    def __init__(self, idx: int, base: str, ua: str, node: str = None,
                 proxy: str = None, trust_env: bool = False,
                 insecure: bool = False):
        self.idx = idx
        self.hs = ph.PureHandshake(base=base, ua=ua, node=node, proxy=proxy,
                                   trust_env=trust_env, insecure=insecure)
        self.token: Optional[str] = None
        self.token_ts: float = 0.0
        self.state = "idle"       # idle | ready | cooling | dead
        self.cool_until: float = 0.0
        self.requests: int = 0
        self.errors: int = 0
        self.limits: list[str] = []

    def solve(self, force: bool = False) -> Optional[tuple[str, str]]:
        if self.state == "cooling" and time.time() < self.cool_until:
            return None
        try:
            tok, ua = self.hs.solve(force=force)
            self.token = tok
            self.token_ts = time.time()
            self.state = "ready"
            self.requests += 1
            return tok, ua
        except Exception as e:
            self.errors += 1
            self.state = "cooling"
            self.cool_until = time.time() + 120
            return None

    def mark_limited(self, hard: bool = False):
        self.state = "cooling" if hard else "ready"
        if hard:
            self.cool_until = time.time() + 120
            self.limits.append(time.strftime("%H:%M:%S"))
        else:
            self.cool_until = time.time() + 10

    def close(self):
        try:
            self.hs.close()
        except:
            pass


class IdentityPool:
    """多身份轮换池。线程安全。"""

    def __init__(self, size: int = 3, base: str = "https://duck.ai",
                 ua: str = ph.DEFAULT_UA, node: str = None,
                 proxy: str = None, trust_env: bool = False,
                 insecure: bool = False, token_ttl: float = 1800):
        self.base = base
        self.ua = ua
        self.node = node
        self.proxy = proxy
        self.trust_env = trust_env
        self.insecure = insecure
        self.token_ttl = token_ttl
        self._lock = threading.Lock()
        self._identities: list[Identity] = []
        self._current: int = 0
        self._ban_until: float = 0.0

        for i in range(size):
            self._identities.append(Identity(
                i, base, ua, node, proxy, trust_env, insecure))

    def _pick_ready(self) -> Optional[Identity]:
        """选一个 ready 的身份。"""
        with self._lock:
            if time.time() < self._ban_until:
                return None
            for offset in range(len(self._identities)):
                ident = self._identities[(self._current + offset) % len(self._identities)]
                if ident.state == "ready" and ident.token and                    (time.time() - ident.token_ts) < self.token_ttl:
                    self._current = (self._current + offset) % len(self._identities)
                    return ident
                if ident.state == "cooling" and time.time() >= ident.cool_until:
                    r = ident.solve(force=True)
                    if r:
                        self._current = (self._current + offset) % len(self._identities)
                        return ident
            return None

    def _solve_new(self) -> Optional[Identity]:
        """找一个 idle 身份求解。"""
        with self._lock:
            for i in range(len(self._identities)):
                ident = self._identities[(self._current + i) % len(self._identities)]
                if ident.state in ("idle", "dead"):
                    r = ident.solve(force=True)
                    if r:
                        self._current = (self._current + i) % len(self._identities)
                        return ident
            return None

    def solve(self) -> tuple[str, str]:
        """获取可用 token。返回 (token, ua)。"""
        ident = self._pick_ready()
        if ident:
            return ident.token, ident.hs.ua
        ident = self._solve_new()
        if ident:
            return ident.token, ident.hs.ua
        wait = max(0, min(i.cool_until for i in self._identities) - time.time()) + 1
        print("[pool] all identities cooling, waiting %.0fs" % wait, flush=True)
        time.sleep(min(wait, 120))
        ident = self._solve_new()
        if ident:
            return ident.token, ident.hs.ua
        raise RuntimeError("all identities exhausted")

    def rotate(self):
        """手动轮换到下一个身份。"""
        with self._lock:
            ident = self._identities[self._current]
            ident.state = "cooling"
            ident.cool_until = time.time() + 5
            self._current = (self._current + 1) % len(self._identities)

    def mark_limited(self, hard: bool = False):
        """当前身份被限流。"""
        with self._lock:
            if self._identities:
                ident = self._identities[self._current]
                ident.mark_limited(hard)
                if hard:
                    self._ban_until = time.time() + 5
                self._current = (self._current + 1) % len(self._identities)

    def chat(self, body: dict, on_line=None, read_timeout: float = 600):
        """用池子发请求。返回 (status, raw_text, ua)。"""
        tok, ua = self.solve()
        ident = self._identities[self._current]
        status, raw = ident.hs.chat(body, on_line=on_line, read_timeout=read_timeout)
        if status in (418, 429):
            self.mark_limited(hard=True)
        elif status >= 400 and "ERR_" in raw:
            self.mark_limited(hard=False)
        return status, raw, ua

    def stats(self) -> dict:
        with self._lock:
            return {
                "size": len(self._identities),
                "current": self._current,
                "banned_until": self._ban_until,
                "identities": [
                    {"idx": i.idx, "state": i.state,
                     "requests": i.requests, "errors": i.errors,
                     "limits": len(i.limits)}
                    for i in self._identities
                ],
            }

    def close(self):
        for i in self._identities:
            i.close()
