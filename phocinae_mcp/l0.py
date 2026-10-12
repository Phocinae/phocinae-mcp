# -*- coding: utf-8 -*-
"""L0 确定性判定层 —— 从 phocinae-guard 逐字抽取函数体，随本包一起发行。

为什么随包发行：没有 L0，gate 的每条命令都会去问 150M 模型，而模型在命令审批
域没有校准（guard 自己的 README 就写了这点），实测连 `git status` 都拿到
score=3.0（低于 ask 阈 4.0）而被拒 —— 普通 pip 安装后工具直接不可用。
把 5.6KB 的表和判定逻辑带上，任何安装都可用。

为什么是"抽取"而不是"重写"：重写就等于两份实现，guard 更新表语义时这份会悄悄
分叉。这里的函数体与 guard.py 逐字一致，并且 tests/test_l0_parity.py 用共享语料
逐条比对两份实现对同一命令的裁决 —— 一旦漂移，测试立刻红。

来源：phocinae-guard（Apache-2.0）。抽取脚本：tools/bundle_l0_into_mcp.py
"""

import json
import os
import re

_TABLE_CACHE = {}


def load_bundled_table():
    """读随包发行的 l0_table.json；缺失或损坏时返回 None（调用方据此降级）。"""
    if "table" in _TABLE_CACHE:
        return _TABLE_CACHE["table"]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "l0_table.json")
    try:
        with open(path, encoding="utf-8") as fh:
            table = json.load(fh)
    except (OSError, ValueError):
        table = None
    _TABLE_CACHE["table"] = table
    return table


_ESC_NL = '\x00esc_nl\x00'
_LIT_NL = '\x00lit_nl\x00'
_SEPARATORS = ('&&', '||', ';', '|')


def split_commands(cmd):
    """把命令行按**未转义、未加引号**的 `&&` `||` `;` `|` 拆成多段。

    用于「逐段过黑名单」：`ls ; \\rm -rf /` 的第二段会被单独看到。
    引号内的分隔符（`echo "a|b"`）与反斜杠转义的分隔符保持原样，
    避免把合法命令误判成多段。
    """
    segs = []
    buf = []
    quote = None
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if quote:
            # 引号内：只有配对的引号能关闭引用，其余原样保留
            if ch == quote:
                quote = None
            buf.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\":
            # 转义：字符原样保留，且不参与分隔符识别（`rm\ -rf` / `a\|b`）
            if i + 1 < n:
                buf.append(ch)
                buf.append(cmd[i + 1])
                i += 2
            else:
                buf.append(ch)
                i += 1
            continue
        sep = None
        for cand in _SEPARATORS:
            if cmd.startswith(cand, i):
                sep = cand
                break
        if sep:
            segs.append("".join(buf))
            buf = []
            i += len(sep)
            continue
        buf.append(ch)
        i += 1
    segs.append("".join(buf))
    return [seg.strip() for seg in segs if seg.strip()]


def _split_command_segments(cmd):
    """按段返回命令：新行（normalize 后为 ` ; `）与 `;`/`&&`/`||`/`|` 均分段。"""
    return split_commands(cmd)


def _map_unquoted(text, fn, pair_escapes=True):
    """对 `text` 中**引号外**的字符跑 `fn(ch)`，引号内原样保留。

    反斜杠转义对（`\\x`）整体保留，避免把被转义的引号当成引号边界。
    """
    out = []
    quote = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
            out.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if pair_escapes and ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(text[i + 1])
            i += 2
            continue
        out.append(fn(ch))
        i += 1
    return "".join(out)


