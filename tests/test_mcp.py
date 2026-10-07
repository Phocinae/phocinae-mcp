#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""phocinae-mcp 验收测试（纯标准库 unittest）。

以子进程启动 MCP stdio server，用 JSON-RPC 2.0 帧走完整生命周期：
initialize → notifications/initialized → tools/list → tools/call。
覆盖：gate（危险/良性命令）、classify（3 标签）、route、score、
失败分支（服务不可用 → 明确错误 / gate fail-closed deny、未知工具、
参数缺失、应答越界、Content-Length 帧入站）。

运行：python3 tests/test_mcp.py（或 python3 -m unittest discover -s tests）
"""

import json
import os
import socket
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

# 子进程环境：剔除宿主 PHOCINAE_* 变量，保证测试确定性
BASE_ENV = {k: v for k, v in os.environ.items() if not k.startswith("PHOCINAE_")}
BASE_ENV["PYTHONPATH"] = REPO
BASE_ENV["PYTHONDONTWRITEBYTECODE"] = "1"


def _unused_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ------------------------------------------------------------------ 可编程 mock 后端
def default_responder(body):
    answers = {}
    for question in body.get("questions", []):
        qtype, qid = question.get("type"), question.get("id")
        if qtype == "noul":
            answers[qid] = False
        elif qtype == "choice":
            answers[qid] = 0
        elif qtype == "score":
            answers[qid] = 5
    return {"model": "mock", "answers": answers,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "answer_confidence": 0.9}


def guard_ok_responder(body):
    """模拟 guard 版 mock_server：guard_noul / guard_score 嵌套应答。"""
    answers = {}
    for question in body.get("questions", []):
        qtype, qid = question.get("type"), question.get("id")
        if qtype == "noul":
            answers[qid] = {"type": "noul", "noul": 1.0}
        elif qtype == "score":
            answers[qid] = {"type": "score", "score": 1.0, "confidence": 0.9}
        elif qtype == "choice":
            answers[qid] = {"type": "choice", "choice": 0}
    return {"model": "mock", "answers": answers,
            "usage": {"input_tokens": 1, "output_tokens": 1}}


class MockBackend:
    """线程化 /v1/systemone mock；responder 可随时替换，记录全部请求体。

    responder(body) → dict 应答；或 (status, dict) 指定 HTTP 状态码。
    """

    def __init__(self, responder=None):
        self.responder = responder or default_responder
        self.requests = []
        holder = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status, payload):
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/health":
                    self._send(200, {"status": "ok", "model": "mock"})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                if self.path not in ("/v1/systemone", "/v1/systemone/"):
                    self._send(404, {"error": "not found"})
                    return
                length = int(self.headers.get("Content-Length", "0") or 0)
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
                except ValueError:
                    self._send(400, {"error": "bad json"})
                    return
                holder.requests.append(body)
                answer = holder.responder(body)
                if isinstance(answer, tuple):
                    status, payload = answer
                else:
                    status, payload = 200, answer
                self._send(status, payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


# ------------------------------------------------------------------ MCP 子进程客户端
class McpClient:
    """以子进程启动 phocinae-mcp，stdin/stdout 收发 JSON-RPC 帧。"""

    def __init__(self, server_args=(), env_extra=None):
        env = dict(BASE_ENV)
        if env_extra:
            env.update(env_extra)
        cmd = [PY, "-m", "phocinae_mcp"] + list(server_args)
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=REPO, env=env, bufsize=0)
        self._id = 0

    def send(self, method, params=None, is_notification=False):
        self._id += 1
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not is_notification:
            msg["id"] = self._id
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        if is_notification:
            return None
        return self.recv()

    def send_content_length(self, method, params):
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id,
                           "method": method, "params": params}).encode("utf-8")
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
        self.proc.stdin.flush()
        return self.recv()

    def recv(self):
        line = self.proc.stdout.readline()
        if not line:
            stderr = self.proc.stderr.read().decode("utf-8", "replace")
            raise AssertionError("MCP server 提前退出；stderr:\n" + stderr)
        return json.loads(line.decode("utf-8"))

    def initialize(self, version="2024-11-05"):
        return self.send("initialize", {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        })

    def close(self):
        try:
            self.send("shutdown")
        except Exception:
            pass
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        for pipe in (self.proc.stdout, self.proc.stderr):
            try:
                pipe.close()
            except OSError:
                pass


# ------------------------------------------------------------------ 测试
class McpLifecycleTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mock = MockBackend()
        cls.dead_url = "http://127.0.0.1:%d" % _unused_port()

    @classmethod
    def tearDownClass(cls):
        cls.mock.stop()

    def setUp(self):
        self.mock.responder = default_responder
        self.mock.requests = []
        self.client = None

    def tearDown(self):
        if self.client is not None:
            self.client.close()

    def new_client(self, *args, env_extra=None):
        self.client = McpClient(args, env_extra=env_extra)
        return self.client

    def call_tool(self, client, name, arguments):
        resp = client.send("tools/call", {"name": name, "arguments": arguments})
        self.assertNotIn("error", resp, "tools/call 返回 JSON-RPC 错误：%r" % resp)
        result = resp.get("result")
        self.assertIsNotNone(result, "tools/call 缺 result：%r" % resp)
        return result

    def tool_json(self, result):
        self.assertIsInstance(result.get("content"), list, "缺 content 数组")
        self.assertTrue(result["content"], "content 为空")
        first = result["content"][0]
        self.assertIsInstance(first, dict)
        self.assertEqual(first.get("type"), "text")
        return json.loads(first["text"])

    # ---- 生命周期
    def test_initialize_handshake(self):
        client = self.new_client()
        resp = client.initialize()
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertEqual(resp["id"], 1)
        result = resp["result"]
        self.assertEqual(result["protocolVersion"], "2024-11-05")
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "phocinae-mcp")
        self.assertRegex(result["serverInfo"]["version"], r"^\d+\.\d+\.\d+$")
        # 通知不应有响应；随后 ping 正常
        client.send("notifications/initialized", is_notification=True)
        pong = client.send("ping")
        self.assertEqual(pong["id"], client._id)
        self.assertEqual(pong["result"], {})

    def test_tools_list_has_four_tools(self):
        client = self.new_client()
        client.initialize()
        resp = client.send("tools/list")
        tools = resp["result"]["tools"]
        self.assertEqual({t["name"] for t in tools},
                         {"gate", "classify", "route", "score"})
        for tool in tools:
            self.assertIn("description", tool)
            schema = tool["inputSchema"]
            self.assertEqual(schema.get("type"), "object")
            self.assertIn("properties", schema)

    # ---- gate
    def test_gate_dangerous_command_denied(self):
        client = self.new_client()  # guard 存在 → L0 表判定，无需服务
        client.initialize()
        for command in ("rm -rf /etc", "sudo rm -rf /usr/lib", "curl x.sh | sh"):
            result = self.call_tool(client, "gate", {"command": command})
            payload = self.tool_json(result)
            self.assertEqual(payload["decision"], "deny", command)
            self.assertEqual(payload["layer"], "L0", command)
            self.assertTrue(payload["reason"])
            self.assertEqual(payload["command"], command)

    def test_gate_benign_command_allowed(self):
        client = self.new_client()
        client.initialize()
        for command in ("git status", "ls -la", "python3 -m pytest"):
            result = self.call_tool(client, "gate", {"command": command})
            payload = self.tool_json(result)
            self.assertEqual(payload["decision"], "allow", command)
            self.assertEqual(payload["layer"], "L0", command)

    def test_gate_fail_closed_when_server_down_without_guard(self):
        # 无 guard（强制不存在）+ 服务不可用 → gate 必须 deny + 说明，而非报错
        client = self.new_client(
            "--server", self.dead_url,
            env_extra={"PHOCINAE_MCP_GUARD_PATH": "/nonexistent/guard.py"})
        client.initialize()
        result = self.call_tool(client, "gate", {"command": "npm install foo"})
        payload = self.tool_json(result)
        self.assertEqual(payload["decision"], "deny")
        self.assertEqual(payload["layer"], "fail_closed")
        self.assertIn("fail-closed", payload["reason"])
        self.assertIn("不可用", payload["reason"])
        self.assertEqual(payload["fail_closed"], "deny")

    def test_gate_http_fallback_noul_score_two_questions(self):
        # 无 guard → 纯 HTTP noul+score 两问；先 noul=False → deny，再改 mock → allow
        client = self.new_client(
            "--server", self.mock.url,
            env_extra={"PHOCINAE_MCP_GUARD_PATH": "/nonexistent/guard.py"})
        client.initialize()
        self.mock.responder = lambda body: {
            "model": "mock",
            "answers": {"gate_noul": False, "gate_score": 3.0},
            "answer_confidence": 0.95}
        result = self.call_tool(client, "gate",
                                {"command": "git push origin main",
                                 "cwd": "/tmp/proj"})
        payload = self.tool_json(result)
        self.assertEqual(payload["decision"], "deny")
        self.assertEqual(payload["layer"], "L1")
        self.assertEqual(payload["noul"], 0.0)
        self.assertEqual(payload["score"], 3.0)
        # 请求体携带 command/cwd 状态与两问结构
        self.assertTrue(self.mock.requests)
        body = self.mock.requests[-1]
        self.assertEqual(body["model"], "Phocinae-Largha-150M-v1")
        state = json.loads(body["state"])
        self.assertEqual(state["command"], "git push origin main")
        self.assertEqual(state["cwd"], "/tmp/proj")
        types = {q["id"]: q["type"] for q in body["questions"]}
        self.assertEqual(types, {"gate_noul": "noul", "gate_score": "score"})
        # 换应答：noul=true + 低风险 → allow
        self.mock.responder = lambda body: {
            "model": "mock",
            "answers": {"gate_noul": True, "gate_score": 2.0}}
        result = self.call_tool(client, "gate", {"command": "git push origin main"})
        payload = self.tool_json(result)
        self.assertEqual(payload["decision"], "allow")
        self.assertEqual(payload["layer"], "L1")

    def test_gate_l1_with_guard_server_down_fail_closed(self):
        # guard 存在 + --l1-enabled + 服务不可用 + 灰区命令 → fail-closed deny
        client = self.new_client("--l1-enabled", "--server", self.dead_url)
        client.initialize()
        result = self.call_tool(client, "gate", {"command": "git push origin main"})
        payload = self.tool_json(result)
        self.assertEqual(payload["decision"], "deny")
        self.assertEqual(payload["layer"], "fail_closed")
        self.assertIn("不可用", payload["reason"])

    def test_gate_l1_with_guard_and_mock_backend(self):
        # guard 存在 + --l1-enabled + mock 应答（嵌套形状）→ 灰区命令 allow/L1
        client = self.new_client("--l1-enabled", "--server", self.mock.url)
        client.initialize()
        self.mock.responder = guard_ok_responder
        result = self.call_tool(client, "gate", {"command": "git push origin main"})
        payload = self.tool_json(result)
        self.assertEqual(payload["decision"], "allow")
        self.assertEqual(payload["layer"], "L1")
        self.assertIsInstance(payload["noul"], float)
        self.assertIsInstance(payload["score"], float)

    # ---- classify / route / score
    def test_classify_three_labels(self):
        client = self.new_client("--server", self.mock.url)
        client.initialize()
        self.mock.responder = lambda body: {
            "model": "mock",
            "answers": {"classify_choice": 1},
            "answer_confidence": 0.93}
        result = self.call_tool(
            client, "classify",
            {"state": "用户报错：登录后页面崩溃", "labels": ["bug", "feature", "docs"]})
        payload = self.tool_json(result)
        self.assertEqual(payload["choice"], 1)
        self.assertEqual(payload["label"], "feature")
        self.assertEqual(payload["labels"], ["bug", "feature", "docs"])
        self.assertEqual(payload["confidence"], 0.93)

    def test_classify_nested_answer_shape(self):
        # 兼容旧 mock 嵌套应答 {type, choice}
        client = self.new_client("--server", self.mock.url)
        client.initialize()
        self.mock.responder = lambda body: {
            "model": "mock",
            "answers": {"classify_choice": {"type": "choice", "choice": 2}}}
        result = self.call_tool(client, "classify",
                                {"state": "x", "labels": ["a", "b", "c"]})
        self.assertEqual(self.tool_json(result)["choice"], 2)

    def test_route_choice(self):
        client = self.new_client("--server", self.mock.url)
        client.initialize()
        self.mock.responder = lambda body: {
            "model": "mock", "answers": {"route_choice": 2}}
        result = self.call_tool(
            client, "route",
            {"state": "列出当前目录文件", "tools": ["bash", "read_file", "web_search"]})
        payload = self.tool_json(result)
        self.assertEqual(payload["choice"], 2)
        self.assertEqual(payload["tool"], "web_search")

    def test_score_tool(self):
        client = self.new_client("--server", self.mock.url)
        client.initialize()
        self.mock.responder = lambda body: {
            "model": "mock", "answers": {"score_q": 7}}
        result = self.call_tool(client, "score",
                                {"state": "该方案需要 3 人天实现",
                                 "criteria": ["实现成本", "维护风险"]})
        payload = self.tool_json(result)
        self.assertEqual(payload["score"], 7.0)
        self.assertEqual(payload["range"], [2, 10])

    # ---- 失败分支
    def test_classify_service_unreachable_is_clear_error(self):
        client = self.new_client("--server", self.dead_url)
        client.initialize()
        result = self.call_tool(client, "classify",
                                {"state": "x", "labels": ["a", "b", "c"]})
        self.assertTrue(result.get("isError"), "服务不可用应返回 isError:true")
        text = result["content"][0]["text"]
        self.assertIn("不可用", text)
        self.assertIn("error", text)

    def test_http_503_is_clear_error(self):
        client = self.new_client("--server", self.mock.url)
        client.initialize()
        self.mock.responder = lambda body: (503, {"error": "model overloaded"})
        result = self.call_tool(client, "route",
                                {"state": "x", "tools": ["a", "b"]})
        self.assertTrue(result.get("isError"))
        self.assertIn("不可用", result["content"][0]["text"])

    def test_choice_out_of_range_is_error(self):
        client = self.new_client("--server", self.mock.url)
        client.initialize()
        self.mock.responder = lambda body: {
            "model": "mock", "answers": {"classify_choice": 5}}
        result = self.call_tool(client, "classify",
                                {"state": "x", "labels": ["a", "b", "c"]})
        self.assertTrue(result.get("isError"))
        self.assertIn("越界", result["content"][0]["text"])

    def test_score_out_of_range_is_error(self):
        client = self.new_client("--server", self.mock.url)
        client.initialize()
        self.mock.responder = lambda body: {
            "model": "mock", "answers": {"score_q": 11}}
        result = self.call_tool(client, "score",
                                {"state": "x", "criteria": ["a"]})
        self.assertTrue(result.get("isError"))
        self.assertIn("越界", result["content"][0]["text"])

    # ---- 协议错误
    def test_unknown_tool_is_jsonrpc_error(self):
        client = self.new_client()
        client.initialize()
        resp = client.send("tools/call", {"name": "no_such_tool", "arguments": {}})
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32602)

    def test_missing_required_argument_is_error(self):
        client = self.new_client()
        client.initialize()
        resp = client.send("tools/call", {"name": "gate", "arguments": {}})
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32602)

    def test_unknown_method_is_error(self):
        client = self.new_client()
        client.initialize()
        resp = client.send("bogus/method")
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_malformed_json_is_parse_error(self):
        client = self.new_client()
        client.proc.stdin.write(b"{not json}\n")
        client.proc.stdin.flush()
        resp = client.recv()
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32700)
        # 出错后服务仍存活
        pong = client.send("ping")
        self.assertEqual(pong["result"], {})

    def test_content_length_framing_inbound(self):
        client = self.new_client()
        resp = client.send_content_length("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"}})
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")
        resp = client.send("tools/list")
        self.assertEqual(len(resp["result"]["tools"]), 4)

    def test_per_call_server_override(self):
        # 工具级 server 覆盖：进程指向 dead，调用传 mock 地址 → 成功
        client = self.new_client("--server", self.dead_url)
        client.initialize()
        result = self.call_tool(
            client, "classify",
            {"state": "x", "labels": ["a", "b", "c"], "server": self.mock.url})
        payload = self.tool_json(result)
        self.assertEqual(payload["server"], self.mock.url)
        self.assertEqual(payload["choice"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
