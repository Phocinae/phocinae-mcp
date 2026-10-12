#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""server.py —— 斑海豹 MCP server 主程序（stdio + Streamable HTTP，双代并存）。

协议：MCP 2026-07-28（现代：无握手、逐请求 _meta）+ 2024-11-05（旧客户端
initialize 握手，兼容保留）。逐请求判定年代：
  - initialize → 旧年代；server/discover、resources/* → 现代年代；
  - 其余方法：params._meta 携带 io.modelcontextprotocol/* 键 → 现代，否则旧。

现代面（2026-07-28 对齐）：
  - server/discover 必实现（supportedVersions / capabilities / serverInfo）；
  - _meta 必填校验：protocolVersion + clientCapabilities，缺失 → -32602；
    版本不支持 → -32022（data.supported / data.requested）；
  - 一切结果带 resultType；缓存型结果（discover/tools/list/resources/*）
    带 ttlMs + cacheScope；
  - gate 的 ask → MRTR 交互（elicitation/create + HMAC requestState，
    见 protocol.encode_state/decode_state）；
  - 已移除方法（ping / shutdown / logging/setLevel / subscriptions/listen
    未声明能力）→ -32601；
  - resources/list + resources/read 暴露审批策略文本。

旧年代（2024-11-05）：行为与原版 0.1.x 一致（initialize 握手、tools/*）。

stdout 纯净性：stdout 只输出 JSON-RPC 帧；一切日志走 stderr
（protocol.log）。纯标准库（Python >= 3.8）。
"""

import argparse
import json
import os
import sys

from . import __version__
from .backend import (SystemoneClient, SystemoneError, DEFAULT_SERVER,
                      DEFAULT_MODEL)
from .guard_bridge import GuardBridge
from .protocol import (MODERN_VERSION, LEGACY_VERSION, SUPPORTED_VERSIONS,
                       META_PREFIX, META_CLIENT_CAPS,
                       ERR_PARSE, ERR_INVALID_REQUEST, ERR_METHOD_NOT_FOUND,
                       ERR_INVALID_PARAMS, ERR_INTERNAL,
                       TTL_TOOLS_MS, TTL_DISCOVER_MS, TTL_POLICY_MS,
                       CACHE_SCOPE_PUBLIC, POLICY_URI, POLICY_NAME,
                       POLICY_MIME, POLICY_DESCRIPTION, POLICY_TEXT,
                       complete, input_required, extract_meta,
                       unsupported_version_error, encode_state, decode_state,
                       log)
from .tools import TOOL_DEFS, TOOL_MAP, ToolContext, ToolError, McpError

SERVER_NAME = "phocinae-mcp"
LEGACY_PROTOCOL_VERSION = LEGACY_VERSION
SUPPORTED_LEGACY_VERSIONS = (LEGACY_VERSION,)

ENV_SERVER = "PHOCINAE_MCP_SERVER"
ENV_TIMEOUT = "PHOCINAE_MCP_TIMEOUT"
ENV_GUARD_PATH = "PHOCINAE_MCP_GUARD_PATH"
ENV_L1 = "PHOCINAE_MCP_L1_ENABLED"
ENV_MODEL = "PHOCINAE_MCP_MODEL"
ENV_TRANSPORT = "PHOCINAE_MCP_TRANSPORT"
ENV_HTTP_PORT = "PHOCINAE_MCP_HTTP_PORT"

LEGACY_INSTRUCTIONS = (
    "斑海豹审批/推理工具集：gate 判定 shell 命令（allow/deny/ask，"
    "服务不可用时 fail-closed 返回 deny+说明）；classify/route/score "
    "调 /v1/systemone 推理，服务不可用时返回明确错误。")
MODERN_INSTRUCTIONS = (
    "斑海豹审批/推理工具集（MCP 2026-07-28）。何时使用：需要命令级审批"
    "（gate）、意图分类（classify）、工具选择（route）、按标准打分（score）时。"
    "何时不使用：无斑海豹 P0 服务（http://127.0.0.1:8155）时不依赖推理类工具"
    "（gate 仍可用 L0 表/fail-closed）；gate 返回 ask 时必须把命令呈现给人工"
    "确认（elicitation 交互），未批准不得执行；审批策略全文见资源 "
    "phocinae://policy/gate-approval。")


# ------------------------------------------------------------------ 帧读写
def read_frame(stream):
    """读一帧 JSON-RPC 消息（换行分隔；兼容 LSP Content-Length 头帧）。"""
    first = stream.readline()
    if not first:
        return None
    first = first.strip()
    if not first:  # 跳过空行
        return read_frame(stream)
    if first.lower().startswith(b"content-length:"):
        try:
            length = int(first.split(b":", 1)[1].strip())
        except (IndexError, ValueError):
            return {"__parse_error__": "bad Content-Length header"}
        while True:
            line = stream.readline()
            if not line:
                return {"__parse_error__": "unexpected EOF in headers"}
            if line.strip() == b"":
                break
        body = stream.read(length)
        if len(body) != length:
            return {"__parse_error__": "short body (%d/%d bytes)"
                                        % (len(body), length)}
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError as exc:
            return {"__parse_error__": str(exc)}
    try:
        return json.loads(first.decode("utf-8"))
    except ValueError as exc:
        return {"__parse_error__": str(exc)}


def _force_utf8_streams():
    """把标准流固定为 UTF-8 + LF。

    MCP 帧必须是 UTF-8；文本模式 stdout 会用 locale 编码（Windows 上常是
    cp936），中文因此被编成 GBK，客户端严格 UTF-8 解码即失败。
    这里双保险：reconfigure（可用时）＋ 帧一律走 buffer 写字节。
    """
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", newline="\n")
        except (AttributeError, ValueError, OSError):
            pass


def _write_stdout_bytes(data):
    """把字节写到 stdout；没有 .buffer 时退回 os.write(1, ...)。"""
    buf = getattr(sys.stdout, "buffer", None)
    if buf is not None:
        buf.write(data)
        buf.flush()
    else:  # pragma: no cover - 极少数被替换过的 stdout
        import os
        os.write(1, data)


def write_frame(obj):
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    # 帧一律以 UTF-8 字节写出：与 locale 无关，且行尾固定 LF。
    _write_stdout_bytes(raw.encode("utf-8") + b"\n")


def make_response(request_id, result=None, error=None):
    resp = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result
    return resp


def rpc_error(code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return err


def _unknown_method(method, modern):
    """-32601；现代年代附 supported 版本提示（帮助旧客户端显示诊断）。"""
    data = None
    if modern:
        data = {"supported": list(SUPPORTED_VERSIONS)}
    return rpc_error(ERR_METHOD_NOT_FOUND, "Method not found: %r" % (method,),
                     data=data)


# ------------------------------------------------------------------ 请求处理
class McpServer:
    def __init__(self, server, timeout, guard_path, l1_enabled, model):
        self.client = SystemoneClient(server, timeout, model)
        self.bridge = GuardBridge(guard_path)
        self.ctx = ToolContext(self.client, self.bridge, server, timeout,
                               l1_enabled)
        self.server = server
        self.initialized = False  # 仅旧年代握手使用

    # ------------------------------ 年代判定
    @staticmethod
    def _era_of(msg):
        method = msg.get("method")
        params = msg.get("params")
        if method == "initialize":
            return "legacy"
        if method in ("server/discover", "resources/list", "resources/read"):
            return "modern"
        if isinstance(params, dict) and isinstance(params.get("_meta"), dict):
            if any(k.startswith(META_PREFIX) for k in params["_meta"]):
                return "modern"
        return "legacy"

    def handle(self, msg):
        """处理一条已解析消息；返回响应 dict 或 None（通知/无需响应）。"""
        if isinstance(msg, dict) and "__parse_error__" in msg:
            return make_response(
                None, error=rpc_error(ERR_PARSE,
                                      "Parse error: %s" % msg["__parse_error__"]))
        if isinstance(msg, list):
            return make_response(
                None, error=rpc_error(ERR_INVALID_REQUEST,
                                      "Invalid Request: batch not supported"))
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return make_response(
                msg.get("id") if isinstance(msg, dict) else None,
                error=rpc_error(ERR_INVALID_REQUEST,
                                "Invalid Request: not a JSON-RPC 2.0 message"))
        method = msg.get("method")
        request_id = msg.get("id")
        is_notification = "id" not in msg
        params = msg.get("params")
        if params is None:
            params = {}
        era = self._era_of(msg)

        if era == "modern":
            return self._handle_modern(msg, method, request_id,
                                       is_notification, params)
        return self._handle_legacy(msg, method, request_id,
                                   is_notification, params)

    # ------------------------------ 现代年代（2026-07-28）
    def _handle_modern(self, msg, method, request_id, is_notification, params):
        meta = None
        if is_notification:
            # 通知从宽：_meta 缺省时不校验（cancelled 等通知无版本语义）；
            # 携带 _meta 则校验其形状。
            if isinstance(params, dict) and "_meta" in params:
                meta, err = extract_meta(params)
                if err:
                    return make_response(
                        request_id, error=rpc_error(ERR_INVALID_PARAMS, err))
        else:
            meta, err = extract_meta(params)
            if err:
                return make_response(
                    request_id, error=rpc_error(ERR_INVALID_PARAMS, err))
            version = meta[META_PREFIX + "protocolVersion"]
            if version not in SUPPORTED_VERSIONS:
                return make_response(
                    request_id,
                    error=unsupported_version_error(version))
        try:
            result = self._dispatch_modern(method, params, meta)
        except McpError as exc:
            return make_response(request_id,
                                 error=rpc_error(exc.code, exc.message))
        except (Exception, SystemExit) as exc:
            log("error", "现代请求 %s 处理异常：%s: %s"
                         % (method, type(exc).__name__, exc))
            return make_response(
                request_id, error=rpc_error(ERR_INTERNAL, "Internal error"))
        if result is _NO_RESPONSE:
            return None
        return make_response(request_id, result=result)

    def _dispatch_modern(self, method, params, meta):
        if method == "server/discover":
            return complete(
                {
                    "supportedVersions": list(SUPPORTED_VERSIONS),
                    "capabilities": {"tools": {}, "resources": {}},
                    "_meta": {META_PREFIX + "serverInfo":
                              {"name": SERVER_NAME, "version": __version__}},
                    "instructions": MODERN_INSTRUCTIONS,
                },
                ttl_ms=TTL_DISCOVER_MS, scope=CACHE_SCOPE_PUBLIC)
        if method == "tools/list":
            return complete({"tools": TOOL_DEFS},
                            ttl_ms=TTL_TOOLS_MS, scope=CACHE_SCOPE_PUBLIC)
        if method == "tools/call":
            return self.call_tool(params, meta, modern=True)
        if method == "resources/list":
            return complete(
                {"resources": [{"uri": POLICY_URI, "name": POLICY_NAME,
                                "description": POLICY_DESCRIPTION,
                                "mimeType": POLICY_MIME}]},
                ttl_ms=TTL_POLICY_MS, scope=CACHE_SCOPE_PUBLIC)
        if method == "resources/read":
            return self._resources_read(params)
        if method in ("notifications/cancelled", "notifications/progress",
                      "notifications/initialized"):
            return _NO_RESPONSE
        raise McpError(ERR_METHOD_NOT_FOUND,
                       "Method not found: %r（supportedVersions=%s）"
                       % (method, list(SUPPORTED_VERSIONS)))

    def _resources_read(self, params):
        if not isinstance(params, dict):
            raise McpError(ERR_INVALID_PARAMS,
                           "resources/read 的 params 必须是对象")
        uri = params.get("uri")
        if uri != POLICY_URI:
            raise McpError(ERR_INVALID_PARAMS,
                           "资源不存在：%r（可用：%s）" % (uri, POLICY_URI))
        return complete(
            {"contents": [{"uri": POLICY_URI, "mimeType": POLICY_MIME,
                           "text": POLICY_TEXT}]},
            ttl_ms=TTL_POLICY_MS, scope=CACHE_SCOPE_PUBLIC)

    # ------------------------------ 旧年代（2024-11-05，兼容原版行为）
    def _handle_legacy(self, msg, method, request_id, is_notification, params):
        if method == "initialize":
            requested = (params.get("protocolVersion")
                         if isinstance(params, dict) else None)
            negotiated = (requested if requested in SUPPORTED_LEGACY_VERSIONS
                          else LEGACY_PROTOCOL_VERSION)
            return make_response(request_id, result={
                "protocolVersion": negotiated,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": LEGACY_INSTRUCTIONS,
            })
        if method in ("notifications/initialized", "notifications/cancelled",
                      "notifications/progress"):
            if method == "notifications/initialized":
                self.initialized = True
            return None
        if method == "ping":
            return make_response(request_id, result={})
        if method == "tools/list":
            return make_response(request_id, result={"tools": TOOL_DEFS})
        if method == "tools/call":
            try:
                result = self.call_tool(params, None, modern=False)
            except McpError as exc:
                return make_response(request_id,
                                     error=rpc_error(exc.code, exc.message))
            return make_response(request_id, result=result)
        if method == "shutdown":
            return make_response(request_id, result=None)
        if is_notification:
            return None
        return make_response(
            request_id, error=_unknown_method(method, modern=False))

    # ------------------------------ tools/call（双代共用核心）
    def call_tool(self, params, meta, modern):
        if not isinstance(params, dict):
            raise McpError(ERR_INVALID_PARAMS, "tools/call 的 params 必须是对象")
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOL_MAP:
            raise McpError(ERR_INVALID_PARAMS, "未知工具：%r（可用：%s）"
                                               % (name, "、".join(sorted(TOOL_MAP))))
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise McpError(ERR_INVALID_PARAMS, "工具参数 arguments 必须是对象")

        # MRTR 重试路径：参数携带 requestState（gate 的 ask 人工确认回执）。
        # 放在 try 内：重试期重新判定若遇服务不可用，同样走 isError 兜底。
        if params.get("requestState") is not None:
            try:
                return self._gate_mrtr_retry(name, params, meta, modern)
            except ToolError as exc:
                return self._tool_result(name, error=str(exc), modern=modern)
            except SystemoneError as exc:
                return self._tool_result(name, error=str(exc), modern=modern)

        try:
            payload = TOOL_MAP[name](self.ctx, arguments)
        except ToolError as exc:
            return self._tool_result(name, error=str(exc), modern=modern)
        except SystemoneError as exc:
            return self._tool_result(name, error=str(exc), modern=modern)
        except McpError:  # JSON-RPC 层错误向上抛
            raise
        except (Exception, SystemExit) as exc:  # 兜底：工具内部未知异常不静默
            log("error", "工具 %s 执行异常：%s: %s"
                         % (name, type(exc).__name__, exc))
            return self._tool_result(
                name,
                error="工具执行异常（%s: %s）" % (type(exc).__name__, exc),
                modern=modern)

        if modern and name == "gate" and payload.get("decision") == "ask":
            caps = (meta or {}).get(META_CLIENT_CAPS) or {}
            if isinstance(caps, dict) and caps.get("elicitation") is not None:
                return self._gate_ask_input_required(payload)
        return self._tool_result(name, payload=payload, modern=modern)

    # ------------------------------ gate ask → MRTR 交互
    def _gate_ask_input_required(self, payload):
        state = encode_state({"tool": "gate",
                              "command": payload.get("command"),
                              "orig": payload})
        return input_required(
            input_requests={
                "gate_confirmation": {
                    "method": "elicitation/create",
                    "params": {
                        "mode": "form",
                        "message": ("gate 判定该命令需要人工复核：%s —— "
                                    "是否批准执行？"
                                    % payload.get("command", "")),
                        "requestedSchema": {
                            "type": "object",
                            "properties": {
                                "decision": {"type": "string",
                                             "enum": ["approve", "reject"]},
                            },
                            "required": ["decision"],
                            "additionalProperties": False,
                        },
                    },
                },
            },
            request_state=state)

    def _gate_mrtr_retry(self, name, params, meta, modern):
        """处理 gate ask 的人工确认回执。requestState 校验失败 → 重发交互
        （MRTR：不应报错，而是再次请求缺失信息）。"""
        if name != "gate":
            raise McpError(ERR_INVALID_PARAMS,
                           "requestState 仅用于 gate 的 ask 人工确认重试")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}
        command = arguments.get("command")
        state = decode_state(params.get("requestState"),
                             {"tool": "gate", "command": command})
        if state is None:
            # 状态无效/过期/指纹不符 → 重新判定并重发交互
            payload = TOOL_MAP["gate"](self.ctx, arguments)
            if payload.get("decision") == "ask":
                return self._gate_ask_input_required(payload)
            return self._tool_result("gate", payload=payload, modern=modern)
        responses = params.get("inputResponses")
        if not isinstance(responses, dict):
            return self._gate_ask_input_required(state["orig"])
        conf = responses.get("gate_confirmation")
        action = conf.get("action") if isinstance(conf, dict) else None
        if action not in ("accept", "decline", "cancel"):
            return self._gate_ask_input_required(state["orig"])
        final = dict(state["orig"])
        final["human_action"] = ("approve" if action == "accept"
                                 else ("reject" if action == "decline"
                                       else "cancel"))
        final["layer"] = "human_approval"
        if action == "accept":
            final["decision"] = "allow"
            final["reason"] = (final.get("reason", "") +
                               " → 人工批准（elicitation: accept）")
        elif action == "decline":
            final["decision"] = "deny"
            final["reason"] = (final.get("reason", "") +
                               " → 人工拒绝（elicitation: decline）")
        else:
            final["status"] = "cancelled"
            final["reason"] = (final.get("reason", "") +
                               " → 人工取消（elicitation: cancel），不授权执行")
        return self._tool_result("gate", payload=final, modern=modern)

    # ------------------------------ 结果封装
    def _tool_result(self, name, payload=None, error=None, modern=True):
        body = payload if payload is not None else \
            {"tool": name, "error": error or "未知错误"}
        text = json.dumps(body, ensure_ascii=False, indent=2)
        result = {
            "content": [{"type": "text", "text": text}],
            "isError": error is not None,
        }
        if modern and payload is not None:
            result["structuredContent"] = payload
        if modern:
            return complete(result)
        return result

    # ------------------------------ stdio 主循环
    def run(self):
        log("info",
            "started v%s（server=%s, guard=%s, l1_enabled=%s, "
            "modern=%s, legacy=%s）"
            % (__version__, self.server,
               self.bridge.path or "未找到→纯HTTP降级", self.ctx.l1_enabled,
               SUPPORTED_VERSIONS[0], LEGACY_PROTOCOL_VERSION))
        stdin = sys.stdin.buffer
        while True:
            frame = read_frame(stdin)
            if frame is None:
                break  # 客户端关闭 stdin → 正常退出
            try:
                response = self.handle(frame)
            except McpError as exc:
                rid = frame.get("id") if isinstance(frame, dict) else None
                response = make_response(rid,
                                         error=rpc_error(exc.code, exc.message))
            except (Exception, SystemExit) as exc:
                log("error", "未捕获异常（%s: %s），继续服务"
                             % (type(exc).__name__, exc))
                rid = frame.get("id") if isinstance(frame, dict) else None
                response = make_response(rid,
                                         error=rpc_error(ERR_INTERNAL,
                                                         "Internal error"))
            if response is not None:
                write_frame(response)


_NO_RESPONSE = object()  # 通知哨兵（区别于合法结果 None/{}）


# ------------------------------------------------------------------ CLI
def build_parser():
    parser = argparse.ArgumentParser(
        prog="phocinae-mcp",
        description=("斑海豹 MCP server：gate/classify/route/score 四工具；"
                     "stdio（默认）或 Streamable HTTP 传输"),
    )
    parser.add_argument("--server", default=None,
                        help="斑海豹 P0 服务基地址（默认 %s；env %s 覆盖）"
                             % (DEFAULT_SERVER, ENV_SERVER))
    parser.add_argument("--timeout", type=float, default=None,
                        help="服务请求超时秒数（默认 5.0；env %s）" % ENV_TIMEOUT)
    parser.add_argument("--guard", default=None, dest="guard_path",
                        help="guard.py 路径（默认自动探测同级 ../phocinae-guard/"
                             "guard.py；env %s）" % ENV_GUARD_PATH)
    parser.add_argument("--l1-enabled", action="store_true", default=None,
                        help="启用 L1：灰区命令交 /v1/systemone 裁决（env %s）"
                             % ENV_L1)
    parser.add_argument("--model", default=None,
                        help="模型名（默认 %s；env %s）" % (DEFAULT_MODEL, ENV_MODEL))
    parser.add_argument("--transport", choices=("stdio", "http"), default=None,
                        help="传输方式：stdio（默认）或 http（env %s）"
                             % ENV_TRANSPORT)
    parser.add_argument("--host", default=None,
                        help="HTTP 监听地址（默认 127.0.0.1；仅允许回环地址）")
    parser.add_argument("--port", type=int, default=None,
                        help="HTTP 监听端口（默认 8255；0=随机端口；env %s）"
                             % ENV_HTTP_PORT)
    parser.add_argument("--version", action="store_true", help="打印版本")
    return parser


def _env_bool(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def main(argv=None):
    # 固定标准流为 UTF-8：帧走 buffer 写字节已与 locale 无关，这里再兜住
    # stderr（日志含中文）与 stdin，避免任何路径回落到 locale 编码。
    _force_utf8_streams()
    args = build_parser().parse_args(argv)
    if args.version:
        _write_stdout_bytes(("phocinae-mcp %s\n" % __version__).encode("utf-8"))
        return 0
    server = (args.server or os.environ.get(ENV_SERVER)
              or os.environ.get("PHOCINAE_GUARD_SERVER") or DEFAULT_SERVER)
    timeout = args.timeout if args.timeout is not None else float(
        os.environ.get(ENV_TIMEOUT, "5"))
    l1_enabled = (args.l1_enabled if args.l1_enabled is not None
                  else _env_bool(ENV_L1))
    model = args.model or os.environ.get(ENV_MODEL) or DEFAULT_MODEL
    mcp = McpServer(server, timeout, args.guard_path, l1_enabled, model)

    transport = args.transport or os.environ.get(ENV_TRANSPORT) or "stdio"
    if transport == "http":
        from .http_transport import serve_http
        host = args.host or "127.0.0.1"
        port = args.port
        if port is None:
            port = int(os.environ.get(ENV_HTTP_PORT, "8255"))
        serve_http(mcp, host=host, port=port)
    else:
        mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

