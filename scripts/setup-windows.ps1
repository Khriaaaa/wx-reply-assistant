# setup-windows.ps1 —— Windows 侧环境体检 / 补齐（微信跑在这台机器上时）
#
# 用法：在 Windows 上以管理员身份打开 PowerShell，然后
#   powershell -NoProfile -ExecutionPolicy Bypass -File setup-windows.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File setup-windows.ps1 -Auto
#
#   不加 -Auto：只体检，缺什么打印一条装它的命令，不动系统
#   加   -Auto：缺什么就装什么（下载 PsExec64 / winapp CLI），并关掉睡眠
#
# 注：这个脚本本身不需要在交互会话里跑；但 UIA 相关的命令（winapp ui ...）
#     必须注入到用户登录的 session 1 才有内容 —— 见 INSTALL.md 3.3。
param(
    [string]$WorkDir   = 'C:\dl\wxc',
    [string]$WinappDir = 'C:\winapp-cli',
    [string]$Psexec    = 'C:\dl\PsExec64.exe',
    [switch]$Auto
)

$ErrorActionPreference = 'Continue'
$rows = @()

function Add-Row([string]$Item, [string]$State, [string]$Detail) {
    $script:rows += [pscustomobject]@{ 项目 = $Item; 状态 = $State; 说明 = $Detail }
}

function Write-Step([string]$msg) { Write-Host "`n=== $msg" -ForegroundColor Cyan }

# ---------------------------------------------------------------- 会话
Write-Host '微信回复助手 · Windows 侧环境体检' -ForegroundColor Green
$sid = (Get-Process -Id $PID).SessionId
Add-Row '当前会话' $(if ($sid -eq 1) { 'ok' } else { '注意' }) "session $sid"

# ---------------------------------------------------------------- QEMU guest agent
Write-Step 'QEMU guest agent'
$ga = Get-Service | Where-Object { $_.Name -eq 'QEMU-GA' -or $_.DisplayName -like '*QEMU Guest Agent*' } |
      Where-Object { $_.Name -notlike '*VSS*' } | Select-Object -First 1
if ($ga) {
    if ($ga.Status -eq 'Running') {
        Add-Row 'guest agent' 'ok' "$($ga.Name) 正在运行"
    } else {
        Add-Row 'guest agent' 'FAIL' "$($ga.Name) 是 $($ga.Status)，先 Start-Service $($ga.Name)"
    }
} else {
    Add-Row 'guest agent' 'FAIL' '没找到 QEMU-GA 服务 —— 装 virtio-win 里的 qemu-guest-agent'
}

# ---------------------------------------------------------------- winapp CLI
Write-Step 'winapp CLI（读 UIA 用）'
$winapp = $null
$cmd = Get-Command winapp -ErrorAction SilentlyContinue
if ($cmd) { $winapp = $cmd.Source }
elseif (Test-Path (Join-Path $WinappDir 'winapp.exe')) { $winapp = Join-Path $WinappDir 'winapp.exe' }

