# CHANGES

## 0.2.1 — 2026-10-11

- README 顶部加入 MCP Registry 的归属令牌 `mcp-name: io.github.Phocinae/phocinae-mcp`。
  官方 Registry 在发布时要求 PyPI 包的 README 里出现该令牌；0.2.0 的 metadata 已不可变，
  故以 0.2.1 补上（代码与 0.2.0 完全一致，仅 README 与版本号变化）。

# CHANGES —— 相对 PyPI phocinae-mcp 0.1.1 的增强清单

基线：`_orig/`（phocinae_mcp-0.1.1-py3-none-any.whl 解包，纯标准库 stdio
MCP server，协议 2024-11-05）。增强版：`phocinae_mcp/`（v0.2.0）。

## 逐文件 diff 摘要

| 文件 | 变化 |
|------|------|
| `server.py` | 大改。双代并存：`_era_of()` 按请求分派（initialize→旧年代；server/discover、resources/*→现代；其余看 `params._meta` 是否含 `io.modelcontextprotocol/*`）。新增 `_handle_modern`/`_dispatch_modern`：server/discover、_meta 强制校验（缺 protocolVersion/clientCapabilities → -32602；版本不支持 → -32022 + data.supported/requested）、tools/list 带 ttlMs/cacheScope、resources/list+read、现代面下 initialize/ping/shutdown 等已移除方法 → -32601、通知 → 无响应。`call_tool` 双形状（现代：resultType+structuredContent；旧：原版形状）。gate ask → `_gate_ask_input_required`（elicitation/create）+ `_gate_mrtr_retry`（HMAC requestState 校验、accept/decline/cancel）。run 循环加逐帧兜底（-32603，日志走 stderr，循环不退出）。CLI 新增 `--transport stdio|http`、`--host`（回环白名单）、`--port`（0=随机）。read_frame 兼容 Content-Length 帧。 |
| `tools.py` | 大改。gate/classify/route/score 逻辑不变（与原版同口径），新增：长度上限常量与 `_require_str`/`_require_str_list` 服务端校验（越限 -32602）；TOOL_DEFS 收紧（maxLength/minItems/maxItems/enum/const、additionalProperties:false、title、outputSchema 全量声明）；四工具 description 按「何时使用/何时不使用」重写。 |
| `protocol.py` | **新增**。2026-07-28 共享件：MODERN/LEGACY 版本常量、错误码重编号（-32020/-32021/-32022）、complete/input_required 结果构造、extract_meta、unsupported_version_error、HMAC-SHA256 requestState 编解码（防篡改+过期+指纹）、ttlMs/cacheScope 常量、stderr 日志门控（支持 _meta logLevel）、审批策略资源文本 POLICY_TEXT/URI/NAME/MIME。 |
| `http_transport.py` | **新增**。Streamable HTTP：回环限定 ThreadingHTTPServer；Server Validation（MCP-Protocol-Version/Mcp-Method/Mcp-Name/Accept → 400 -32020/-32022、406）；通知 202 空 body；未知方法 404 -32601；GET/DELETE/OPTIONS 405；无会话/无 SSE 缓冲；日志只走 stderr；SIGTERM 优雅退出。 |
| `guard_bridge.py` | 改。原逻辑保留；新增 `_stdout_to_stderr` 上下文：guard.py 导入期 stdout 重定向到 stderr（stdlib 纯净性）；其余（路径探测、load_config、decide）同原版。 |
| `__init__.py` | 版本 0.1.1 → 0.2.0；文档说明双代+双传输。 |
| `__main__.py` | 不变。 |
| `backend.py` | **原版不动**（逐字节一致）。 |
| `README.md` | 重写：双传输用法、协议对齐表、双代分派规则、测试说明、安全声明。 |
| `pyproject.toml` | 新增（0.2.0，stdlib-only，entry point phocinae-mcp）。 |
| `tests/` | **新增**：mock_systemone.py（确定性 mock P0）、test_mcp.py（stdio 双代全流程 42 项断言 → mcp_smoke.txt）、test_http.py（HTTP 头矩阵+MRTR 18 项 → http_smoke.txt）。 |
| `LICENSE` | 原版 Apache-2.0 复制。 |

## 原版已存在的协议缺口（本版修复）

1. 无 `server/discover`（2026-07-28 必实现）→ 已实现（含 supportedVersions/
   capabilities/serverInfo/instructions + ttlMs/cacheScope）。
2. 无逐请求 `_meta` 校验 → 已实现（-32602 缺失 / -32022 版本协商）。
3. `tools/list` 无 `ttlMs`/`cacheScope` → 已实现（5 分钟/public）。
4. 结果无 `resultType` → 全部结果补齐（complete/input_required）。
5. `gate` 的 `ask` 是普通文本结果、无人工确认交互语义 → 现代面接 MRTR：
   elicitation/create + HMAC requestState + accept/decline/cancel 回执
   （未声明 elicitation 或旧年代时回退为原版行为）。
6. 审批策略只存在于 README → 转为 resources（list/read，text/markdown）。
7. 无 Streamable HTTP 传输 → 新增（stdio 并存，仅回环）。
8. 工具 schema 无边界（长度无上限、无 enum/outputSchema）→ 收紧并服务端
   同步校验（越限 -32602）。
9. description 未按「何时用/何时不用」组织 → 四工具全部重写。
10. stdout 纯净性无系统保障（guard.py 导入打印可能污染协议通道）→
    全链路审计：日志门控只走 stderr、guard 导入 stdout→stderr、
    测试逐行校验 stdout 每行均为 JSON-RPC 帧。
11. 已移除方法（ping/shutdown/initialize 现代面等）未按新版返回 -32601
    → 现代面统一 -32601（data 附 supportedVersions）。

## 协议细节实现备注

- **年代分派**是「按请求」而非「按连接」：同一 stdio 连接上可先 initialize
  （旧）再发带 _meta 的 discover（现代）。这是双代兼容的唯一可行路径，
  也是 README 明示的行为契约。
- **-32021（MissingRequiredClientCapability）**：定义但当前不触发——gate 的
  ask 在客户端未声明 elicitation 时**回退**为普通 complete 结果而非报错
  （对 2025-06-18 时代的客户端更友好）。如需严格模式可把 `call_tool` 中
  该分支改为 `raise McpError(-32021, ...)`。
- **requestState 安全**：base64url(payload).HMAC-SHA256；payload 含
  exp（10 分钟）+ tool/command 指纹；每次进程启动随机 32 字节密钥
  （重启后旧 state 自然失效）；长度上限 40KB。
- **stdio 帧格式**：换行分隔单行 JSON（`ensure_ascii=False`，文本内含换行
  以 \n 转义，帧本身恒单行）；read_frame 额外兼容 LSP Content-Length 帧。
- **HTTP 状态码映射**：-32601→404；-32020/-32022/-32700/-32600/-32602→400；
  -32603→500；通知→202；工具执行失败（result.isError）仍为 200。
- **缓存提示**：discover/tools/list/resources 三者为进程内静态内容，
  ttlMs 分别为 3600000/300000/3600000，cacheScope=public。

## 未做（有意）

- 不发布（无 pypi publish / git push / 联网上传）。
- 不引入 fastmcp/python-sdk 依赖：保持纯标准库（原包核心卖点）。
- 不做 SSE 流式/会话恢复：2026-07-28 已移除会话模型，无状态 POST 已够。
- resources 仅静态策略文本；不做 subscribe（需订阅能力声明，价值低）。
