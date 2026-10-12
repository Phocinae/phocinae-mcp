#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""phocinae-mcp —— 斑海豹 MCP server（纯标准库，Python >= 3.8）。

双代协议：
  - MCP 2026-07-28（现代：server/discover、_meta、resultType、ttlMs/cacheScope、
    MRTR elicitation、Streamable HTTP）；
  - MCP 2024-11-05（旧客户端 initialize 握手，兼容保留）。

传输：stdio（默认）+ Streamable HTTP（--transport http，仅回环绑定）。
本包为本地增强版，不发布到 PyPI。
"""

__version__ = "0.2.2"
__all__ = ["__version__"]
