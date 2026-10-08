#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成用户侧的校验批处理 verify-installer.bat（把安装包当前哈希写进去）。

用法：
    python scripts/make-verify-bat.py desktop/dist/wx-reply-assistant-setup.exe out/verify-installer.bat

为什么批处理要用 GBK 落地：中文 Windows 的 cmd 默认按 CP936 读批处理，
UTF-8 的中文在执行时是乱码。
"""
import hashlib
import sys
from pathlib import Path

TEMPLATE = r"""@echo off
setlocal enabledelayedexpansion
title 微信回复助手 —— 安装包校验
cd /d "%~dp0"

set "EXE=wx-reply-assistant-setup.exe"
set "EXPECT=__SHA256__"

if not exist "%EXE%" (
  echo.
  echo   没找到 %EXE%
  echo   请把本文件和安装包放在同一个文件夹，再双击本文件
  echo.
  pause
  exit /b 1
)

echo.
echo   正在核对 %EXE% ...
echo.

for /f "delims=" %%h in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "(Get-FileHash -LiteralPath '%EXE%' -Algorithm SHA256).Hash.ToLower()"') do set "H=%%h"

echo   实际 SHA256 = !H!
echo   官方 SHA256 = %EXPECT%
echo.

if /i "!H!"=="%EXPECT%" (
  echo   [通过] 文件完整，没有被改动过
  echo.
  choice /c YN /m "   现在就安装吗"
  if errorlevel 2 exit /b 0
  start "" "%EXE%"
  exit /b 0
)

echo   [不通过] 文件不完整或已被改动
echo.
echo   常见原因：下载中断；通过聊天工具中转导致文件被重新编码
echo   处理办法：回到发布页重新下载，别用聊天软件里转过来的那份
echo.
pause
exit /b 2
"""


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    exe = Path(sys.argv[1])
    if not exe.exists():
        print("找不到安装包：%s" % exe)
        sys.exit(1)
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else exe.with_name("verify.bat")
    digest = sha256(exe)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="gbk", newline="\r\n") as f:
        f.write(TEMPLATE.replace("__SHA256__", digest))
        f.write("\n")
    print("%s  %s" % (digest, out))


if __name__ == "__main__":
    main()
