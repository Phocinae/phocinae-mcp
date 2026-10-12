# verify_docker.ps1 — verify the container against the directory-check criterion.
#
# Why PowerShell rather than the Python verifier in the same folder: on this host
# `docker` cannot open its named pipe when a Python process captures stdio, so
# `docker run` from Python fails with "permission denied while trying to connect
# to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine" while the same
# command from PowerShell succeeds. The criterion being checked is identical.
#
# It asserts what a container-based MCP directory (Glama and similar) checks:
# the server starts and answers introspection requests, and stdout carries only
# JSON-RPC frames.
#
# Usage:  pwsh -File tests/verify_docker.ps1 [-Image phocinae-mcp:0.2.1]

param([string]$Image = "phocinae-mcp:0.2.1")

$ErrorActionPreference = "Continue"
$fails = @()

function Check($name, $cond, $detail) {
    if ($cond) {
        Write-Output "PASS $name"
    } else {
        Write-Output "FAIL $name  $detail"
        $script:fails += $name
    }
}

# --- daemon reachable -------------------------------------------------------
$ver = (docker version --format "{{.Server.Version}}" 2>&1 | Out-String).Trim()
Check "docker daemon reachable" ($LASTEXITCODE -eq 0 -and $ver -match '^\d+\.\d+') "got [$ver]"

# --- image present ----------------------------------------------------------
$imgs = (docker images --format "{{.Repository}}:{{.Tag}}" 2>&1 | Out-String)
Check "image $Image exists" ($imgs -match [regex]::Escape($Image)) "images: $($imgs.Trim())"

# --- drive the container over stdio ----------------------------------------
$reqs = @(
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"directory-probe","version":"1"}}}',
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
) -join "`n"

$payloadFile = Join-Path $env:TEMP "phocinae-mcp-probe.jsonl"
$stdoutFile = Join-Path $env:TEMP "phocinae-mcp-probe.out"
$stderrFile = Join-Path $env:TEMP "phocinae-mcp-probe.err"
[System.IO.File]::WriteAllText($payloadFile, $reqs + "`n", [System.Text.Encoding]::ASCII)

# stdout and stderr are captured to separate files on purpose. The MCP stdio
# contract says stdout carries ONLY JSON-RPC frames and every diagnostic goes to
# stderr, so the two must not be merged before the frame-hygiene check below.
cmd /c "docker run -i --rm $Image < `"$payloadFile`" > `"$stdoutFile`" 2> `"$stderrFile`""
$rc = $LASTEXITCODE

$stdout = if (Test-Path $stdoutFile) { [System.IO.File]::ReadAllText($stdoutFile) } else { "" }
$stderr = if (Test-Path $stderrFile) { [System.IO.File]::ReadAllText($stderrFile) } else { "" }
$raw = $stdout
Remove-Item $payloadFile, $stdoutFile, $stderrFile -Force -ErrorAction SilentlyContinue

Check "container exits 0 on stdin EOF" ($rc -eq 0) "rc=$rc stderr=$($stderr.Substring(0,[Math]::Min(200,$stderr.Length)))"

# --- frame hygiene ---------------------------------------------------------
# stdout must be pure JSON-RPC. A single stray log line on stdout breaks every
# MCP client, which is exactly the defect 0.2.1 fixed for non-UTF-8 locales.
$lines = $stdout -split "`n" | Where-Object { $_.Trim() -ne "" }
$bad = @()
$frames = @{}
foreach ($l in $lines) {
    $t = $l.Trim()
    if ($t -match '^\{') {
        try { $o = $t | ConvertFrom-Json } catch { $bad += $t; continue }
        if ($o.PSObject.Properties.Name -contains 'id') { $frames[[string]$o.id] = $o }
    } else {
        $bad += $t
    }
}
Check "stdout carries only JSON-RPC frames" ($bad.Count -eq 0) ($bad -join ' | ')
Check "stderr carries the startup diagnostics" ($stderr -match 'phocinae-mcp\[') "stderr empty or unexpected"

# --- initialize ------------------------------------------------------------
$init = $frames['1']
Check "initialize answered" ($null -ne $init) "frames=$($frames.Keys -join ',')"
if ($init) {
    $info = $init.result.serverInfo
    Check "serverInfo.name == phocinae-mcp" ($info.name -eq 'phocinae-mcp') "got $($info.name)"
    Check "serverInfo.version non-empty" ([bool]$info.version) "got $($info.version)"
    Check "protocolVersion declared" ([bool]$init.result.protocolVersion) "got $($init.result.protocolVersion)"
    Check "tools capability declared" ($null -ne $init.result.capabilities.tools) "caps=$($init.result.capabilities | ConvertTo-Json -Compress)"
}

# --- tools/list ------------------------------------------------------------
$list = $frames['2']
Check "tools/list answered" ($null -ne $list)
if ($list) {
    $names = @($list.result.tools | ForEach-Object { $_.name })
    $want = @('gate', 'classify', 'route', 'score')
    $missing = @($want | Where-Object { $names -notcontains $_ })
    Check "tools/list returns gate/classify/route/score" ($missing.Count -eq 0) "got [$($names -join ',')]"
    $incomplete = @($list.result.tools | Where-Object { -not $_.inputSchema -or -not $_.description } | ForEach-Object { $_.name })
    Check "every tool has inputSchema and description" ($incomplete.Count -eq 0) ($incomplete -join ',')
}

Write-Output ""
Write-Output "image: $Image"
if ($fails.Count -gt 0) {
    Write-Output "verify_docker: FAILED $($fails.Count) check(s): $($fails -join '; ')"
    exit 1
}
Write-Output "verify_docker: all checks passed"
exit 0