if ($winapp) {
    $ver = (& $winapp --version 2>&1 | Select-Object -First 1)
    Add-Row 'winapp' 'ok' "$winapp（版本 $ver）"
} elseif ($Auto) {
    Write-Host '  正在从 GitHub Releases 取 winappcli-x64.zip ...' -ForegroundColor Yellow
    try {
        $rel = Invoke-RestMethod -UseBasicParsing -TimeoutSec 60 `
               -Uri 'https://api.github.com/repos/microsoft/winappCli/releases/latest' `
               -Headers @{ 'User-Agent' = 'wx-reply-assistant-setup' }
        $asset = $rel.assets | Where-Object { $_.name -match 'x64\.zip$' } | Select-Object -First 1
        if (-not $asset) { throw "release $($rel.tag_name) 里没有 x64 zip 资产" }
        $zip = Join-Path $env:TEMP $asset.name
        Write-Host "  $($asset.name)  $([math]::Round($asset.size/1MB,1)) MB" -ForegroundColor Yellow
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 1800 -Uri $asset.browser_download_url -OutFile $zip
        if (-not (Test-Path $WinappDir)) { New-Item -ItemType Directory -Path $WinappDir -Force | Out-Null }
        Expand-Archive -Path $zip -DestinationPath $WinappDir -Force
        Remove-Item $zip -Force -ErrorAction SilentlyContinue
        $exe = Get-ChildItem -Path $WinappDir -Recurse -Filter 'winapp.exe' | Select-Object -First 1
        if ($exe) {
            $winapp = $exe.FullName
            $ver = (& $winapp --version 2>&1 | Select-Object -First 1)
            Add-Row 'winapp' '已装' "$winapp（版本 $ver）—— 装到别处的话改 collector\wx_collect.ps1 里的 `$Winapp"
        } else {
            Add-Row 'winapp' 'FAIL' "解压完里面没有 winapp.exe —— 手动看下 $WinappDir 的结构"
        }
    } catch {
        Add-Row 'winapp' 'FAIL' "下载失败：$($_.Exception.Message)"
    }
} else {
    Add-Row 'winapp' 'FAIL' "没找到 —— 期望 $WinappDir\winapp.exe 或在 PATH 里"
    Write-Host '  装法（任选一种）：' -ForegroundColor Yellow
    Write-Host '    winget install Microsoft.winappcli --source winget'
    Write-Host '    或到 https://github.com/microsoft/winappCli/releases/latest 下 winappcli-x64.zip 解压'
    Write-Host '    想让它自己装：加 -Auto 重跑'
}

