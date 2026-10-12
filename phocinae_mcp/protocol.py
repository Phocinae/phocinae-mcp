#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""protocol.py —— MCP 2026-07-28 协议对齐共享件（纯标准库）。

内容：
- 版本常量与错误码（2026-07-28 重编号：-32020 HeaderMismatch、
  -32021 MissingRequiredClientCapability、-32022 UnsupportedProtocolVersion）；
- 请求 _meta 校验（protocolVersion / clientCapabilities 必填）；
- 结果构造助手：resultType / ttlMs / cacheScope；
- MRTR requestState 的 HMAC 编解码（防篡改 + 过期 + 请求指纹）；
- 审批策略资源文本（经 resources/list + resources/read 暴露）；
- 日志门控：一律写 stderr，且受 io.modelcontextprotocol/logLevel 约束。

安全：requestState 由客户端回显、属攻击者可控输入 —— 一律 HMAC 校验、
过期检查与请求指纹比对，校验失败即拒绝。
"""

import base64
import hashlib
import hmac
import json
import secrets
import sys
import time

MODERN_VERSION = "2026-07-28"     # 现代协议：无握手、逐请求 _meta
LEGACY_VERSION = "2024-11-05"     # 旧客户端 initialize 握手年代（兼容保留）
SUPPORTED_VERSIONS = (MODERN_VERSION,)

META_PREFIX = "io.modelcontextprotocol/"
META_VERSION = META_PREFIX + "protocolVersion"
META_CLIENT_INFO = META_PREFIX + "clientInfo"
META_CLIENT_CAPS = META_PREFIX + "clientCapabilities"
META_LOG_LEVEL = META_PREFIX + "logLevel"

ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_HEADER_MISMATCH = -32020
ERR_MISSING_CAPABILITY = -32021
ERR_UNSUPPORTED_VERSION = -32022

# 缓存提示（2026-07-28：缓存型结果必带 ttlMs + cacheScope，二者同出）
TTL_TOOLS_MS = 300000            # 工具清单：5 分钟（进程生命周期内静态）
TTL_DISCOVER_MS = 3600000        # server/discover：1 小时
TTL_POLICY_MS = 3600000          # 审批策略文本：1 小时
CACHE_SCOPE_PUBLIC = "public"    # 三份结果均与调用方无关 → 可共享缓存

LOG_LEVELS = {
    "debug": 10, "info": 20, "notice": 30, "warning": 40,
    "error": 50, "critical": 60, "alert": 70, "emergency": 80,
}
DEFAULT_LOG_LEVEL = "info"


def log(level, message, meta=None):
    """日志只走 stderr；当请求 _meta 声明 logLevel 时按其下限门控。"""
    floor = DEFAULT_LOG_LEVEL
    if isinstance(meta, dict):
        declared = meta.get(META_LOG_LEVEL)
        if isinstance(declared, str) and declared in LOG_LEVELS:
            floor = declared
    if LOG_LEVELS.get(level, 100) < LOG_LEVELS.get(floor, 100):
        return
    sys.stderr.write("phocinae-mcp[%s]: %s\n" % (level, message))
    sys.stderr.flush()


# ------------------------------------------------------------------ MRTR state
_STATE_KEY = secrets.token_bytes(32)   # 每进程随机 HMAC 密钥
_STATE_TTL_SEC = 600                   # requestState 有效期 10 分钟


def encode_state(payload):
    """MRTR 中间态 → 不透明 requestState：base64url(json).sha256hmac。

    载荷含 exp 过期戳；客户端不得解析，篡改即失效。
    """
    payload = dict(payload)
    payload["exp"] = int(time.time()) + _STATE_TTL_SEC
    raw = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        .encode("utf-8")).rstrip(b"=")
    sig = hmac.new(_STATE_KEY, raw, hashlib.sha256).hexdigest()
    return raw.decode("ascii") + "." + sig


def decode_state(token, expect):
    """校验并解包 requestState。expect 为请求指纹 dict（如 {"tool": ...,
    "command": ...}），任一不符即 None。注意：本实现为纯函数式判定，
    重放仅得到相同判定结果，无副作用，故按 TTL+指纹 防护即足。"""
    if not isinstance(token, str) or "." not in token:
        return None
    raw, _, sig = token.rpartition(".")
    if len(raw) > 40000 or not raw:
        return None
    if not hmac.compare_digest(
            sig,
            hmac.new(_STATE_KEY, raw.encode("ascii"), hashlib.sha256).hexdigest()):
        return None
    try:
        pad = "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw + pad).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    for key, wanted in expect.items():
        if payload.get(key) != wanted:
            return None
    return payload


# ------------------------------------------------------------------ 结果构造
def complete(result, ttl_ms=None, scope=None):
    """注入 resultType:complete；可选注入缓存提示。"""
    out = dict(result)
    out.setdefault("resultType", "complete")
    if ttl_ms is not None:
        out["ttlMs"] = ttl_ms
    if scope is not None:
        out["cacheScope"] = scope
    return out


def input_required(input_requests=None, request_state=None):
    """MRTR 中间结果：resultType=input_required，二者至少其一。"""
    out = {"resultType": "input_required"}
    if input_requests:
        out["inputRequests"] = input_requests
    if request_state:
        out["requestState"] = request_state
    return out


# ------------------------------------------------------------------ _meta 校验
def extract_meta(params):
    """抽取并校验请求 _meta。返回 (meta, None) 或 (None, 错误消息)。"""
    if not isinstance(params, dict):
        return None, "params 必须是对象"
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None, "缺少必填 params._meta（io.modelcontextprotocol/* 元数据）"
    if not isinstance(meta.get(META_VERSION), str):
        return None, "params._meta 缺少必填字段 io.modelcontextprotocol/protocolVersion"
    if not isinstance(meta.get(META_CLIENT_CAPS), dict):
        return None, ("params._meta 缺少必填字段 "
                      "io.modelcontextprotocol/clientCapabilities")
    return meta, None


def unsupported_version_error(requested):
    return {
        "code": ERR_UNSUPPORTED_VERSION,
        "message": "Unsupported protocol version",
        "data": {"supported": list(SUPPORTED_VERSIONS), "requested": requested},
    }


# ------------------------------------------------------------------ 审批策略资源
POLICY_URI = "phocinae://policy/gate-approval"
POLICY_NAME = "gate-approval-policy"
POLICY_MIME = "text/markdown"
POLICY_DESCRIPTION = ("斑海豹 gate 审批策略：判定链路（L0/L1/fail-closed）、"
                      "阈值参数、allow/deny/ask 语义与集成方义务。")
POLICY_TEXT = (
    "# 斑海豹 gate 审批策略（phocinae-mcp）\n"
    "\n"
    "本文件描述 `gate` 工具的审批语义，供宿主代理与集成方参照实现人工确认流程。\n"
    "\n"
    "## 判定链路\n"
    "\n"
    "1. **L0 确定性表**：本地白名单/黑名单表（phocinae-guard 的 l0_table.json），\n"
    "   离线、毫秒级；命中即返回 allow / deny。\n"
    "2. **L1 模型裁决**（可选，默认关闭）：灰区命令交斑海豹 `/v1/systemone` 两问\n"
    "   —— noul（\"This command requires human review.\"）放行概率 + score（风险 2–10）。\n"
    "   阈值：noul >= 0.65 判需人工；score >= 7 → deny，>= 4 → ask，否则 allow；\n"
    "   通道合成取最严格档。\n"
    "3. **fail-closed**：服务不可用、超时、响应异常、字段缺失/越界 → decision=deny。\n"
    "\n"
    "## 结果语义\n"
    "\n"
    "- `allow`：可以执行。\n"
    "- `deny`：禁止执行，且**无任何覆盖通道**（集成方必须强制阻断）。\n"
    "- `ask`：需要人工确认。集成方必须把命令与判定理由呈现给人类，\n"
    "  未获明确批准前不得执行；批准 → allow，拒绝 → deny。\n"
    "  MCP 2026-07-28 客户端上，ask 以 MRTR 请求交互（elicitation/create）呈现；\n"
    "  旧客户端收到带 `decision=ask` 的普通结果，按同样语义人工处理。\n"
    "\n"
    "## 阈值参数（与 phocinae-guard 同口径）\n"
    "\n"
    "- NOUL_THRESHOLD = 0.65；DENY_AT = 7.0；ASK_AT = 4.0；fail_closed = \"deny\"。\n"
    "\n"
    "## L1 默认关闭\n"
    "\n"
    "斑海豹 150M 权重尚未完成命令审批域校准，`gate` 默认只用本地 L0 表；\n"
    "`--l1-enabled` 是显式选择的实验开关。\n"
    "\n"
    "## 部署边界\n"
    "\n"
    "纯本地部署：不提供公网服务；gate 判定结果不构成任何法律责任承诺；\n"
    "集成方应保留审计日志（guard 侧自带 JSONL 审计）。\n"
)
