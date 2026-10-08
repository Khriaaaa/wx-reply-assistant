# preflight.ps1 —— 启动前环境体检 / 自动补齐
#
# 由 Electron 主进程调用：
#   powershell -NoProfile -ExecutionPolicy Bypass -File preflight.ps1 \
#     -ResDir <resources 目录> -DataDir <%APPDATA%\wx-reply-assistant> -Port 8801 [-Fix]
#
# 不带 -Fix：只体检，报缺什么。
# 带  -Fix：缺什么就自己补什么 —— 自带运行时缺失就从官方源重下到 DataDir\runtime\，
#           微信装了没开就拉起来，端口被自己旧进程占着就清掉，数据目录没有就建。
# 输出：一行 JSON  {"items":[{id,name,state,detail}...],"blockers":N,"notes":[...]}
#       state: ok=没问题  fix=可自动处理（-Fix 时会处理）  fail=要人工
#
# 注意：本文件必须带 UTF-8 BOM，否则 PS 5.1 会按 ANSI 读，中文全乱。

param(
    [string]$ResDir  = '',
    [string]$DataDir = '',
    [int]$Port       = 8801,
    [switch]$Fix
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$RuntimeDir = Join-Path $DataDir 'runtime'
$PY_URL     = 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip'
$WA_API     = 'https://api.github.com/repos/microsoft/winappCli/releases/latest'
$UA         = 'wx-reply-assistant'

$items = New-Object System.Collections.ArrayList
$notes = New-Object System.Collections.ArrayList

function Add-Item([string]$id, [string]$name, [string]$state, [string]$detail) {
    [void]$items.Add([ordered]@{ id = $id; name = $name; state = $state; detail = $detail })
}
function Note([string]$s) { [void]$notes.Add($s) }

# ---------------------------------------------------------------- 下载
function Fetch([string]$url, [string]$dst) {
    try {
        $d = Split-Path $dst -Parent
        if ($d -and -not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
        $wc = New-Object Net.WebClient
        $wc.Headers.Add('User-Agent', $UA)
        $wc.DownloadFile($url, $dst)
        if ((Test-Path $dst) -and ((Get-Item $dst).Length -gt 0)) { return $true }
        Note "下载落地是空的：$url"
        return $false
    } catch {
        Note "下载失败 $url —— $($_.Exception.Message)"
        return $false
    }
}

# ---------------------------------------------------------------- Python
function Test-Py([string]$p) {
    if (-not $p -or -not (Test-Path $p)) { return '' }
    try {
        $v = & $p -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>&1 | Select-Object -First 1
    } catch { return '' }
    if ("$v" -match '^\d+\.\d+\.\d+$') { return "$v" }
    return ''
}

$pyPath = Join-Path $ResDir 'python\python.exe'
$pyVer  = Test-Py $pyPath
if (-not $pyVer) {
    $alt = Join-Path $RuntimeDir 'python\python.exe'
    $v2  = Test-Py $alt
    if ($v2) { $pyVer = $v2; $pyPath = $alt }
}
if ($pyVer) {
    Add-Item 'python' '自带 Python 运行时' 'ok' "$pyVer"
} else {
    $done = $false
    if ($Fix) {
        $zip = Join-Path $env:TEMP 'wxreply-py.zip'
        if (Fetch $PY_URL $zip) {
            $dst = Join-Path $RuntimeDir 'python'
            if (Test-Path $dst) { Remove-Item $dst -Recurse -Force -ErrorAction SilentlyContinue }
            New-Item -ItemType Directory -Path $dst -Force | Out-Null
            try { Expand-Archive -Path $zip -DestinationPath $dst -Force } catch { Note "解压 Python 失败 —— $($_.Exception.Message)" }
            Remove-Item $zip -Force -ErrorAction SilentlyContinue
            $v3 = Test-Py (Join-Path $dst 'python.exe')
            if ($v3) { Add-Item 'python' '自带 Python 运行时' 'ok' "已补齐 $v3"; $done = $true }
        }
    }
    if (-not $done) { Add-Item 'python' '自带 Python 运行时' 'fail' '没找到，也没能补上 —— 重装一遍安装包' }
}

# ---------------------------------------------------------------- winapp
$waPath = Join-Path $ResDir 'winapp\winapp.exe'
if (-not (Test-Path $waPath)) {
    $alt = Join-Path $RuntimeDir 'winapp\winapp.exe'
    if (Test-Path $alt) { $waPath = $alt }
}
if (Test-Path $waPath) {
    Add-Item 'winapp' '自带 winapp（读微信界面用）' 'ok' $waPath
} else {
    $done = $false
    if ($Fix) {
        try {
            $rel = Invoke-RestMethod -Uri $WA_API -Headers @{ 'User-Agent' = $UA } -TimeoutSec 25
            $asset = $rel.assets | Where-Object { $_.name -match 'x64.*\.zip$' } | Select-Object -First 1
            if (-not $asset) { $asset = $rel.assets | Where-Object { $_.name -match '\.zip$' } | Select-Object -First 1 }
            if ($asset) {
                $zip = Join-Path $env:TEMP 'wxreply-winapp.zip'
                if (Fetch $asset.browser_download_url $zip) {
                    $dst = Join-Path $RuntimeDir 'winapp'
                    New-Item -ItemType Directory -Path $dst -Force | Out-Null
                    try { Expand-Archive -Path $zip -DestinationPath $dst -Force } catch { Note "解压 winapp 失败 —— $($_.Exception.Message)" }
                    Remove-Item $zip -Force -ErrorAction SilentlyContinue
                    $found = Get-ChildItem $dst -Recurse -Filter 'winapp.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
                    if ($found) { Add-Item 'winapp' '自带 winapp（读微信界面用）' 'ok' "已补齐 $($found.FullName)"; $done = $true }
                }
            } else { Note 'GitHub 上没找到 winapp 的 zip 资产' }
        } catch { Note "查 winapp 发布失败 —— $($_.Exception.Message)" }
    }
    if (-not $done) { Add-Item 'winapp' '自带 winapp（读微信界面用）' 'fail' '没找到，也没能补上 —— 重装一遍安装包' }
}

# ---------------------------------------------------------------- 程序文件
$appPy = Join-Path $ResDir 'py\assistant.py'
if (Test-Path $appPy) {
    Add-Item 'app' '程序文件' 'ok' 'py\assistant.py 在位'
} else {
    Add-Item 'app' '程序文件' 'fail' '安装目录里没有 py\assistant.py —— 重装一遍安装包'
}

# ---------------------------------------------------------------- 微信客户端
$wxCands = @()
foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
    if ($root) {
        $wxCands += (Join-Path $root 'Tencent\Weixin\Weixin.exe')
        $wxCands += (Join-Path $root 'Tencent\WeChat\WeChat.exe')
    }
}
$wx = $wxCands | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $wx) {
    $keys = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )
    $hit = Get-ItemProperty $keys -ErrorAction SilentlyContinue |
           Where-Object { $_.DisplayName -match '微信|WeChat|Weixin' } | Select-Object -First 1
    if ($hit -and $hit.DisplayIcon) {
        $c = ($hit.DisplayIcon -split ',')[0].Trim('"').Trim()
        if ($c -and (Test-Path $c)) { $wx = $c }
    }
}
if (-not $wx) {
    Add-Item 'wechat' '微信客户端' 'fail' '这台机器没装微信 —— 先装并登录（weixin.qq.com），采集才有对象'
} else {
    $p = Get-Process -Name Weixin, WeChat -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($p) {
        Add-Item 'wechat' '微信客户端' 'ok' "已在运行（#$($p.Id)）"
    } elseif ($Fix) {
        try {
            Start-Process -FilePath $wx
            Start-Sleep -Seconds 4
            $p2 = Get-Process -Name Weixin, WeChat -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($p2) { Add-Item 'wechat' '微信客户端' 'ok' "已启动（#$($p2.Id)）" }
            else { Add-Item 'wechat' '微信客户端' 'fail' "装了但没起来：$wx" }
        } catch { Add-Item 'wechat' '微信客户端' 'fail' "起不来 —— $($_.Exception.Message)" }
    } else {
        Add-Item 'wechat' '微信客户端' 'fix' "装了但没开：$wx"
    }
}

