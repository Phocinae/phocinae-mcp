#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""guard_bridge.py —— 复用 phocinae-guard/guard.py（L0 表 + 决策编排）。

探测顺序：
    1. --guard / PHOCINAE_MCP_GUARD_PATH / PHOCINAE_GUARD_PATH 显式路径；
    2. 同级仓库 <repo>/../phocinae-guard/guard.py（本仓默认布局）；
    3. 找不到 → 降级为纯 HTTP gate（tools.gate 的 noul+score 两问实现）。

导入时禁止写字节码缓存（只读复用 guard 目录，不落任何文件）。

stdout 纯净性（MCP stdio 硬约束：stdout 只允许 JSON-RPC 帧）：
guard.py 是外部文件，导入期可能向 stdout 打印——导入全程把 stdout
重定向到 stderr，杜绝任何杂质混入协议通道。
"""

import contextlib
import importlib.util
import io
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


@contextlib.contextmanager
def _stdout_to_stderr():
    """临时把 sys.stdout 重定向到 stderr（捕获 guard 导入期的任何打印）。"""
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = real_stdout


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
                with _stdout_to_stderr():   # stdout 纯净性：guard 打印进 stderr
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

    def l0_decide(self, command):
        """只用 L0 判一次。返回 (decision, reason, layer) 或 None（L0 说不了）。

        L0 来源优先级：
          1) guard.py（若在场）—— 与 guard 完全同源；
          2) 随包发行的 l0.py + l0_table.json —— 内容是从 guard 逐字抽取的，
             由 tests/test_l0_parity.py 保证不漂移。
        两者都没有（例如表被删除）才返回 None，交由上层问模型。
        """
        table = None
        if self.guard is not None:
            try:
                base = os.path.dirname(os.path.abspath(self.path))
                table = self.guard.load_l0_table(os.path.join(base, "l0_table.json"))
            except (OSError, ValueError, AttributeError):
                table = None
            if table:
                try:
                    return self.guard.l0_judge(command, table)
                except (Exception, SystemExit):
                    pass
        # 退回随包 L0
        try:
            from . import l0 as _l0
        except ImportError:  # pragma: no cover
            return None
        table = table or _l0.load_bundled_table()
        if not table:
            return None
        try:
            return _l0.l0_judge(command, table)
        except (Exception, SystemExit):
            return None

    def unavailable_reason(self):
        """guard.py 缺席时，说明缺的是哪一层以及怎么补。

        L0（离线确定性表）是 gate 的第一层，也是唯一能毫秒级判"明显安全/明显危险"
        的一层。少了它，每条命令都会去问 150M 模型，而模型在命令审批域没有校准，
        结果是良性命令也被拒。所以缺席时必须说出来，而不是照常给裁决。
        """
        return (
            "guard.py (the L0 deterministic layer) was not found, so the gate has "
            "no offline layer to decide with. Falling back to the model alone is "
            "not a usable gate: the 150M weights are not calibrated for command "
            "approval and return a harm score below the 'ask' threshold even for "
            "commands like 'git status', which yields deny for everything.\n"
            "Fix one of these:\n"
            "  1. pip install phocinae-guard, then point at its guard.py:\n"
            "       --guard /path/to/guard.py   (or set PHOCINAE_MCP_GUARD_PATH)\n"
            "  2. keep using a development checkout where ../phocinae-guard/guard.py "
            "exists.\n"
            "To accept model-only adjudication deliberately, set "
            "PHOCINAE_MCP_ALLOW_MODEL_ONLY_GATE=1.\n"
            "Underlying error: " + (self.error or "(none)")
        )

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
