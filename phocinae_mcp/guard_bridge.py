#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""guard_bridge.py —— 复用 phocinae-guard/guard.py（L0 表 + 决策编排）。

探测顺序：
    1. --guard / PHOCINAE_MCP_GUARD_PATH / PHOCINAE_GUARD_PATH 显式路径；
    2. 同级仓库 <repo>/../phocinae-guard/guard.py（本仓默认布局）；
    3. 找不到 → 降级为纯 HTTP gate（tools.gate 的 noul+score 两问实现）。

导入时禁止写字节码缓存（只读复用 guard 目录，不落任何文件）。
"""

import importlib.util
import os
import sys


def locate_guard():
    """返回 guard.py 绝对路径；找不到返回空串。"""
    path = (os.environ.get("PHOCINAE_MCP_GUARD_PATH")
            or os.environ.get("PHOCINAE_GUARD_PATH") or "").strip()
    if path:
        return path if os.path.isfile(path) else ""
    here = os.path.dirname(os.path.abspath(__file__))   # .../phocinae-mcp/phocinae_mcp
    repo_root = os.path.dirname(here)                   # .../phocinae-mcp
    dev_root = os.path.dirname(repo_root)               # .../dev
    for cand in (
        os.path.join(dev_root, "phocinae-guard", "guard.py"),
        os.path.join(repo_root, "phocinae-guard", "guard.py"),
        os.path.join(repo_root, "..", "phocinae-guard", "guard.py"),
    ):
        if os.path.isfile(cand):
            return cand
    return ""


class GuardBridge:
    """guard.py 的动态 import 封装；加载失败自动降级（available=False）。"""

    def __init__(self, path=None):
        self.path = path if path is not None else locate_guard()
        self.guard = None
        self.error = ""
        if not self.path:
            self.error = ("未找到 guard.py（--guard / PHOCINAE_MCP_GUARD_PATH 未设置，"
                          "同级 ../phocinae-guard/guard.py 不存在）→ gate 降级为纯 HTTP "
                          "noul+score 两问")
            return
        try:
            spec = importlib.util.spec_from_file_location(
                "_phocinae_guard_bridge", self.path)
            module = importlib.util.module_from_spec(spec)
            old_flag = sys.dont_write_bytecode
            sys.dont_write_bytecode = True  # 复用 guard 目录时不写 __pycache__
            try:
                spec.loader.exec_module(module)
            finally:
                sys.dont_write_bytecode = old_flag
            self.guard = module
        except Exception as exc:  # guard.py 语法错误/依赖缺失等
            self.error = ("guard.py 加载失败（%s: %s）→ gate 降级为纯 HTTP "
                          "noul+score 两问" % (type(exc).__name__, exc))
            self.guard = None

    @property
    def available(self):
        return self.guard is not None

    def make_cfg(self, server, timeout, l1_enabled):
        """按 guard.load_config 约定构造配置。

        fail_closed 恒为 deny（MCP gate 规范：服务不可用 → deny+说明）；
        table 留空 → 使用 guard 同目录 l0_table.json。
        """
        return self.guard.load_config({
            "server": server,
            "timeout": timeout,
            "l1_enabled": bool(l1_enabled),
            "fail_closed": "deny",
            "table": "",
        })

    def decide(self, command, cwd, server, timeout, l1_enabled):
        """复用 guard.decide 全流程：L0 确定性表 → （l1_enabled 时）L1 两问
        → fail-closed deny。返回 guard 的结果 dict（decision/layer/reason/...）。
        """
        cfg = self.make_cfg(server, timeout, l1_enabled)
        cfg["l0_table_data"] = self.guard.load_l0_table(cfg["l0_table"])
        return self.guard.decide(command, cwd, cfg)
