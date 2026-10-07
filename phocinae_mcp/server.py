#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""server.py —— phocinae-mcp 主程序：MCP stdio server（手写 JSON-RPC 2.0 协议）。

传输：stdin/stdout 新行分隔 JSON-RPC 2.0 帧（MCP stdio 规范）；stdout 只输出
协议帧，日志一律走 stderr。入站同时兼容 Content-Length 头帧（LSP 风格）。

生命周期：initialize → notifications/initialized → tools/list → tools/call；
另支持 ping / shutdown 与常见通知（cancelled/progress）。

纯标准库（Python >= 3.8），零第三方依赖。
"""

import argparse
import json
import os
import sys

from . import __version__
from .backend import SystemoneClient, SystemoneError, DEFAULT_SERVER, DEFAULT_MODEL
from .guard_bridge import GuardBridge
from .tools import TOOL_DEFS, TOOL_MAP, ToolContext, ToolError, McpError

PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_VERSIONS = (PROTOCOL_VERSION,)
SERVER_NAME = "phocinae-mcp"

ENV_SERVER = "PHOCINAE_MCP_SERVER"
ENV_TIMEOUT = "PHOCINAE_MCP_TIMEOUT"
ENV_GUARD_PATH = "PHOCINAE_MCP_GUARD_PATH"
ENV_L1 = "PHOCINAE_MCP_L1_ENABLED"
ENV_MODEL = "PHOCINAE_MCP_MODEL"


def log(message):
    sys.stderr.write("phocinae-mcp: %s\n" % message)
    sys.stderr.flush()


# ------------------------------------------------------------------ 帧读写
def read_frame(stream):
    """读一帧 JSON-RPC 消息。

    支持两种帧格式：
      - 换行分隔 JSON（MCP stdio 规范，一行一帧）；
      - LSP 风格 Content-Length 头 + 正文。
    返回 None=EOF；返回 {"__parse_error__": msg}=帧解析失败。
    """
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
        while True:  # 消费其余头，直到空行
            line = stream.readline()
            if not line:
                return {"__parse_error__": "unexpected EOF in headers"}
            if line.strip() == b"":
                break
        body = stream.read(length)
        if len(body) != length:
            return {"__parse_error__": "short body (%d/%d bytes)" % (len(body), length)}
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError as exc:
            return {"__parse_error__": str(exc)}
    try:
        return json.loads(first.decode("utf-8"))
    except ValueError as exc:
        return {"__parse_error__": str(exc)}


def write_frame(obj):
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write(raw + "\n")
    sys.stdout.flush()


def make_response(request_id, result=None, error=None):
    resp = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result
    return resp


def rpc_error(code, message):
    return {"code": code, "message": message}


# ------------------------------------------------------------------ 请求处理
class McpServer:
    def __init__(self, server, timeout, guard_path, l1_enabled, model):
        self.client = SystemoneClient(server, timeout, model)
        self.bridge = GuardBridge(guard_path)
        self.ctx = ToolContext(self.client, self.bridge, server, timeout,
                               l1_enabled)
        self.server = server
        self.initialized = False

    def handle(self, msg):
        """处理一条已解析的消息；返回响应 dict 或 None（通知/无效无需响应）。"""
        if isinstance(msg, dict) and "__parse_error__" in msg:
            return make_response(
                None, error=rpc_error(-32700, "Parse error: %s" % msg["__parse_error__"]))
        if isinstance(msg, list):
            return make_response(
                None, error=rpc_error(-32600, "Invalid Request: batch not supported"))
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return make_response(
                msg.get("id") if isinstance(msg, dict) else None,
                error=rpc_error(-32600, "Invalid Request: not a JSON-RPC 2.0 message"))
        method = msg.get("method")
        request_id = msg.get("id")
        is_notification = "id" not in msg
        params = msg.get("params") or {}

        if method == "initialize":
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            negotiated = (requested if requested in SUPPORTED_VERSIONS
                          else PROTOCOL_VERSION)
            return make_response(request_id, result={
                "protocolVersion": negotiated,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": (
                    "斑海豹审批/推理工具集：gate 判定 shell 命令（allow/deny/ask，"
                    "服务不可用时 fail-closed 返回 deny+说明）；classify/route/score "
                    "调 /v1/systemone 推理，服务不可用时返回明确错误。"),
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
                result = self.call_tool(params)
            except McpError as exc:
                return make_response(request_id, error=rpc_error(exc.code, exc.message))
            return make_response(request_id, result=result)
        if method == "shutdown":
            return make_response(request_id, result=None)
        if is_notification:
            return None
        return make_response(
            request_id, error=rpc_error(-32601, "Method not found: %r" % (method,)))

    def call_tool(self, params):
        if not isinstance(params, dict):
            raise McpError(-32602, "tools/call 的 params 必须是对象")
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOL_MAP:
            raise McpError(-32602, "未知工具：%r（可用：%s）"
                                    % (name, "、".join(sorted(TOOL_MAP))))
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise McpError(-32602, "工具参数 arguments 必须是对象")
        try:
            payload = TOOL_MAP[name](self.ctx, arguments)
        except ToolError as exc:
            return {
                "content": [{"type": "text",
                             "text": json.dumps({"tool": name, "error": str(exc)},
                                                ensure_ascii=False, indent=2)}],
                "isError": True,
            }
        except SystemoneError as exc:  # classify/route/score 的服务不可用 → 明确错误
            return {
                "content": [{"type": "text",
                             "text": json.dumps({"tool": name, "error": str(exc)},
                                                ensure_ascii=False, indent=2)}],
                "isError": True,
            }
        except McpError:  # JSON-RPC 层错误向上抛，由 handle() 转成 -32602
            raise
        except (Exception, SystemExit) as exc:  # 兜底：工具内部未知异常也不静默
            return {
                "content": [{"type": "text",
                             "text": json.dumps(
                                 {"tool": name,
                                  "error": "工具执行异常（%s: %s）"
                                           % (type(exc).__name__, exc)},
                                 ensure_ascii=False, indent=2)}],
                "isError": True,
            }
        return {
            "content": [{"type": "text",
                         "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
            "isError": False,
        }

    def run(self):
        log("started v%s（server=%s, guard=%s, l1_enabled=%s）"
            % (__version__, self.server,
               self.bridge.path or "未找到→纯HTTP降级", self.ctx.l1_enabled))
        stdin = sys.stdin.buffer
        while True:
            frame = read_frame(stdin)
            if frame is None:
                break  # 客户端关闭 stdin → 正常退出
            response = self.handle(frame)
            if response is not None:
                write_frame(response)


# ------------------------------------------------------------------ CLI
def build_parser():
    parser = argparse.ArgumentParser(
        prog="phocinae-mcp",
        description="斑海豹 MCP stdio server：gate/classify/route/score 四工具",
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
    parser.add_argument("--version", action="store_true", help="打印版本")
    return parser


def _env_bool(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.version:
        print("phocinae-mcp %s" % __version__)
        return 0
    server = (args.server or os.environ.get(ENV_SERVER)
              or os.environ.get("PHOCINAE_GUARD_SERVER") or DEFAULT_SERVER)
    timeout = args.timeout if args.timeout is not None else float(
        os.environ.get(ENV_TIMEOUT, "5"))
    l1_enabled = (args.l1_enabled if args.l1_enabled is not None
                  else _env_bool(ENV_L1))
    model = args.model or os.environ.get(ENV_MODEL) or DEFAULT_MODEL
    mcp = McpServer(server, timeout, args.guard_path, l1_enabled, model)
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
