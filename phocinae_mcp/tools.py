#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools.py —— 四个 MCP 工具：gate / classify / route / score。

fail-closed 语义：
    gate      → 服务不可用 / 判定异常时仍返回成功结果，decision=deny + 原因说明；
    classify / route / score → 服务不可用时返回明确错误（MCP isError:true）。
"""

import json
import os

from . import __version__
from .backend import (SystemoneClient, SystemoneError, map_gate,
                      noul_to_probability, unwrap_choice, unwrap_noul,
                      unwrap_score, NOUL_THRESHOLD, DENY_AT, ASK_AT)

SCORE_RANGE = (2, 10)


class McpError(Exception):
    """JSON-RPC 层错误（-32602 等，由 server.py 转成 error 响应）。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class ToolError(Exception):
    """工具执行错误 → tools/call 返回 isError:true 的文本说明。"""


class ToolContext:
    def __init__(self, client, bridge, server, timeout, l1_enabled):
        self.client = client      # SystemoneClient
        self.bridge = bridge      # GuardBridge（可能不可用 → 降级）
        self.server = server      # 默认服务地址
        self.timeout = timeout
        self.l1_enabled = l1_enabled


# ------------------------------------------------------------------ 参数校验
def _require_str(args, key):
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise McpError(-32602, "工具参数 %r 必须是非空字符串" % key)
    return value.strip()


def _require_str_list(args, key, min_len=1):
    value = args.get(key)
    if (not isinstance(value, list) or len(value) < min_len
            or any(not isinstance(x, str) or not x.strip() for x in value)):
        raise McpError(-32602,
                       "工具参数 %r 必须是长度 >= %d 的非空字符串数组" % (key, min_len))
    return [x.strip() for x in value]


def _opt_server(args, ctx):
    """工具级 --server 覆盖（arg 优先，其次进程级 env/CLI 配置）。"""
    server = args.get("server")
    if isinstance(server, str) and server.strip():
        return server.strip()
    return ctx.server


# ------------------------------------------------------------------ gate
def _gate_via_guard(ctx, command, cwd, server):
    """guard.py 可复用：L0 表 →（l1_enabled 时）L1 两问 → fail-closed deny。"""
    try:
        result = ctx.bridge.decide(command, cwd, server,
                                   ctx.timeout, ctx.l1_enabled)
    except (Exception, SystemExit) as exc:  # guard 内部异常（含 load_config 的
        # _die → SystemExit；配置/表加载失败等）一律 fail-closed，绝不打死 MCP 循环
        return {
            "command": command, "cwd": cwd, "decision": "deny",
            "layer": "fail_closed",
            "reason": ("guard 判定异常（%s: %s）→ fail-closed deny"
                       % (type(exc).__name__, exc)),
            "noul": None, "score": None, "error": str(exc),
        }
    result["engine"] = "guard:" + getattr(ctx.bridge.guard, "VERSION", "?")
    return result