# ---------------------------------------------------------------- 数据目录
if (Test-Path $DataDir) {
    Add-Item 'datadir' '数据目录可写' 'ok' $DataDir
} else {
    $done = $false
    if ($Fix) {
        try { New-Item -ItemType Directory -Path $DataDir -Force | Out-Null; $done = $true } catch { Note "建数据目录失败 —— $($_.Exception.Message)" }
    }
    if ($done) { Add-Item 'datadir' '数据目录可写' 'ok' "已建 $DataDir" }
    elseif (Test-Path $DataDir) { Add-Item 'datadir' '数据目录可写' 'ok' $DataDir }
    else { Add-Item 'datadir' '数据目录可写' 'fail' "建不了 $DataDir" }
}

# ---------------------------------------------------------------- 端口
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $conn) {
    Add-Item 'port' "面板端口 $Port" 'ok' '空着'
} else {
    $owner = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    $mine = $false
    if ($owner) {
        if ($owner.ProcessName -match '^(python|pythonw)$') { $mine = $true }
        elseif ($owner.Path -and $ResDir -and $owner.Path.ToLower().StartsWith($ResDir.ToLower())) { $mine = $true }
    }
    if ($mine -and $Fix) {
        try { Stop-Process -Id $owner.Id -Force -ErrorAction Stop; Add-Item 'port' "面板端口 $Port" 'ok' "已清掉上次残留的 $($owner.ProcessName) #$($owner.Id)" }
        catch { Add-Item 'port' "面板端口 $Port" 'fail' "清不掉 $($owner.ProcessName) #$($owner.Id)" }
    } elseif ($mine) {
        Add-Item 'port' "面板端口 $Port" 'fix' "被上次残留的 $($owner.ProcessName) #$($owner.Id) 占着"
    } else {
        $nm = if ($owner) { "$($owner.ProcessName) #$($owner.Id)" } else { "PID $($conn.OwningProcess)" }
        Add-Item 'port' "面板端口 $Port" 'fail' "被别的程序占着（$nm）—— 关掉它，或用 WXREPLY_PORT 换端口"
    }
}

# ---------------------------------------------------------------- VC++ 运行库
$vc = @('vcruntime140.dll', 'vcruntime140_1.dll') | Where-Object { -not (Test-Path (Join-Path $env:SystemRoot "System32\$_")) }
if ($vc.Count -eq 0) {
    Add-Item 'vcrun' 'VC++ 运行库' 'ok' '在'
} else {
    Add-Item 'vcrun' 'VC++ 运行库' 'fail' ("缺 " + ($vc -join '、') + " —— 装一下 Microsoft Visual C++ 2015-2022 可再发行组件（x64）")
}

# ---------------------------------------------------------------- 输出
$bad = @($items | Where-Object { $_.state -eq 'fail' })
$out = [ordered]@{
    items    = $items
    blockers = $bad.Count
    fixed    = @($items | Where-Object { $_.state -eq 'fix' }).Count
    notes    = $notes
}
$out | ConvertTo-Json -Depth 6 -Compress
