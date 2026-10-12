#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mock_systemone.py —— 斑海豹 P0 服务（/v1/systemone）的确定性 mock。

供 tests/test_mcp.py / test_http.py 使用：http.server 起在 127.0.0.1 随机端口，
按 state 内容确定性应答（不跑模型、不占 GPU）：

gate 触发词（state 为含 command/cwd 的 JSON 字符串）：
    "rm -rf"     → noul=true,  score=9   → deny（score 通道）
    "unsafe"     → noul=false, score=2   → deny（noul 通道）
    "ask"        → noul=true,  score=5   → ask
    默认          → noul=true,  score=2   → allow
故障触发词：
    "boom500"    → HTTP 500（服务不可用 → SystemoneError）
    "badjson"    → 非 JSON 响应体
    "outofrange" → score_q = 42（越界 → ToolError）
choice 题：下标 = len(state) % len(options)；score 题：2 + len(state) % 9。
"""

import json
import threading

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys


def _force_utf8_stdio():
    """把 stdout/stderr 固定为 UTF-8。

    本脚本会打印中文/箭头等非 ASCII 字符；Windows 上 Python 默认用控制台
    代码页（GitHub Actions runner 是 cp1252/437，本机可能是 cp936），
    非 ASCII 直接抛 UnicodeEncodeError 让整个步骤失败。
    这里不依赖调用方设 PYTHONUTF8：自己兜住。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_force_utf8_stdio()




class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # mock 保持安静

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/systemone":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            req = json.loads(raw.decode("utf-8"))
        except ValueError:
            self._send(400, {"error": "bad json"})
            return
        state = str(req.get("state", ""))
        if "boom500" in state:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if "badjson" in state:
            body = b"{not-json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        answers = {}
        for q in req.get("questions", []):
            qid = q.get("id", "")
            qtype = q.get("type", "")
            if qtype == "noul":
                answers[qid] = False if "unsafe" in state else True
            elif qtype == "choice":
                options = q.get("options", [])
                answers[qid] = (len(state) % len(options)) if options else 0
            elif qtype == "score":
                if "outofrange" in state:
                    answers[qid] = 42
                elif "rm -rf" in state:
                    answers[qid] = 9
                elif "ask" in state:
                    answers[qid] = 5
                else:
                    answers[qid] = 2  # 低风险 → allow
        payload = {
            "model": "mock-Phocinae-Largha-150M-v1",
            "answers": answers,
            "usage": {"total_tokens": 7, "prompt_tokens": 5,
                      "completion_tokens": 2},
            "answer_confidence": 0.95,
        }
        self._send(200, payload)

    def _send(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MockSystemone:
    """127.0.0.1 随机端口上的 mock P0 服务。"""

    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def base_url(self):
        return "http://127.0.0.1:%d" % self.port


if __name__ == "__main__":
    import time
    m = MockSystemone().start()
    print("mock on", m.base_url, flush=True)
    time.sleep(3600)
