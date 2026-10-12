#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""http_transport.py —— Streamable HTTP 传输（MCP 2026-07-28）。

- 仅回环绑定（默认 127.0.0.1），POST /mcp；
- 请求头校验（Server Validation，2026-07-28）：
    * MCP-Protocol-Version 必带、须受支持，且与 body _meta 版本一致；
      缺失/不一致 → 400 -32020（HeaderMismatch）；不支持 → 400 -32022；
    * Mcp-Method 必带且等于 body.method；tools/call、resources/read 必带
      Mcp-Name 且等于 body 的 name/uri；违者 → 400 -32020；
    * Accept 未列出 application/json / text/event-stream → 406；
- 通知 → 202 Accepted（空 body）；请求 → 200 application/json 单对象；
- 未知方法 → 404 + -32601；GET/DELETE → 405（2026-07-28 已移除 GET 流与
  会话终结端点）；无会话头（Mcp-Session-Id）、无 SSE 恢复缓冲；
- 日志只走 stderr（protocol.log）。
纯标准库（http.server.ThreadingHTTPServer，daemon 线程）。
"""

import json
import signal
import socket
import sys

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .protocol import (SUPPORTED_VERSIONS, META_VERSION,
                       ERR_PARSE, ERR_INVALID_REQUEST, ERR_METHOD_NOT_FOUND,
                       ERR_INTERNAL, ERR_HEADER_MISMATCH,
                       unsupported_version_error, log)

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
ENDPOINT_PATHS = ("", "/", "/mcp")

# ThreadingHTTPServer 继承的 address_family 默认是 AF_INET（IPv4）。把 `::1` 交给它，
# socket 会拿 IPv6 字面量去做 IPv4 解析，直接 `socket.gaierror: getaddrinfo failed`。
# 文档从 0.2.0 起就把 `::1` 列为允许的回环地址，但**从来没有测试验过这条**，
# 所以这个不一致一直没人发现（实测发布到 PyPI 的 0.2.4：`--host ::1` 以 exit 1 崩溃，
# 而不是开始服务）。这里按主机字面量选地址族，把文档兑现。
def _address_family_for(host):
    """按 host 字面量选 socket 地址族；只有显式 IPv6 才用 AF_INET6。

    刻意**不**用 getaddrinfo 的结果来猜：`localhost` 同时解析出 v4/v6，用 v6 绑定会
    改变 `http://localhost:port` 的可达性。只把字面量 `::1` 当作 v6，其它保持 v4 ——
    改动面最小，且不影响已经工作的两个地址。
    """
    return socket.AF_INET6 if ":" in host else socket.AF_INET


class _IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    """地址族为 IPv6 的 ThreadingHTTPServer。

    `server_bind` 会调 `socket.getfqdn(host)`，对 IPv6 字面量可能很慢；这里保持
    默认实现即可（实测 `::1` 绑定在 12 秒内完成）。存在的唯一理由是换掉
    `address_family` —— 它必须在**实例化时**就正确。
    """
    address_family = socket.AF_INET6


_METHOD_HEADER = "Mcp-Method"
_NAME_HEADER = "Mcp-Name"
_VERSION_HEADER = "MCP-Protocol-Version"
_ACCEPT_HEADER = "Accept"

# 需 Mcp-Name 头的方法 → body params 取值键
_NAME_SOURCE = {"tools/call": "name", "resources/read": "uri",
                "prompts/get": "name"}


def _json_error(code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": None, "error": err}


def _status_for(response):
    """由 JSON-RPC 响应决定 HTTP 状态码（2026-07-28 传输约定）。"""
    error = response.get("error") if isinstance(response, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if code == ERR_METHOD_NOT_FOUND:
        return 404
    if code in (ERR_HEADER_MISMATCH, -32022, ERR_PARSE, -32600, -32602):
        return 400
    if code == ERR_INTERNAL:
        return 500
    return 200


def make_handler(mcp_server):
    """绑定 McpServer 实例的 HTTP 处理器（无会话状态，可并发复用）。"""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "phocinae-mcp/" + __version__
        sys_version = ""
        mcp = mcp_server

        # ---------------- 基础设施
        def log_message(self, fmt, *args):
            if args and args[0].startswith(("4", "5")):
                log("warning", "HTTP %s" % (fmt % args))

        def _send_json(self, status, obj):
            body = json.dumps(obj, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _send_empty(self, status):
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _read_body(self):
            raw = self.headers.get("Content-Length")
            if raw is None:
                self._send_json(400, _json_error(
                    ERR_INVALID_REQUEST, "Missing Content-Length header"))
                return None
            try:
                length = int(raw)
            except ValueError:
                self._send_json(400, _json_error(
                    ERR_INVALID_REQUEST, "Bad Content-Length header"))
                return None
            if length < 0 or length > 10 * 1024 * 1024:
                self._send_json(400, _json_error(
                    ERR_INVALID_REQUEST, "Content-Length out of range"))
                return None
            body = self.rfile.read(length)
            if len(body) != length:
                self._send_json(400, _json_error(
                    ERR_INVALID_REQUEST, "Short body (%d/%d bytes)"
                                         % (len(body), length)))
                return None
            return body

        def _accept_ok(self):
            accept = self.headers.get(_ACCEPT_HEADER, "")
            return ("application/json" in accept
                    or "text/event-stream" in accept)

        # ---------------- 头部校验（Server Validation）
        def _validate_headers(self, msg):
            """返回 None=通过；否则已写出 400/406 响应并返回该响应。"""
            if not self._accept_ok():
                return self._send_json(406, _json_error(
                    ERR_INVALID_REQUEST,
                    "Accept header must include application/json "
                    "and text/event-stream"))
            version = self.headers.get(_VERSION_HEADER)
            if not version:
                return self._send_json(400, _json_error(
                    ERR_HEADER_MISMATCH,
                    "Missing required %s header" % _VERSION_HEADER))
            if version not in SUPPORTED_VERSIONS:
                return self._send_json(
                    400, {"jsonrpc": "2.0", "id": None,
                          "error": unsupported_version_error(version)})
            if not isinstance(msg, dict):
                return None
            method = msg.get("method")
            params = msg.get("params")
            meta = params.get("_meta") if isinstance(params, dict) else None
            body_version = meta.get(META_VERSION) if isinstance(meta, dict) \
                else None
            if body_version and body_version != version:
                return self._send_json(400, _json_error(
                    ERR_HEADER_MISMATCH,
                    "%s header does not match body _meta" % _VERSION_HEADER))
            header_method = self.headers.get(_METHOD_HEADER)
            if not header_method:
                return self._send_json(400, _json_error(
                    ERR_HEADER_MISMATCH,
                    "Missing required %s header" % _METHOD_HEADER))
            if header_method != method:
                return self._send_json(400, _json_error(
                    ERR_HEADER_MISMATCH,
                    "%s header %r does not match body method %r"
                    % (_METHOD_HEADER, header_method, method)))
            src = _NAME_SOURCE.get(method)
            if src:
                header_name = self.headers.get(_NAME_HEADER)
                body_name = params.get(src) if isinstance(params, dict) \
                    else None
                if not header_name:
                    return self._send_json(400, _json_error(
                        ERR_HEADER_MISMATCH,
                        "Missing required %s header for %s"
                        % (_NAME_HEADER, method)))
                if str(body_name) != header_name:
                    return self._send_json(400, _json_error(
                        ERR_HEADER_MISMATCH,
                        "%s header %r does not match body %s %r"
                        % (_NAME_HEADER, header_name, src, body_name)))
            return None

        # ---------------- HTTP 动词
        def do_POST(self):
            if self.path.rstrip("/") not in ENDPOINT_PATHS:
                return self._send_json(404, _json_error(
                    ERR_METHOD_NOT_FOUND,
                    "Not found: %r（MCP 端点为 /mcp）" % self.path))
            body = self._read_body()
            if body is None:
                return
            try:
                msg = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                return self._send_json(400, _json_error(
                    ERR_PARSE, "Parse error: %s" % exc))
            if isinstance(msg, list):
                return self._send_json(400, _json_error(
                    ERR_INVALID_REQUEST,
                    "Invalid Request: batch not supported"))
            if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
                return self._send_json(400, _json_error(
                    ERR_INVALID_REQUEST,
                    "Invalid Request: not a JSON-RPC 2.0 message"))
            if self._validate_headers(msg):
                return
            if "id" not in msg:  # 通知 → 202 Accepted（空 body）
                return self._send_empty(202)
            response = self.mcp.handle(msg)
            if response is None:
                return self._send_empty(202)
            return self._send_json(_status_for(response), response)

        def do_GET(self):
            self._reject_other_methods()

        def do_DELETE(self):
            self._reject_other_methods()

        def do_OPTIONS(self):
            self._reject_other_methods()

        def _reject_other_methods(self):
            # 2026-07-28：GET 流与会话终结端点已移除；本端点为 POST-only
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


def serve_http(mcp_server, host="127.0.0.1", port=8255):
    """在 127.0.0.1 启动 Streamable HTTP 服务（阻塞；SIGTERM/^C 优雅退出）。"""
    if host not in LOOPBACK_HOSTS:
        log("error", "拒绝绑定非回环地址 %r（本服务仅允许本机访问）" % host)
        raise SystemExit(2)
    # 按 host 选地址族。ThreadingHTTPServer 的 address_family 是 AF_INET，把 `::1`
    # 交给它会拿 IPv6 字面量做 IPv4 解析 -> `socket.gaierror: getaddrinfo failed`。
    # 地址族由**类属性**决定，所以必须在实例化时就用对的类，事后覆盖 socket 没用。
    factory = (_IPv6ThreadingHTTPServer
               if _address_family_for(host) == socket.AF_INET6
               else ThreadingHTTPServer)
    httpd = factory((host, port), make_handler(mcp_server))
    httpd.daemon_threads = True
    actual_host, actual_port = httpd.server_address[:2]

    def _stop(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    log("info", "Streamable HTTP 监听 http://%s:%d/mcp "
                "（MCP %s；stdio 版 phocinae-mcp 不受影响）"
                % (actual_host, actual_port, SUPPORTED_VERSIONS[0]))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        log("info", "HTTP 服务已停止")

