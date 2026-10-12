#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_docker.py — 用 Glama 的检查口径验证容器镜像。

Glama 的目录检查要求「服务器能起来并应答 introspection 请求」。这个脚本把
那条要求变成可复跑的命令：起容器、用 stdin 发 initialize 与 tools/list、
逐帧解析 stdout，并断言：

  1. 两帧都回来，且是合法 JSON-RPC；
  2. serverInfo.name/version 正确，version 与镜像里装的 PyPI 版本一致；
  3. tools/list 返回四个工具，每个都有 inputSchema 与 description；
  4. **stdout 只含 JSON-RPC 帧** —— MCP 的 stdio 硬约束；诊断必须在 stderr。
     这条最容易在容器里被 locale 或日志配置破坏，所以单列。

退出码：0=通过，1=失败。
"""
import json
import subprocess
import sys


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


IMAGE = sys.argv[1] if len(sys.argv) > 1 else "phocinae-mcp:0.2.2"
EXPECTED_TOOLS = {"gate", "classify", "route", "score"}

REQS = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "directory-probe", "version": "1"}}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
]
payload = ("\n".join(json.dumps(r) for r in REQS) + "\n").encode("utf-8")

fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  " + str(detail)))
    if not cond:
        fails.append(name)
    return cond


def main():
    proc = subprocess.run(["docker", "run", "-i", "--rm", IMAGE],
                          input=payload, capture_output=True, timeout=300)
    stdout = proc.stdout.decode("utf-8", "replace")
    stderr = proc.stderr.decode("utf-8", "replace")

    check("容器以退出码 0 收尾（stdin EOF 后正常退出）",
          proc.returncode == 0, "rc=%s stderr=%s" % (proc.returncode, stderr[:200]))

    non_json = []
    frames = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            non_json.append(line[:120])
            continue
        if isinstance(obj, dict) and "id" in obj:
            frames[obj["id"]] = obj

    check("stdout 只含 JSON-RPC 帧（无日志混入）", not non_json,
          "non-JSON lines: %s" % non_json[:2])
    check("stderr 有启动诊断（诊断不该走 stdout）", bool(stderr.strip()),
          "stderr empty")

    init = frames.get(1, {})
    result = init.get("result") or {}
    info = result.get("serverInfo") or {}
    check("initialize 应答存在", bool(result), json.dumps(init)[:200])
    check("serverInfo.name == phocinae-mcp", info.get("name") == "phocinae-mcp",
          repr(info.get("name")))
    version = info.get("version")
    check("serverInfo.version 非空", bool(version), repr(version))
    check("initialize 声明 protocolVersion", bool(result.get("protocolVersion")),
          repr(result.get("protocolVersion")))
    check("initialize 声明 tools 能力",
          "tools" in (result.get("capabilities") or {}),
          json.dumps(result.get("capabilities")))

    listing = frames.get(2, {})
    tools = ((listing.get("result") or {}).get("tools")) or []
    names = {t.get("name") for t in tools}
    check("tools/list 返回四个工具", names == EXPECTED_TOOLS,
          "got %s" % sorted(names))
    missing = [t.get("name") for t in tools
               if not t.get("inputSchema") or not t.get("description")]
    check("每个工具都有 inputSchema 与 description", not missing, missing)

    print()
    print("镜像 %s：serverInfo=%s version=%s tools=%s"
          % (IMAGE, info.get("name"), version, ",".join(sorted(names))))
    if fails:
        print("verify_docker: FAILED %d check(s)" % len(fails))
        return 1
    print("verify_docker: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
