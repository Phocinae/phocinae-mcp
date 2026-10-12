#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_http.py —— Streamable HTTP 传输冒烟测试（MCP 2026-07-28）。

校验：头部强制（MCP-Protocol-Version / Mcp-Method / Mcp-Name / Accept）、
-32020 / -32022 / 404 / 405 / 406 / 202、以及 gate ask 的 MRTR 全链路。
输出：tests/http_smoke.txt；退出码：全过 0。
"""

import json
import os
import re
import subprocess
import tempfile
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, PKG_ROOT)

from mock_systemone import MockSystemone  # noqa: E402


def _force_utf8_stdio():
    """把 stdout/stderr 固定为 UTF-8。

    本脚本会打印中文诊断；Windows 上 Python 默认用控制台代码页
    （GitHub Actions runner 是 cp1252/437，本机可能是 cp936），
    非 ASCII 直接抛 UnicodeEncodeError 让整个步骤失败。
    这里不依赖调用方设 PYTHONUTF8：自己兜住。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_force_utf8_stdio()


SMOKE_PATH = os.path.join(HERE, "http_smoke.txt")

VERSION = "2026-07-28"
META = {"io.modelcontextprotocol/protocolVersion": VERSION,
        "io.modelcontextprotocol/clientInfo": {"name": "http-smoke",
                                               "version": "1.0.0"},
        "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}}


def _with_meta(params):
    p = dict(params)
    p["_meta"] = META
    return p


class Checks:
    def __init__(self):
        self.entries = []
        self.n_pass = 0
        self.n_fail = 0
        self.failures = []

    def check(self, name, ok, detail=""):
        if ok:
            self.n_pass += 1
            self.entries.append("PASS: %s%s"
                                % (name, (" — " + detail) if detail else ""))
        else:
            self.n_fail += 1
            self.failures.append(name)
            self.entries.append("FAIL: %s%s"
                                % (name, (" — " + detail) if detail else ""))

    def write(self):
        with open(SMOKE_PATH, "w", encoding="utf-8") as fh:
            fh.write("phocinae-mcp Streamable HTTP 冒烟（MCP %s）\n" % VERSION)
            fh.write("生成：每次运行重新生成（时间戳刻意不写入）\n")
            fh.write("=" * 72 + "\n\n")
            fh.write("\n".join(self.entries))
            fh.write("\n\n" + "=" * 72 + "\n")
            fh.write("汇总：PASS=%d FAIL=%d\n" % (self.n_pass, self.n_fail))
            if self.failures:
                fh.write("失败项：%s\n" % "；".join(self.failures))


_SENTINEL = object()


class HttpClient:
    """POST /mcp 助手：返回 (status, json_obj|None, raw_body)。

    默认自动携带 MCP-Protocol-Version（VERSION）与 Mcp-Method（=method）；
    显式传 None 表示省略该头（用于缺头测试）。
    """

    def __init__(self, base):
        self.base = base

    def post(self, method, params=None, rid=None, headers=None,
             raw_body=None, accept=("application/json", "text/event-stream"),
             method_hdr=_SENTINEL, name_hdr=None, version_hdr=_SENTINEL):
        body = raw_body
        if raw_body is None:
            msg = {"jsonrpc": "2.0", "method": method, "params": params}
            if rid is not None:
                msg["id"] = rid
            body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        hdrs = {"Content-Type": "application/json",
                "Accept": ", ".join(accept)}
        if version_hdr is _SENTINEL:
            version_hdr = VERSION
        if version_hdr:
            hdrs["MCP-Protocol-Version"] = version_hdr
        if method_hdr is _SENTINEL:
            method_hdr = method
        if method_hdr is not None:
            hdrs["Mcp-Method"] = method_hdr
        if name_hdr is not None:
            hdrs["Mcp-Name"] = name_hdr
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(self.base + "/mcp", data=body,
                                     headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                return (resp.status, self._try_json(raw), raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return (exc.code, self._try_json(raw), raw)
        except urllib.error.URLError as exc:
            return (None, None, str(exc).encode())

    @staticmethod
    def _try_json(raw):
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return None


def wait_port(proc, timeout=20.0):
    """从 stderr 解析监听端口（--port 0 → 随机端口）。"""
    pattern = re.compile(r"http://127\.0\.0\.1:(\d+)/mcp")
    found = {}

    def reader():
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace")
            m = pattern.search(line)
            if m and "port" not in found:
                found["port"] = int(m.group(1))

    threading.Thread(target=reader, daemon=True).start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if "port" in found:
            return found["port"]
        if proc.poll() is not None:
            raise RuntimeError("HTTP server 提前退出（rc=%s）" % proc.poll())
        time.sleep(0.1)
    raise RuntimeError("超时未解析到监听端口")


def main():
    mock = MockSystemone().start()
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    # Pin guard discovery to "not found" so the gate deterministically takes the
    # pure-HTTP fallback. Otherwise the suite's result depends on whether a
    # sibling phocinae-guard checkout happens to exist: with one present, L1 is
    # off by default and a grey-zone command becomes fail-closed deny instead of
    # the ask -> MRTR path H4/H5 exercise. locate_guard() treats a non-existent
    # path as "no guard".
    env["PHOCINAE_MCP_GUARD_PATH"] = os.path.join(
        tempfile.gettempdir(), "phocinae-mcp-test-no-such-guard.py")
    env.pop("PHOCINAE_GUARD_PATH", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "phocinae_mcp", "--transport", "http",
         "--port", "0", "--server", mock.base_url, "--timeout", "2"],
        cwd=PKG_ROOT, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE)
    ck = Checks()
    try:
        port = wait_port(proc)
        hc = HttpClient("http://127.0.0.1:%d" % port)
        ck.check("H0 HTTP 服务启动", True, "port=<PORT>（内核分配，不写入真实端口）")

        # ---- 正常路径
        st, obj, _ = hc.post("server/discover", _with_meta({}), rid=1)
        ck.check("H1 discover 200", st == 200 and obj["result"]["resultType"]
                 == "complete" and "2026-07-28" in
                 obj["result"]["supportedVersions"],
                 "status=%s" % st)

        st, obj, _ = hc.post("tools/list", _with_meta({}), rid=2)
        ck.check("H2 tools/list 缓存提示", st == 200
                 and obj["result"]["ttlMs"] > 0
                 and obj["result"]["cacheScope"] == "public")

        st, obj, _ = hc.post("tools/call",
                             _with_meta({"name": "gate",
                                         "arguments":
                                         {"command": "echo ok"}}),
                             rid=3, name_hdr="gate")
        ck.check("H3 gate allow", st == 200 and
                 obj["result"]["structuredContent"]["decision"] == "allow")

        # ---- MRTR：ask → retry accept
        st, obj, _ = hc.post("tools/call",
                             _with_meta({"name": "gate",
                                         "arguments":
                                         {"command": "ask human first"}}),
                             rid=4, name_hdr="gate")
        state = obj["result"].get("requestState")
        ck.check("H4 ask → input_required",
                 st == 200 and obj["result"]["resultType"]
                 == "input_required" and state)

        st, obj, _ = hc.post("tools/call",
                             _with_meta({"name": "gate",
                                         "arguments":
                                         {"command": "ask human first"},
                                         "inputResponses":
                                         {"gate_confirmation":
                                          {"action": "accept"}},
                                         "requestState": state}),
                             rid=5, name_hdr="gate")
        sc = obj["result"]["structuredContent"]
        ck.check("H5 accept → allow",
                 st == 200 and sc["decision"] == "allow"
                 and sc["layer"] == "human_approval")

        # ---- 头部校验（Server Validation）
        st, obj, _ = hc.post("server/discover", _with_meta({}), rid=6,
                             version_hdr=None)
        ck.check("H6 缺版本头 → 400 -32020",
                 st == 400 and obj["error"]["code"] == -32020)

        st, obj, _ = hc.post("server/discover", _with_meta({}), rid=7,
                             version_hdr="1900-01-01")
        ck.check("H7 版本不支持 → 400 -32022",
                 st == 400 and obj["error"]["code"] == -32022)

        st, obj, _ = hc.post("server/discover", _with_meta({}), rid=8,
                             method_hdr="tools/list")
        ck.check("H8 Mcp-Method 不一致 → 400 -32020",
                 st == 400 and obj["error"]["code"] == -32020)

        st, obj, _ = hc.post("tools/call",
                             _with_meta({"name": "gate",
                                         "arguments":
                                         {"command": "echo ok"}}),
                             rid=9, name_hdr=None)
        ck.check("H9 缺 Mcp-Name → 400 -32020",
                 st == 400 and obj["error"]["code"] == -32020)

        st, obj, _ = hc.post("tools/call",
                             _with_meta({"name": "gate",
                                         "arguments":
                                         {"command": "echo ok"}}),
                             rid=10, name_hdr="classify")
        ck.check("H10 Mcp-Name 不一致 → 400 -32020",
                 st == 400 and obj["error"]["code"] == -32020)

        st, obj, _ = hc.post("tools/list", _with_meta({}), rid=11,
                             accept=("application/xml",))
        ck.check("H11 Accept 不含 JSON/SSE → 406", st == 406)

        mismatch_body = json.dumps(
            {"jsonrpc": "2.0", "id": 12, "method": "tools/list",
             "params": {"_meta": {
                 "io.modelcontextprotocol/protocolVersion": "1900-01-01",
                 "io.modelcontextprotocol/clientCapabilities": {}}}},
            ensure_ascii=False).encode("utf-8")
        st, obj, _ = hc.post("tools/list", _with_meta({}), rid=12,
                             raw_body=mismatch_body)
        ck.check("H12 头版本与 body _meta 不一致 → 400 -32020",
                 st == 400 and obj["error"]["code"] == -32020)

        # ---- 通知 / 未知方法 / 坏请求
        st, _, raw = hc.post("notifications/cancelled",
                             _with_meta({"requestId": 3}))
        ck.check("H13 通知 → 202 空 body",
                 st == 202 and raw == b"")

        st, obj, _ = hc.post("ping", _with_meta({}), rid=13)
        ck.check("H14 未知方法 → 404 -32601",
                 st == 404 and obj["error"]["code"] == -32601)

        st, obj, _ = hc.post("server/discover", _with_meta({}), rid=14,
                             raw_body=b"{not-json")
        ck.check("H15 坏 JSON → 400 -32700",
                 st == 400 and obj["error"]["code"] == -32700)

        # ---- resources
        st, obj, _ = hc.post("resources/read",
                             _with_meta({"uri":
                                         "phocinae://policy/gate-approval"}),
                             rid=15, name_hdr="phocinae://policy/gate-approval")
        ck.check("H16 resources/read（Mcp-Name=uri）",
                 st == 200 and "审批策略" in
                 obj["result"]["contents"][0]["text"])

        # ---- GET 应 405（2026-07-28 移除 GET 流）
        req = urllib.request.Request(
            "http://127.0.0.1:%d/mcp" % port, method="GET")
        try:
            urllib.request.urlopen(req, timeout=10)
            st = 200
        except urllib.error.HTTPError as exc:
            st = exc.code
        ck.check("H17 GET → 405", st == 405, "status=%s" % st)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        mock.stop()
    ck.write()
    print("http_smoke: PASS=%d FAIL=%d → %s"
          % (ck.n_pass, ck.n_fail, SMOKE_PATH))
    if ck.n_fail:
        print("失败项：%s" % "；".join(ck.failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
