#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backend.py —— 斑海豹 P0 服务客户端（POST /v1/systemone，纯标准库）。

P0 扁平协议：
    POST /v1/systemone
    {"model": "Phocinae-Largha-150M-v1", "state": str,
     "questions": [{"id", "type": noul|choice|score, "options"?, "threshold"?}]}
    → {"model", "answers": {id: 值}, "usage", "answer_confidence"}
    noul=bool；choice=int 下标；score=2–10。
"""

import json
import urllib.request

from . import __version__

DEFAULT_SERVER = "http://127.0.0.1:8155"
DEFAULT_MODEL = "Phocinae-Largha-150M-v1"
DEFAULT_TIMEOUT = 5.0

# 通道合成阈值（与 phocinae-guard 保持一致）
NOUL_THRESHOLD = 0.65
DENY_AT = 7.0
ASK_AT = 4.0


class SystemoneError(Exception):
    """服务不可用 / 超时 / 响应异常 / 应答缺失（fail-closed 触发条件）。"""


class SystemoneClient:
    """/v1/systemone 客户端。失败一律抛 SystemoneError，绝不静默放行。"""

    def __init__(self, server=DEFAULT_SERVER, timeout=DEFAULT_TIMEOUT,
                 model=DEFAULT_MODEL):
        self.server = (server or DEFAULT_SERVER).rstrip("/")
        self.timeout = max(0.1, float(timeout))
        self.model = model

    def _url(self, server=None):
        base = (server or self.server).rstrip("/")
        return base if base.endswith("/v1/systemone") else base + "/v1/systemone"

    def ask(self, questions, state, server=None, timeout=None):
        """POST /v1/systemone；网络/解析/结构异常 → SystemoneError。"""
        body = {
            "model": self.model,
            "state": state,
            "questions": questions,
        }
        req = urllib.request.Request(
            self._url(server),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "phocinae-mcp/" + __version__,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=float(timeout or self.timeout)) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except Exception as exc:  # 连接拒绝/超时/HTTP 错误码
            raise SystemoneError(
                "系统服务不可用（%s: %s，地址 %s）"
                % (type(exc).__name__, exc, self._url(server))) from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise SystemoneError("服务响应不是合法 JSON：%s" % exc) from exc
        if not isinstance(data, dict):
            raise SystemoneError("服务响应结构异常（顶层非 JSON 对象）")
        return data


# ------------------------------------------------------------------ 应答解包
def _unwrap(value, keys):
    """兼容 P0 扁平值与旧 mock 嵌套对象（{type, noul|score|choice, ...}）。"""
    seen = 0
    while isinstance(value, dict) and seen < 4:
        seen += 1
        hit = None
        for key in keys:
            if key in value:
                hit = value[key]
                break
        if hit is None:
            for key in ("answer", "value"):
                if key in value:
                    hit = value[key]
                    break
        if hit is None:
            return None
        value = hit
    return value


def unwrap_noul(value):
    return _unwrap(value, ("noul",))


def unwrap_choice(value):
    return _unwrap(value, ("choice", "index"))


def unwrap_score(value):
    return _unwrap(value, ("score",))


# ------------------------------------------------------------------ gate 通道合成
def noul_to_probability(noul):
    """扁平 bool → 放行概率（threshold 已在服务端生效）；数值原样传递。"""
    if noul is True:
        return 1.0
    if noul is False:
        return 0.0
    return float(noul)


def map_gate(noul_p, score, noul_threshold=NOUL_THRESHOLD,
             deny_at=DENY_AT, ask_at=ASK_AT):
    """阈值映射（通道合成取最严格）：
    score>=deny_at 或 noul=false → deny；score>=ask_at 或 noul 摇摆 → ask；
    其余 → allow。与 guard.map_l1 同口径。
    """
    if noul_p <= 1.0 - noul_threshold:
        noul_state = "false"
    elif noul_p >= noul_threshold:
        noul_state = "true"
    else:
        noul_state = "wavering"
    channels = []
    if score >= deny_at:
        channels.append("deny")
    elif score >= ask_at:
        channels.append("ask")
    else:
        channels.append("allow")
    if noul_state == "false":
        channels.append("deny")
    elif noul_state == "wavering":
        channels.append("ask")
    else:
        channels.append("allow")
    if "deny" in channels:
        decision = "deny"
    elif "ask" in channels:
        decision = "ask"
    else:
        decision = "allow"
    return decision, noul_state