def normalize(cmd):
    """归一化**但不破坏命令边界**：
      - 转义换行（`\\` + 换行）与字面 `\\n` 保留，不作为分隔符；
      - `\\r\\n` / `\\n` / `\\r` → ` ; `（换行 = 新命令，不是普通空白）；
      - 引号外、未转义的制表符也视为命令分隔 → ` ; `（`ls\\trm -rf /` 是两条
        命令，旧实现把它折叠成 `ls rm -rf /`，正好绕开锚定 `(^|[;&|]\\s*)` 的规则）；
      - 其余行内空白（空格/多空格）折叠为一个空格。

    注意：换行若折叠成空格，`ls\\nrm -rf /` 会变成 `ls rm -rf /`，黑名单里
    锚定 `(^|[;&|]\\s*)` 的 `rm` 规则就再也匹配不上——旧实现正是这样被绕过的。
    """
    if not isinstance(cmd, str):
        cmd = str(cmd)
    cmd = (cmd.replace("\x00", "")
              .replace("\\\r\n", _ESC_NL)
              .replace("\\\n", _ESC_NL)
              .replace("\\n", _LIT_NL))
    cmd = re.sub(r"\r\n|\r|\n", " ; ", cmd)
    cmd = _map_unquoted(cmd, lambda ch: " ; " if ch == "\t" else ch)
    text = re.sub(r"[ \f\v]+", " ", cmd).strip()
    return text.replace(_ESC_NL, "\\\n").replace(_LIT_NL, "\\n")


def _snippet(text, limit=160):
    """reason 里回显命中片段时截断，避免 100KB 命令把审计/日志撑爆。"""
    text = text.replace("\n", "\\n").replace("\r", "\\r")
    if len(text) <= limit:
        return text
    return text[:limit] + "…(%d 字符已截断)" % (len(text) - limit)


def _blacklist_candidates(cmd, segs):
    """黑名单要检查的候选串：原串 + 归一化串 + 每一段（去前导 `\\`）。"""
    out = [cmd]
    norm = normalize(cmd)
    if norm and norm not in out:
        out.append(norm)
    for seg in segs or []:
        out.append(seg)
        # shell 里 `\\rm` 与 `rm` 等价（还有 `\\\\rm`），去掉前导转义再查一遍
        stripped = seg
        while stripped.startswith("\\"):
            stripped = stripped[1:]
        if stripped and stripped not in out:
            out.append(stripped)
    return out


def _blacklist_hit(table, candidates):
    for name, pattern, note in table["blacklist"]:
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            sys.stderr.write("guard: bad L0 pattern %s (%s): %s\n"
                             % (name, pattern, exc))
            continue
        for text in candidates:
            if text and rx.search(text):
                return name, note, text
    return None


def _segment_hit(text, table):
    """单段是否命中白名单（精确或前缀）。命中返回理由，否则 None。"""
    if text in table["whitelist"]:
        return "L0 白名单精确命中"
    for prefix in sorted(table["whitelist_prefix"], key=len, reverse=True):
        if text == prefix or text.startswith(prefix + " "):
            return "L0 白名单前缀命中：%s" % prefix
    return None


def l0_judge(cmd, table):
    """L0 判定：黑名单（全串 + 逐段）优先，之后白名单**逐段**校验。

    旧实现只看归一化后的整串且只匹配首 token，于是
    `ls\\nrm -rf /`、`cat x | "sh"`、`echo pwned>/etc/passwd` 都会被
    首 token 白名单放行。现在：
      1) 黑名单同时对原串、归一化串、每一段（含去前导 `\\`）跑；
      2) 只有在黑名单全不中的前提下才看白名单；
      3) 白名单要求**每一段**都命中——第 1 段命中不能给第 2 段背书。
    """
    segs = _split_command_segments(normalize(cmd))
    if not segs:
        return "deny", "空命令", "L0"
    hit = _blacklist_hit(table, _blacklist_candidates(cmd, segs))
    if hit is not None:
        name, note, matched = hit
        return ("deny", "L0 黑名单 %s：%s（命中：%s）"
                % (name, note, _snippet(matched)), "L0")
    for seg in segs:
        reason = _segment_hit(seg, table)
        if reason is None:
            return None, "L0 不能定夺", "L0"
    return "allow", "L0 白名单逐段命中（%s）" % segs[0], "L0"

# ------------------------------------------------------------------ L1：/v1/systemone 两问
