#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""registry_entry_exists.py — 官方 MCP Registry 里是否已有该 name+version。

用途：让 .github/workflows/publish-registry.yml 具备幂等性。Registry 不允许
重复发布同一版本（"invalid version: cannot publish duplicate version"），
所以重推 tag 或修好后重跑时，必须先判断「已经有了」——那是前置条件已满足，
不是错误。

单独成文件的理由：这段逻辑原本内联在 workflow 的 run: 块里，里面有
python -c 的多行脚本与 shell 变量替换，既难以阅读，也容易把 YAML 块标量
写坏（实测导致 GitHub 直接以 422 拒绝 dispatch）。

用法：
    python scripts/registry_entry_exists.py io.github.Phocinae/phocinae-mcp 0.2.2
退出码：0 = 已存在；1 = 不存在；2 = 查询失败（网络/结构异常）

2 的处理方式：调用方（workflow）应把它当作「未知」并继续尝试发布，而不是
直接失败——发布本身才是权威判定。若真的重复，registry 会以 400 明确拒绝，
那时报错信息比「查询不通」更有用。
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

REGISTRY = "https://registry.modelcontextprotocol.io/v0/servers"


def fetch(name):
    """查询 registry；失败返回 None（调用方据此走「未知 → 继续发布」）。"""
    url = "%s?search=%s" % (REGISTRY, urllib.parse.quote(name, safe=""))
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                              "User-Agent": "phocinae-mcp"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 任何失败都只是「查不到」
        sys.stderr.write("registry query failed (%s: %s)\n"
                         % (type(exc).__name__, exc))
        return None


def main():
    if len(sys.argv) < 3:
        sys.stderr.write(__doc__)
        return 2
    name, version = sys.argv[1], sys.argv[2]
    data = fetch(name)
    if data is None:
        return 2
    hit = [s for s in data.get("servers", [])
           if s.get("server", {}).get("name") == name
           and s.get("server", {}).get("version") == version]
    if hit:
        meta = hit[0].get("_meta", {}).get(
            "io.modelcontextprotocol.registry/official", {})
        print("already on the registry: %s %s (status=%s, isLatest=%s)"
              % (name, version, meta.get("status"), meta.get("isLatest")))
        return 0
    print("not on the registry yet: %s %s" % (name, version))
    return 1


if __name__ == "__main__":
    sys.exit(main())
