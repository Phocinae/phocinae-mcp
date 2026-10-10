# phocinae-mcp

<!-- mcp-name: io.github.Phocinae/phocinae-mcp -->

斑海豹（Phocinae）审批/推理能力的 **MCP stdio server**：把 `phocinae-guard` 的命令审批门（gate）和斑海豹 P0 推理协议（classify / route / score）暴露成 MCP 工具，接入 Cline / Windsurf / Zed / Codex CLI 等仅支持 MCP 的编码代理。

- 纯标准库（Python ≥ 3.8），零第三方依赖；MCP 协议为手写 JSON-RPC 2.0 stdio 实现。
- 复用 phocinae-guard 的 `l0_judge` / `decide`（自动探测同级目录）；guard 不存在时 gate 自动降级为纯 HTTP `noul+score` 两问实现。
- fail-closed 语义：服务不可用时 **gate 返回 deny + 原因说明**，其余工具返回明确错误（MCP `isError:true`），绝不静默放行。
- 许可证：Apache-2.0（见 LICENSE）。

## 工具一览

| 工具 | 参数 | 返回 | fail-closed 行为 |
|---|---|---|---|
| `gate` | `command`（必填）、`cwd?`、`server?` | `decision ∈ allow\|deny\|ask`、`layer`（L0/L1/fail_closed）、`reason`、`noul`、`score` | 服务不可用/判定异常 → `decision=deny` + 原因 |
| `classify` | `state`（必填）、`labels`（必填，≥1 个非空字符串）、`server?` | `choice`（0 起下标）、`label`、`confidence` | 服务不可用 → 明确错误（isError） |
| `route` | `state`（必填）、`tools`（必填）、`server?` | `choice`、`tool`（选中的工具名） | 服务不可用 → 明确错误（isError） |
| `score` | `state`（必填）、`criteria`（必填）、`server?` | `score ∈ [2, 10]`、`range` | 服务不可用/应答越界 → 明确错误（isError） |

- `server?` 为工具级服务地址覆盖，等价于进程级 `--server`；地址解析顺序：工具参数 > `--server` CLI > `PHOCINAE_MCP_SERVER` env > `http://127.0.0.1:8155`。
- `gate` 判定链路：L0 确定性表（白名单/黑名单，本地、离线、毫秒级）→ 灰区时若启用 `--l1-enabled` 再调 `/v1/systemone` 两问（noul 放行概率 + score 风险 2–10，阈值 0.65 / 7 / 4 合成取最严格）；L1 未启用（默认：150M 权重未做命令审批域校准）则灰区直接 fail-closed deny。

## 服务协议（P0）

```
POST /v1/systemone
{"model": "Phocinae-Largha-150M-v1", "state": str,
 "questions": [{"id", "type": noul|choice|score, "options"?, "threshold"?}]}
→ {"model", "answers": {id: 值}, "usage", "answer_confidence"}
```

noul=bool、choice=int 下标、score=2–10。应答兼容扁平值与旧 mock 嵌套对象两种形状。

## Install

无需安装即可运行（仓库根目录执行）；也可本地 pip 安装：

```bash
cd phocinae-mcp
python3 -m phocinae_mcp --version          # phocinae-mcp 0.1.4
pip install -e .                            # 可选：提供 phocinae-mcp 命令
```

运行测试（子进程启动 server、全生命周期 JSON-RPC 帧、失败分支）：

```bash
python3 tests/test_mcp.py                   # 22 项
```

## 配置

| 项 | CLI | 环境变量 | 默认 |
|---|---|---|---|
| 服务地址 | `--server http://host:port` | `PHOCINAE_MCP_SERVER`（兼容 `PHOCINAE_GUARD_SERVER`） | `http://127.0.0.1:8155` |
| 请求超时 | `--timeout 5.0` | `PHOCINAE_MCP_TIMEOUT` | `5.0` |
| guard.py 路径 | `--guard /path/to/guard.py` | `PHOCINAE_MCP_GUARD_PATH`（兼容 `PHOCINAE_GUARD_PATH`） | 自动探测 `../phocinae-guard/guard.py` |
| 启用 L1 模型裁决 | `--l1-enabled` | `PHOCINAE_MCP_L1_ENABLED=1` | 关（灰区 fail-closed） |
| 模型名 | `--model` | `PHOCINAE_MCP_MODEL` | `Phocinae-Largha-150M-v1` |

## 客户端接入示例

**Cline / Windsurf（mcpServers）**

```json
{
  "mcpServers": {
    "phocinae": {
      "command": "python3",
      "args": ["-m", "phocinae_mcp", "--server", "http://127.0.0.1:8155"],
      "env": { "PHOCINAE_MCP_L1_ENABLED": "0" }
    }
  }
}
```

（若已 `pip install -e .`，`command` 可写 `phocinae-mcp`。）

**Zed（context_servers）**

```json
{
  "context_servers": {
    "phocinae": {
      "command": {
        "path": "python3",
        "args": ["-m", "phocinae_mcp", "--server", "http://127.0.0.1:8155"]
      }
    }
  }
}
```

**Codex CLI（~/.codex/config.toml）**

```toml
[mcp_servers.phocinae]
command = ["python3", "-m", "phocinae_mcp", "--server", "http://127.0.0.1:8155"]
```

完整示例见 `mcp_config.example.json`。

## 安全声明

1. **fail-closed 是硬语义**：服务不可用、超时、响应异常、应答字段缺失/越界时，`gate` 一律返回 `deny` 并附原因；`classify/route/score` 返回明确错误。任何情况下都不会在服务异常时静默放行。
2. **deny 不可覆盖**：`gate` 返回 `deny` 后无任何放行通道；`ask` 需人工确认（由宿主代理实现确认流程）。
3. **L1 默认关闭**：斑海豹 150M 权重尚未做命令审批域校准，`gate` 默认只用本地确定性 L0 表；`--l1-enabled` 是显式选择的实验开关。
4. **纯本地部署**：本仓库不包含任何发布/推送/上传逻辑，仅本地运行与本地测试；禁止将本服务暴露到公网。
5. 集成方应对 `gate` 的 `deny/ask` 结果做强制阻断（exit 非零 / 拒绝执行），并建议在宿主侧对判定追加审计日志（guard 侧自带 JSONL 审计）。

## License

Apache License 2.0 —— 见 [LICENSE](LICENSE)。