def _gate_via_http(ctx, command, cwd, server):
    """无 guard 时的等价实现：noul + score 两问（P0 协议），阈值同 guard。"""
    state = json.dumps({
        "command": command, "cwd": cwd,
        "tool": "phocinae-mcp/" + __version__,
    }, ensure_ascii=False)
    questions = [
        {"id": "gate_noul", "type": "noul", "threshold": NOUL_THRESHOLD,
         "instructions": "This command requires human review.",
         "criteria": {
             "false": "The command is safe to run without human attention.",
             "true": "A human should inspect this command before it runs."}},
        {"id": "gate_score", "type": "score",
         "instructions": "Assess the risk of the command described in the state."},
    ]
    try:
        data = ctx.client.ask(questions, state, server=server)
    except SystemoneError as exc:
        return {
            "command": command, "cwd": cwd, "decision": "deny",
            "layer": "fail_closed",
            "reason": "%s → fail-closed deny（服务不可用时一律拒绝）" % exc,
            "noul": None, "score": None, "error": str(exc),
        }
    answers = data.get("answers", {}) or {}
    noul_raw = unwrap_noul(answers.get("gate_noul"))
    score_raw = unwrap_score(answers.get("gate_score"))
    if noul_raw is None or score_raw is None:
        return {
            "command": command, "cwd": cwd, "decision": "deny",
            "layer": "fail_closed",
            "reason": ("服务应答缺少 gate_noul / gate_score 字段 → fail-closed deny"),
            "noul": noul_raw, "score": score_raw,
        }
    try:
        noul_p = noul_to_probability(noul_raw)
        score = float(score_raw)
    except (TypeError, ValueError) as exc:
        return {
            "command": command, "cwd": cwd, "decision": "deny",
            "layer": "fail_closed",
            "reason": "服务应答数值异常（%s: %s）→ fail-closed deny"
                     % (type(exc).__name__, exc),
            "noul": noul_raw, "score": score_raw, "error": str(exc),
        }
    decision, noul_state = map_gate(noul_p, score)
    return {
        "command": command, "cwd": cwd, "decision": decision, "layer": "L1",
        "noul": noul_p, "score": score, "noul_state": noul_state,
        "reason": ("L1 noul=%.4f（%s，threshold=%.2f）score=%.4f"
                   "（deny>=%g，ask>=%g）→ %s"
                   % (noul_p, noul_state, NOUL_THRESHOLD, score,
                      DENY_AT, ASK_AT, decision)),
        "model": data.get("model"),
        "confidence": data.get("answer_confidence"),
    }


def gate(ctx, args):
    """gate(command, cwd?, server?) → {decision: allow|deny|ask, layer, reason, ...}"""
    command = _require_str(args, "command")
    cwd = args.get("cwd") if isinstance(args.get("cwd"), str) else ""
    cwd = cwd or os.getcwd()
    server = _opt_server(args, ctx)
    if ctx.bridge is not None and ctx.bridge.available:
        result = _gate_via_guard(ctx, command, cwd, server)
    else:
        result = _gate_via_http(ctx, command, cwd, server)
        result["engine"] = "http:noul+score"
    result.setdefault("engine", "unknown")
    result["server"] = server
    result["fail_closed"] = "deny"
    result["tool"] = "phocinae-mcp/" + __version__
    return result


# ------------------------------------------------------------------ classify / route / score
def _ask_single_choice(ctx, state, options, question_id, instructions, server):
    question = {
        "id": question_id, "type": "choice", "options": options,
        "instructions": instructions,
        "criteria": {str(i): label for i, label in enumerate(options)},
    }
    data = ctx.client.ask([question], state, server=server)  # 失败 → SystemoneError
    answers = data.get("answers", {}) or {}
    raw = unwrap_choice(answers.get(question_id))
    if raw is None:
        raise ToolError("服务应答缺少 %s 字段（answers=%r）" % (question_id, answers))
    try:
        index = int(raw)
    except (TypeError, ValueError) as exc:
        raise ToolError("服务应答 %s 不是整数下标：%r" % (question_id, raw)) from exc
    if not 0 <= index < len(options):
        raise ToolError("服务应答下标越界：%d 不在 [0, %d)（%s）"
                        % (index, len(options), question_id))
    return index, data


def classify(ctx, args):
    """classify(state, labels, server?) → {choice: 下标, label, confidence, ...}"""
    state = _require_str(args, "state")
    labels = _require_str_list(args, "labels")
    server = _opt_server(args, ctx)
    index, data = _ask_single_choice(
        ctx, state, labels, "classify_choice",
        "Choose the single best category for the state described.", server)
    return {
        "question": "classify_choice", "state": state, "labels": labels,
        "choice": index, "label": labels[index],
        "confidence": data.get("answer_confidence"),
        "model": data.get("model"), "usage": data.get("usage"),
        "server": server,
    }


def route(ctx, args):
    """route(state, tools, server?) → {choice: 下标, tool, ...}（工具路由 choice 题）"""
    state = _require_str(args, "state")
    tools = _require_str_list(args, "tools")
    server = _opt_server(args, ctx)
    index, data = _ask_single_choice(
        ctx, state, tools, "route_choice",
        "Choose the single most suitable tool for the request described in the state.",
        server)
    return {
        "question": "route_choice", "state": state, "tools": tools,
        "choice": index, "tool": tools[index],
        "confidence": data.get("answer_confidence"),
        "model": data.get("model"), "usage": data.get("usage"),
        "server": server,
    }


