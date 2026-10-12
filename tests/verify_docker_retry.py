#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_docker_retry.py — run tests/verify_docker.py, retrying past daemon flakes.

Docker Desktop on this machine intermittently refuses the named-pipe connection
("permission denied while trying to connect to the docker API at
npipe:////./pipe/dockerDesktopLinuxEngine") even while `docker info` answers.
That is an environment fault, not an image fault, so this wrapper waits for the
daemon to accept a trivial `docker version`, then runs the real check, and only
reports failure once the daemon is demonstrably up and the check still fails.
"""
import subprocess
import sys
import time


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


IMAGE = sys.argv[1] if len(sys.argv) > 1 else "phocinae-mcp:0.2.3"
ATTEMPTS = 12


def daemon_ok():
    try:
        p = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0 and bool(p.stdout.strip())


for attempt in range(1, ATTEMPTS + 1):
    if not daemon_ok():
        print("[%d/%d] daemon not answering yet; waiting 15s" % (attempt, ATTEMPTS),
              flush=True)
        time.sleep(15)
        continue
    print("[%d/%d] daemon up; running the image check" % (attempt, ATTEMPTS), flush=True)
    p = subprocess.run([sys.executable, "tests/verify_docker.py", IMAGE], timeout=600)
    if p.returncode == 0:
        print("verify_docker_retry: PASS", flush=True)
        sys.exit(0)
    print("[%d/%d] check failed with the daemon up; retrying" % (attempt, ATTEMPTS),
          flush=True)
    time.sleep(10)

print("verify_docker_retry: could not get a clean run (daemon flake)", flush=True)
sys.exit(1)
