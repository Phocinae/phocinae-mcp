#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_mcp.py —— MCP 客户端模拟：stdio 全流程冒烟测试（双代）。

流程：
  旧年代（2024-11-05，兼容原版）：initialize → notifications/initialized
    → ping → tools/list → tools/call 全工具矩阵（含失败分支）→ shutdown；
  现代年代（2026-07-28）：server/discover → tools/list → tools/call 全工具
    矩阵（含 gate ask 的 MRTR 交互、-32022/-32602/-32601、resources/*）。

全程校验 stdout 纯净性：stdout 每一行都必须是合法 JSON-RPC 帧；
日志（含启动行）只允许出现在 stderr。

输出：tests/mcp_smoke.txt（完整请求/响应记录 + 逐项 PASS/FAIL + 汇总）。
退出码：全过 0；任一失败 1。
"""

import json
import os
import queue
import subprocess
import tempfile
import sys
import threading
import time

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


SMOKE_PATH = os.path.join(HERE, "mcp_smoke.txt")

META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "smoke-client",
                                               "version": "1.0.0"},
        "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}}
META_NO_ELICIT = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                  "io.modelcontextprotocol/clientInfo": {"name": "smoke-client",
                                                         "version": "1.0.0"},
                  "io.modelcontextprotocol/clientCapabilities": {}}


def _with_meta(params, meta):
    p = dict(params)
    p["_meta"] = meta
    return p


class Transcript:
    """冒烟记录：请求/响应/断言 → tests/mcp_smoke.txt。

    client 可选：设置后 req() 在记录的同时把帧发给 stdio 客户端。
    """

    def __init__(self, client=None):
        self.entries = []
        self.n_pass = 0
        self.n_fail = 0
        self.failures = []
        self.client = client

    def req(self, tag, obj):
        self.entries.append(">>> [%s] %s"
                            % (tag, json.dumps(obj, ensure_ascii=False,
                                               separators=(",", ":"))))
        if self.client is not None:
            self.client.send(obj)

    def resp(self, obj):
        self.entries.append("<<< %s"
                            % json.dumps(obj, ensure_ascii=False,
                                         separators=(",", ":")))

    def note(self, text):
        self.entries.append("--- %s" % text)

    def check(self, name, ok, detail=""):
        if ok:
            self.n_pass += 1
            self.entries.append("PASS: %s%s" % (name,
                                                (" — " + detail) if detail else ""))
        else:
            self.n_fail += 1
            self.failures.append(name)
            self.entries.append("FAIL: %s%s" % (name,
                                                (" — " + detail) if detail else ""))

    def write(self):
        with open(SMOKE_PATH, "w", encoding="utf-8") as fh:
            fh.write("phocinae-mcp MCP 客户端模拟（stdio 全流程）\n")
            fh.write("时间：%s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            fh.write("传输：stdio（子进程 -m phocinae_mcp，mock P0 服务）\n")
            fh.write("=" * 72 + "\n\n")
            for e in self.entries:
                fh.write(e + "\n")
            fh.write("\n" + "=" * 72 + "\n")
            fh.write("汇总：PASS=%d FAIL=%d\n" % (self.n_pass, self.n_fail))
            if self.failures:
                fh.write("失败项：%s\n" % "；".join(self.failures))
        return SMOKE_PATH


class StdioClient:
    """stdio MCP 客户端：逐帧收发 + stdout 纯净性采集。"""

    def __init__(self, proc):
        self.proc = proc
        self.q = queue.Queue()
        self.raw_lines = []
        self.stderr_data = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        for raw in self.proc.stdout:
            line = raw.decode("utf-8").rstrip("\n")
            self.raw_lines.append(line)
            self.q.put(line)

    def _read_stderr(self):
        for raw in self.proc.stderr:
            self.stderr_data.append(raw.decode("utf-8", "replace").rstrip("\n"))

    def send(self, obj):
        self.proc.stdin.write(
            (json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
             + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def recv(self, timeout=15.0):
        try:
            return json.loads(self.q.get(timeout=timeout))
        except queue.Empty:
            err_tail = "".join(self.stderr_data[-20:])
            raise RuntimeError(
                "recv 超时（%ss）；server rc=%s；stderr 尾部：\n%s"
                % (timeout, self.proc.poll(), err_tail or "（空）"))

    def expect_no_response(self, timeout=1.0):
        """预期无响应；超时未收到帧 = 通过。"""
        try:
            extra = self.q.get(timeout=timeout)
            return ("unexpected", extra)
        except queue.Empty:
            return (None, None)

    def close(self):
        try:
            self.proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        self.proc.wait(timeout=10)


def start_server(mock):
    """启动被测 server。

    关键：把 guard 发现**固定为「找不到」**，让 gate 稳定走纯 HTTP 回退路径。

    否则本套件的结果取决于仓库旁边是否存在 phocinae-guard：
      · 没有 → 走 HTTP 回退，题面文本作为 state 送进 mock 模型，
        "ask human first" 命中 mock 的 ask 分支 → 灰区 → MRTR（M6 要考的路径）；
      · 有   → 走 guard 的 L0/编排，L1 默认关闭 → 灰区 fail-closed deny，
        没有 MRTR，M6 直接 KeyError。
    指向一个必然不存在的路径即可让 `locate_guard()` 返回空串（它用
    `os.path.isfile` 判定），从而与「干净检出/独立安装」的行为一致。
    真实 guard 的编排由 phocinae-guard 仓自己的回归套件覆盖。
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["PHOCINAE_MCP_GUARD_PATH"] = os.path.join(
        tempfile.gettempdir(), "phocinae-mcp-test-no-such-guard.py")
    env.pop("PHOCINAE_GUARD_PATH", None)  # 兼容别名不得覆盖上面的值
    proc = subprocess.Popen(
        [sys.executable, "-m", "phocinae_mcp",
         "--server", mock.base_url, "--timeout", "2"],
        cwd=PKG_ROOT, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)
    return proc


def rpc(method, params, rid):
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}


def legacy_flow(t, c):
    """旧年代（2024-11-05）：initialize → 全工具矩阵 → shutdown。"""
    t.note("======== 旧年代（2024-11-05，兼容原版 0.1.x）========")

    t.req("L1 initialize", rpc("initialize",
                               {"protocolVersion": "2024-11-05",
                                "capabilities": {},
                                "clientInfo": {"name": "legacy-client",
                                               "version": "0.9"}}, 1))
    r = c.recv(); t.resp(r)
    t.check("L1 initialize", r["result"]["protocolVersion"] == "2024-11-05"
            and r["result"]["serverInfo"]["name"] == "phocinae-mcp")

    t.req("L2 initialized", rpc("notifications/initialized", {}, None))
    t.check("L2 initialized 无响应",
            c.expect_no_response()[0] is None)

    t.req("L3 ping", rpc("ping", {}, 2))
    r = c.recv(); t.resp(r)
    t.check("L3 ping", r.get("result") == {})

    t.req("L4 tools/list", rpc("tools/list", {}, 3))
    r = c.recv(); t.resp(r)
    names = [x["name"] for x in r["result"]["tools"]]
    t.check("L4 tools/list 四工具", names == ["gate", "classify", "route",
                                              "score"],
            "顺序=" + ",".join(names))

    # ---- tools/call 全工具矩阵（旧年代）
    t.req("L5 gate allow", rpc("tools/call",
                               {"name": "gate",
                                "arguments": {"command": "echo ok"}}, 4))
    r = c.recv(); t.resp(r)
    body = json.loads(r["result"]["content"][0]["text"])
    t.check("L5 gate allow", body["decision"] == "allow"
            and r["result"]["isError"] is False,
            "decision=%s layer=%s" % (body.get("decision"),
                                      body.get("layer")))

    t.req("L6 gate deny", rpc("tools/call",
                              {"name": "gate",
                               "arguments": {"command": "rm -rf /"}}, 5))
    r = c.recv(); t.resp(r)
    body = json.loads(r["result"]["content"][0]["text"])
    t.check("L6 gate deny", body["decision"] == "deny",
            "decision=%s" % body.get("decision"))

    t.req("L7 gate ask（旧年代：普通结果）",
          rpc("tools/call", {"name": "gate",
                             "arguments": {"command": "ask human first"}}, 6))
    r = c.recv(); t.resp(r)
    body = json.loads(r["result"]["content"][0]["text"])
    t.check("L7 gate ask 旧形状", body["decision"] == "ask"
            and "resultType" not in r["result"]
            and r["result"]["isError"] is False,
            "decision=%s" % body.get("decision"))

    t.req("L8 classify", rpc("tools/call",
                             {"name": "classify",
                              "arguments": {"state": "用户问天气",
                                            "labels": ["weather", "news",
                                                       "sports"]}}, 7))
    r = c.recv(); t.resp(r)
    body = json.loads(r["result"]["content"][0]["text"])
    t.check("L8 classify", body["question"] == "classify_choice"
            and isinstance(body["choice"], int)
            and 0 <= body["choice"] < 3,
            "label=%s choice=%s" % (body.get("label"), body.get("choice")))

    t.req("L9 route", rpc("tools/call",
                          {"name": "route",
                           "arguments": {"state": "读文件",
                                         "tools": ["cat", "sed",
                                                   "awk"]}}, 8))
    r = c.recv(); t.resp(r)
    body = json.loads(r["result"]["content"][0]["text"])
    t.check("L9 route", body["question"] == "route_choice"
            and body["tool"] in ("cat", "sed", "awk"),
            "tool=%s" % body.get("tool"))

    t.req("L10 score", rpc("tools/call",
                           {"name": "score",
                            "arguments": {"state": "该命令风险",
                                          "criteria": ["危害面", "可逆性"]}},
                           9))
    r = c.recv(); t.resp(r)
    body = json.loads(r["result"]["content"][0]["text"])
    t.check("L10 score", isinstance(body["score"], (int, float))
            and 2 <= body["score"] <= 10, "score=%s" % body.get("score"))

    t.req("L11 未知工具", rpc("tools/call",
                              {"name": "nope",
                               "arguments": {}}, 10))
    r = c.recv(); t.resp(r)
    t.check("L11 未知工具 -32602", r["error"]["code"] == -32602)

    t.req("L12 shutdown", rpc("shutdown", {}, 11))
    r = c.recv(); t.resp(r)
    t.check("L12 shutdown", r.get("result") is None)


def modern_flow(t, c, mock):
    """现代年代（2026-07-28）：discover → 缓存提示 → 全工具矩阵 → MRTR →
    错误码 → 资源 → 通知。"""
    t.note("======== 现代年代（2026-07-28 对齐面）========")

    t.req("M1 discover", rpc("server/discover", _with_meta({}, META), 100))
    r = c.recv(); t.resp(r)
    res = r["result"]
    t.check("M1 discover 形状",
            res["resultType"] == "complete"
            and "2026-07-28" in res["supportedVersions"]
            and "tools" in res["capabilities"]
            and "resources" in res["capabilities"]
            and res["_meta"]["io.modelcontextprotocol/serverInfo"]["name"]
            == "phocinae-mcp"
            and res["ttlMs"] >= 0
            and res["cacheScope"] == "public",
            "supportedVersions=%s" % res["supportedVersions"])

    t.req("M2 tools/list", rpc("tools/list", _with_meta({}, META), 101))
    r = c.recv(); t.resp(r)
    res = r["result"]
    names = [x["name"] for x in res["tools"]]
    t.check("M2 tools/list 缓存提示",
            res["resultType"] == "complete"
            and res["ttlMs"] > 0 and res["cacheScope"] == "public"
            and names == ["gate", "classify", "route", "score"],
            "ttlMs=%s scope=%s" % (res["ttlMs"], res["cacheScope"]))

    t.req("M3 gate allow", rpc("tools/call",
                               _with_meta({"name": "gate",
                                           "arguments":
                                           {"command": "echo ok"}}, META),
                               102))
    r = c.recv(); t.resp(r)
    sc = r["result"]["structuredContent"]
    t.check("M3 gate allow（resultType+structuredContent）",
            r["result"]["resultType"] == "complete"
            and sc["decision"] == "allow"
            and r["result"]["isError"] is False,
            "decision=%s layer=%s" % (sc["decision"], sc["layer"]))

    t.req("M4 gate deny", rpc("tools/call",
                              _with_meta({"name": "gate",
                                          "arguments":
                                          {"command": "rm -rf /"}}, META),
                              103))
    r = c.recv(); t.resp(r)
    t.check("M4 gate deny",
            r["result"]["structuredContent"]["decision"] == "deny")

    # M5 uses a GREY-ZONE command, not `echo ok`. `echo ok` is in the L0
    # whitelist, and the L0 layer now ships inside this package, so it is allowed
    # offline without ever contacting the server — which means it cannot exercise
    # "fail-closed when the service is unreachable". A grey-zone command (one L0
    # cannot decide) does reach the model, and that is the path this assertion is
    # about. Changing the command does not weaken the check; it makes the check
    # test what it claims to test.
    t.req("M5 gate fail-closed（服务不可用 · 灰区命令）",
          rpc("tools/call",
              _with_meta({"name": "gate",
                          "arguments": {"command": "pip install requests",
                                        "server":
                                        "http://127.0.0.1:1"}}, META),
              104))
    r = c.recv(); t.resp(r)
    sc = r["result"]["structuredContent"]
    t.check("M5 gate fail-closed",
            sc["decision"] == "deny" and sc["layer"] == "fail_closed"
            and r["result"]["isError"] is False,
            "layer=%s reason=%s" % (sc["layer"], sc["reason"][:40]))

    # ---- gate ask → MRTR（elicitation/create + requestState）----
    t.req("M6 gate ask（elicitation 已声明）",
          rpc("tools/call",
              _with_meta({"name": "gate",
                          "arguments": {"command": "ask human first"}},
                         META), 105))
    r = c.recv(); t.resp(r)
    res = r["result"]
    ir = res.get("inputRequests", {}).get("gate_confirmation", {})
    t.check("M6 ask → input_required",
            res["resultType"] == "input_required"
            and ir.get("method") == "elicitation/create"
            and "requestState" in res
            and res["requestState"],
            "mode=%s" % ir.get("params", {}).get("mode"))
    state_token = res["requestState"]

    t.req("M7 retry accept", rpc("tools/call",
                                 _with_meta(
                                     {"name": "gate",
                                      "arguments":
                                      {"command": "ask human first"},
                                      "inputResponses":
                                      {"gate_confirmation":
                                       {"action": "accept"}},
                                      "requestState": state_token}, META),
                                 106))
    r = c.recv(); t.resp(r)
    sc = r["result"]["structuredContent"]
    t.check("M7 accept → allow",
            sc["decision"] == "allow"
            and sc["layer"] == "human_approval"
            and sc["human_action"] == "approve",
            "decision=%s human_action=%s" % (sc["decision"],
                                             sc["human_action"]))

    t.req("M8 再问 + decline",
          rpc("tools/call",
              _with_meta({"name": "gate",
                          "arguments": {"command": "ask human first"}},
                         META), 107))
    r = c.recv(); t.resp(r)
    state2 = r["result"]["requestState"]
    t.req("M8b retry decline", rpc("tools/call",
                                   _with_meta(
                                       {"name": "gate",
                                        "arguments":
                                        {"command": "ask human first"},
                                        "inputResponses":
                                        {"gate_confirmation":
                                         {"action": "decline"}},
                                        "requestState": state2}, META), 108))
    r = c.recv(); t.resp(r)
    sc = r["result"]["structuredContent"]
    t.check("M8 decline → deny",
            sc["decision"] == "deny" and sc["human_action"] == "reject")

    t.req("M9 再问 + cancel",
          rpc("tools/call",
              _with_meta({"name": "gate",
                          "arguments": {"command": "ask human first"}},
                         META), 109))
    r = c.recv(); t.resp(r)
    state3 = r["result"]["requestState"]
    t.req("M9b retry cancel", rpc("tools/call",
                                  _with_meta(
                                      {"name": "gate",
                                       "arguments":
                                       {"command": "ask human first"},
                                       "inputResponses":
                                       {"gate_confirmation":
                                        {"action": "cancel"}},
                                       "requestState": state3}, META), 110))
    r = c.recv(); t.resp(r)
    sc = r["result"]["structuredContent"]
    t.check("M9 cancel → 不授权",
            sc.get("status") == "cancelled"
            and sc["decision"] == "ask"
            and sc["human_action"] == "cancel")

    t.req("M10 篡改 requestState → 重发交互",
          rpc("tools/call",
              _with_meta({"name": "gate",
                          "arguments": {"command": "ask human first"},
                          "inputResponses": {"gate_confirmation":
                                             {"action": "accept"}},
                          "requestState": state_token + "tampered"},
                         META), 111))
    r = c.recv(); t.resp(r)
    t.check("M10 篡改被拒 → input_required 重发",
            r["result"]["resultType"] == "input_required"
            and "requestState" in r["result"])

    t.req("M11 ask 但客户端未声明 elicitation",
          rpc("tools/call",
              _with_meta({"name": "gate",
                          "arguments": {"command": "ask human first"}},
                         META_NO_ELICIT), 112))
    r = c.recv(); t.resp(r)
    sc = r["result"]["structuredContent"]
    t.check("M11 无 elicitation → complete+ask",
            r["result"]["resultType"] == "complete"
            and sc["decision"] == "ask"
            and "inputRequests" not in r["result"])

    # ---- 其余工具矩阵（现代年代）
    t.req("M12 classify", rpc("tools/call",
                              _with_meta({"name": "classify",
                                          "arguments":
                                          {"state": "用户问天气",
                                           "labels": ["weather", "news",
                                                      "sports"]}}, META),
                              113))
    r = c.recv(); t.resp(r)
    sc = r["result"]["structuredContent"]
    t.check("M12 classify", r["result"]["resultType"] == "complete"
            and sc["question"] == "classify_choice"
            and sc["label"] in ("weather", "news", "sports"),
            "label=%s" % sc.get("label"))

    t.req("M13 route", rpc("tools/call",
                           _with_meta({"name": "route",
                                       "arguments":
                                       {"state": "读文件",
                                        "tools": ["cat", "sed", "awk"]}},
                                      META), 114))
    r = c.recv(); t.resp(r)
    t.check("M13 route", r["result"]["structuredContent"]["tool"]
            in ("cat", "sed", "awk"))

    t.req("M14 score", rpc("tools/call",
                           _with_meta({"name": "score",
                                       "arguments":
                                       {"state": "该命令风险",
                                        "criteria": ["危害面", "可逆性"]}},
                                      META), 115))
    r = c.recv(); t.resp(r)
    sc = r["result"]["structuredContent"]
    t.check("M14 score", 2 <= sc["score"] <= 10
            and sc["range"] == [2, 10], "score=%s" % sc["score"])

    t.req("M15 score 应答越界 → isError",
          rpc("tools/call",
              _with_meta({"name": "score",
                          "arguments": {"state": "outofrange 值",
                                        "criteria": ["x"]}}, META), 116))
    r = c.recv(); t.resp(r)
    t.check("M15 score 越界", r["result"]["isError"] is True
            and r["result"]["resultType"] == "complete")

    t.req("M16 classify 服务不可用 → isError",
          rpc("tools/call",
              _with_meta({"name": "classify",
                          "arguments": {"state": "boom500 服务挂了",
                                        "labels": ["a", "b"]}}, META), 117))
    r = c.recv(); t.resp(r)
    t.check("M16 classify 服务不可用", r["result"]["isError"] is True)

    t.req("M17 长度上限（command 20001 字符）",
          rpc("tools/call",
              _with_meta({"name": "gate",
                          "arguments": {"command": "x" * 20001}}, META), 118))
    r = c.recv(); t.resp(r)
    t.check("M17 长度上限 -32602", r["error"]["code"] == -32602)

    # ---- 错误码矩阵
    t.req("M18 未知工具", rpc("tools/call",
                              _with_meta({"name": "nope",
                                          "arguments": {}}, META), 119))
    r = c.recv(); t.resp(r)
    t.check("M18 未知工具 -32602", r["error"]["code"] == -32602)

    t.req("M19 现代年代 ping（已移除）",
          rpc("ping", _with_meta({}, META), 120))
    r = c.recv(); t.resp(r)
    t.check("M19 ping -32601", r["error"]["code"] == -32601)

    t.req("M20 版本不支持", rpc("server/discover",
                                _with_meta({},
                                           {"io.modelcontextprotocol/protocolVersion":
                                            "1900-01-01",
                                            "io.modelcontextprotocol/clientCapabilities":
                                            {}}), 121))
    r = c.recv(); t.resp(r)
    t.check("M20 -32022", r["error"]["code"] == -32022
            and r["error"]["data"]["supported"] == ["2026-07-28"]
            and r["error"]["data"]["requested"] == "1900-01-01")

    t.req("M21 server/discover 缺 _meta",
          rpc("server/discover", {}, 122))
    r = c.recv(); t.resp(r)
    t.check("M21 现代专属方法缺 _meta → -32602",
            r["error"]["code"] == -32602)

    t.req("M21b tools/list 无 _meta（兼容设计：按旧年代应答）",
          rpc("tools/list", {}, 1220))
    r = c.recv(); t.resp(r)
    names = [x["name"] for x in r["result"]["tools"]]
    t.check("M21b 无 _meta 走旧年代形状",
            "resultType" not in r["result"]
            and names == ["gate", "classify", "route", "score"])

    t.req("M22 缺 clientCapabilities",
          rpc("tools/list",
              _with_meta({},
                         {"io.modelcontextprotocol/protocolVersion":
                          "2026-07-28"}), 123))
    r = c.recv(); t.resp(r)
    t.check("M22 缺 clientCapabilities -32602",
            r["error"]["code"] == -32602)

    t.req("M23 cancelled 通知", rpc("notifications/cancelled",
                                    _with_meta({"requestId": 102}, META),
                                    None))
    t.check("M23 通知无响应", c.expect_no_response()[0] is None)

    # ---- resources
    t.req("M24 resources/list", rpc("resources/list",
                                    _with_meta({}, META), 124))
    r = c.recv(); t.resp(r)
    res = r["result"]
    t.check("M24 resources/list",
            res["resultType"] == "complete"
            and res["resources"][0]["uri"]
            == "phocinae://policy/gate-approval"
            and res["ttlMs"] > 0 and res["cacheScope"] == "public")

    t.req("M25 resources/read", rpc("resources/read",
                                    _with_meta({"uri":
                                                "phocinae://policy/gate-approval"},
                                               META), 125))
    r = c.recv(); t.resp(r)
    res = r["result"]
    text = res["contents"][0]["text"]
    t.check("M25 resources/read 审批策略",
            res["resultType"] == "complete" and "审批策略" in text
            and "fail-closed" in text and res["ttlMs"] > 0,
            "%d 字符" % len(text))

    t.req("M26 resources/read 未知 uri",
          rpc("resources/read",
              _with_meta({"uri": "phocinae://nope"}, META), 126))
    r = c.recv(); t.resp(r)
    t.check("M26 资源不存在 -32602", r["error"]["code"] == -32602)


def main():
    mock = MockSystemone().start()
    proc = start_server(mock)
    c = StdioClient(proc)
    t = Transcript(client=c)
    try:
        legacy_flow(t, c)
        modern_flow(t, c, mock)
        time.sleep(0.3)
        # ---- stdout 纯净性审计
        n_lines = len(c.raw_lines)
        all_json = True
        bad = []
        for i, line in enumerate(c.raw_lines):
            try:
                json.loads(line)
            except ValueError:
                all_json = False
                bad.append(i)
        t.check("S1 stdout 每行均为 JSON-RPC 帧", all_json,
                "%d 帧，非法帧=%s" % (n_lines, bad or "无"))
        t.check("S2 日志只走 stderr",
                all("phocinae-mcp[" not in ln for ln in c.raw_lines)
                and any(ln for ln in c.stderr_data),
                "stderr %d 行（含启动行）" % len(c.stderr_data))
        t.check("S3 无 traceback 泄漏到 stdout",
                all("Traceback" not in ln for ln in c.raw_lines))
    except Exception:
        t.write()  # 失败也落盘，便于诊断
        raise
    finally:
        c.close()
        mock.stop()
    path = t.write()
    print("mcp_smoke: PASS=%d FAIL=%d → %s"
          % (t.n_pass, t.n_fail, path))
    if t.n_fail:
        print("失败项：%s" % "；".join(t.failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