def score(ctx, args):
    """score(state, criteria, server?) → {score: 2–10, ...}"""
    state = _require_str(args, "state")
    criteria = _require_str_list(args, "criteria")
    server = _opt_server(args, ctx)
    question = {
        "id": "score_q", "type": "score",
        "instructions": "Score from %d to %d per the criteria described in the state."
                        % SCORE_RANGE,
        "criteria": {str(i): c for i, c in enumerate(criteria)},
    }
    data = ctx.client.ask([question], state, server=server)  # 失败 → SystemoneError
    answers = data.get("answers", {}) or {}
    raw = unwrap_score(answers.get("score_q"))
    if raw is None:
        raise ToolError("服务应答缺少 score_q 字段（answers=%r）" % answers)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ToolError("服务应答 score_q 不是数值：%r" % raw) from exc
    if not SCORE_RANGE[0] <= value <= SCORE_RANGE[1]:
        raise ToolError("服务应答越界：%.4f 不在 [%d, %d]"
                        % (value, SCORE_RANGE[0], SCORE_RANGE[1]))
    return {
        "question": "score_q", "state": state, "criteria": criteria,
        "score": value, "range": list(SCORE_RANGE),
        "confidence": data.get("answer_confidence"),
        "model": data.get("model"), "usage": data.get("usage"),
        "server": server,
    }


# ------------------------------------------------------------------ MCP tools/list 定义
TOOL_DEFS = [
    {
        "name": "gate",
        "description": ("斑海豹命令审批门：判定一条 shell 命令是否放行。"
                        "L0 确定性表直接判 allow/deny；灰区可交斑海豹模型裁决（L1）。"
                        "服务不可用时 fail-closed：返回 deny 并附原因说明。"
                        "返回 decision ∈ allow|deny|ask 与 layer/reason。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string",
                            "description": "待判定的 shell 命令（必填）"},
                "cwd": {"type": "string",
                        "description": "命令执行工作目录（默认服务器进程当前目录）"},
                "server": {"type": "string",
                           "description": "可选：覆盖服务地址（http://host:port，"
                                          "等价于 --server 参数）"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "classify",
        "description": ("分类：把 state 描述归入 labels 之一（choice 题）。"
                        "返回 choice=选项下标（0 起）、label、置信度。"
                        "服务不可用时返回明确错误。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {"type": "string",
                          "description": "待分类的上下文/状态描述（必填）"},
                "labels": {"type": "array", "items": {"type": "string"},
                           "description": "候选标签列表（必填，>=1 个非空字符串）"},
                "server": {"type": "string",
                           "description": "可选：覆盖服务地址（等价于 --server 参数）"},
            },
            "required": ["state", "labels"],
        },
    },
    {
        "name": "route",
        "description": ("工具路由：从 tools 列表里挑出最合适的一个（choice 题）。"
                        "返回 choice=下标、tool=选中的工具名。"
                        "服务不可用时返回明确错误。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {"type": "string",
                          "description": "请求/上下文描述（必填）"},
                "tools": {"type": "array", "items": {"type": "string"},
                          "description": "候选工具名列表（必填，>=1 个非空字符串）"},
                "server": {"type": "string",
                           "description": "可选：覆盖服务地址（等价于 --server 参数）"},
            },
            "required": ["state", "tools"],
        },
    },
    {
        "name": "score",
        "description": ("打分：按 criteria 对 state 打分，返回 score ∈ [2, 10]。"
                        "服务不可用或应答越界时返回明确错误。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {"type": "string",
                          "description": "待打分对象的状态描述（必填）"},
                "criteria": {"type": "array", "items": {"type": "string"},
                             "description": "打分标准列表（必填，>=1 个非空字符串）"},
                "server": {"type": "string",
                           "description": "可选：覆盖服务地址（等价于 --server 参数）"},
            },
            "required": ["state", "criteria"],
        },
    },
]

TOOL_MAP = {
    "gate": gate,
    "classify": classify,
    "route": route,
    "score": score,
}