# ---------------------------------------------------------------- PsExec64
Write-Step 'PsExec64（注入交互会话用）'
if (Test-Path $Psexec) {
    Add-Row 'PsExec64' 'ok' $Psexec
} elseif ($Auto) {
    try {
        $dir = Split-Path $Psexec -Parent
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Write-Host '  正在从 live.sysinternals.com 取 PsExec64.exe ...' -ForegroundColor Yellow
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 300 `
            -Uri 'https://live.sysinternals.com/PsExec64.exe' -OutFile $Psexec
        if (Test-Path $Psexec) {
            Add-Row 'PsExec64' '已装' "$Psexec（$([math]::Round((Get-Item $Psexec).Length/1KB)) KB）—— 采集器里写死的就是这个路径"
        } else {
            Add-Row 'PsExec64' 'FAIL' '下载没落地，检查网络或代理'
        }
    } catch {
        Add-Row 'PsExec64' 'FAIL' "下载失败：$($_.Exception.Message) —— 也可以手动从 https://learn.microsoft.com/sysinternals/downloads/psexec 拿"
    }
} else {
    Add-Row 'PsExec64' 'FAIL' "没找到 $Psexec"
    Write-Host '  想让它自己装：加 -Auto 重跑（从 live.sysinternals.com 取）' -ForegroundColor Yellow
}

# ---------------------------------------------------------------- 工作目录
Write-Step '工作目录'
if (Test-Path $WorkDir) {
    Add-Row '工作目录' 'ok' $WorkDir
} else {
    try {
        New-Item -ItemType Directory -Path $WorkDir -Force | Out-Null
        Add-Row '工作目录' '已建' $WorkDir
    } catch {
        Add-Row '工作目录' 'FAIL' "$WorkDir 建不出来：$($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------- 电源
Write-Step '电源设置（读屏不能被睡眠打断）'
try {
    # 别按字段名找「交流」那一行 —— 中文系统上字段名是本地化的（当前交流电源设置索引），
    # 英文匹配会一个都抓不到。改用位置：powercfg 的输出里最后两个 0x 值固定是
    # 交流、直流（倒数第二个就是交流）。
    $raw = (powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 2>&1 | Out-String)
    $hex = @([regex]::Matches($raw, '0x[0-9a-fA-F]{8}') | ForEach-Object { $_.Value })
    if ($hex.Count -ge 2) {
        $sleepMin = [Convert]::ToInt32($hex[$hex.Count - 2], 16) / 60
        if ($sleepMin -eq 0) {
            Add-Row '睡眠' 'ok' '交流电下已设为「从不」'
        } else {
            Add-Row '睡眠' '注意' "交流电下现在是 $sleepMin 分钟后睡眠"
            if ($Auto) {
                powercfg /change standby-timeout-ac 0 | Out-Null
                powercfg /change monitor-timeout-ac 0 | Out-Null
                powercfg /change disk-timeout-ac 0    | Out-Null
                Add-Row '睡眠' '已改' '交流电下睡眠/关屏/关盘全部设为「从不」'
            } else {
                Write-Host '  改法：powercfg /change standby-timeout-ac 0（加 -Auto 会自动改）' -ForegroundColor Yellow
            }
        }
    } else {
        Add-Row '睡眠' '注意' '没解析出电源设置，建议手动跑一次 powercfg /change standby-timeout-ac 0'
    }
} catch {
    Add-Row '睡眠' '注意' "查不出来：$($_.Exception.Message)"
}

# ---------------------------------------------------------------- 微信窗口
Write-Step '微信窗口'
if ($winapp) {
    if ($sid -ne 1) {
        Add-Row '微信窗口' '跳过' "当前是 session $sid，UIA 读不到东西；要在 session 1 里跑或经 PsExec -i 1 注入"
    } else {
        $out = (& $winapp ui list-windows -a Weixin 2>&1 | Out-String)
        if ($out -match 'HWND\s+(\d+)') {
            Add-Row '微信窗口' 'ok' "HWND $($Matches[1])"
        } else {
            Add-Row '微信窗口' 'FAIL' '没找到 Weixin 窗口 —— 微信没开，或窗口是「微信」而不是「Weixin」（实测两者都有，代码会都试）'
        }
    }
} else {
    Add-Row '微信窗口' '跳过' 'winapp 还没就位'
}

# ---------------------------------------------------------------- 汇总
Write-Step '体检结果'
# 不用 Format-Table —— 中文是双宽字符，按字符数补空格会歪。
# 这里按显示宽度自己补，输出在任何控制台宽度下都是齐的。
function Pad-Disp([string]$s, [int]$width) {
    $dw = 0
    foreach ($ch in $s.ToCharArray()) {
        if ([int]$ch -gt 0x2E80) { $dw += 2 } else { $dw += 1 }
    }
    return $s + (' ' * [Math]::Max(1, $width - $dw))
}
$markOf = @{ ok = '[ok]'; '已装' = '[ok]'; '已建' = '[ok]'; '已改' = '[ok]'; FAIL = '[!!]'; '注意' = '[--]'; '跳过' = '[--]' }
foreach ($r in $rows) {
    $mark = if ($markOf.ContainsKey($r.状态)) { $markOf[$r.状态] } else { '[--]' }
    $color = switch ($mark) { '[ok]' { 'Green' } '[!!]' { 'Red' } default { 'Yellow' } }
    # 整行一次写出去 —— Write-Host -NoNewline 分段写的话，
    # Start-Transcript / 重定向到文件时每段会被记成一行，文件里就散架了
    Write-Host ('  ' + $mark + '  ' + (Pad-Disp $r.项目 16) + $r.说明) -ForegroundColor $color
}
Write-Host ''
$extra = @($rows | Where-Object { $_.状态 -in @('已装', '已建', '已改') })
if ($extra.Count -gt 0) { Write-Host "本次改了 $($extra.Count) 处（看上面标 [ok] 且说明里写了装/建/改的行）。" }

$bad = @($rows | Where-Object { $_.状态 -eq 'FAIL' })
if ($bad.Count -eq 0) {
    Write-Host '没有 FAIL 项。接着把 NAS 侧的 config.local.yaml 填好，就能开采集。' -ForegroundColor Green
    Write-Host '（别忘了微信本身要登录着，并且打开一个会话）' -ForegroundColor Gray
} else {
    Write-Host "$($bad.Count) 项 FAIL，看上面说明那一列。" -ForegroundColor Yellow
    Write-Host '不加 -Auto 重跑一次可以只体检；缺工具时用 -Auto 让它自己装。' -ForegroundColor Gray
}
