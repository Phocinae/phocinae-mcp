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

IMAGE = sys.argv[1] if len(sys.argv) > 1 else "phocinae-mcp:0.2.1"
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
