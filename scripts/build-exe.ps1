<#
build-exe.ps1 —— 把 setup-windows.ps1 打包成一个可双击的 exe

在 Windows 上（管理员 PowerShell）跑：
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-exe.ps1

产出：scripts\dist\wxreply-setup.exe

要点（都是踩过的）：
  * 用 ps2exe（PSGallery，`Install-Module ps2exe`）把 .ps1 编译成单文件 exe。
  * 要 -noConsole：双击时不弹黑框；脚本在没有控制台时会自己改用窗口显示结果
    （见 setup-windows.ps1 里的 $HasConsole 判断）。
  * 要 -requireAdmin：改电源计划、往 C:\dl 写东西都需要管理员，双击时由 UAC 提权。
  * 源 .ps1 必须带 UTF-8 BOM，否则 PS 5.1 按 ANSI 读，编进 exe 里中文就是乱码。
  * 出来的 exe 没有代码签名，别人第一次跑会被 SmartScreen 拦一下
    （「更多信息」→「仍要运行」），杀软也可能报「未知发布者」，这是正常的。
#>
param(
    [string]$Source = (Join-Path $PSScriptRoot 'setup-windows.ps1'),
    [string]$OutDir = (Join-Path $PSScriptRoot 'dist')
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if (-not (Test-Path $Source)) { throw "找不到源脚本：$Source" }

# 源文件得有 BOM
$bytes = [IO.File]::ReadAllBytes($Source)
if (-not ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)) {
    Write-Host '源脚本没有 BOM，补上' -ForegroundColor Yellow
    [IO.File]::WriteAllBytes($Source, ([byte[]](0xEF, 0xBB, 0xBF) + $bytes))
}

$m = Get-Module ps2exe -ListAvailable | Sort-Object Version -Descending | Select-Object -First 1
if (-not $m) {
    Write-Host '装 ps2exe（从 PSGallery）...' -ForegroundColor Yellow
    Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Force | Out-Null
    if (-not (Get-PSRepository PSGallery -ErrorAction SilentlyContinue)) { Register-PSRepository -Default }
    Set-PSRepository -Name PSGallery -InstallationPolicy Trusted
    Install-Module ps2exe -Scope AllUsers -Force
    $m = Get-Module ps2exe -ListAvailable | Sort-Object Version -Descending | Select-Object -First 1
}
Import-Module $m.Path -Force
Write-Host "ps2exe $($m.Version)"

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$dst = Join-Path $OutDir 'wxreply-setup.exe'
Remove-Item $dst -ErrorAction SilentlyContinue

Invoke-ps2exe -inputFile $Source -outputFile $dst `
    -title '微信回复助手 · Windows 侧环境体检' `
    -description '检查并按需补齐微信回复助手在 Windows 上要用的东西：winapp CLI、PsExec64、工作目录、睡眠设置、微信窗口' `
    -company 'wx-reply-assistant' -product 'wx-reply-assistant 环境体检' `
    -version '1.0.0.0' -requireAdmin -noConsole

$f = Get-Item $dst
Write-Host ''
Write-Host "好了：$($f.FullName)" -ForegroundColor Green
Write-Host "大小 $($f.Length) 字节   SHA256 $((Get-FileHash $dst -Algorithm SHA256).Hash)"
