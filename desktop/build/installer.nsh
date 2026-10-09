; 卸载前先停掉本应用和它的子进程。
;
; 不这么做的话：采集器是 powershell 跑 wx_collect.ps1，它不在应用的进程树里
; （Electron 退出了它还在），会一直锁着 %APPDATA%\wx-reply-assistant\store
; 下面的文件。NSIS 的 RMDir /r 遇到被占用的文件是静默失败的 —— 表现就是
; 「卸载跑完 exit=0，但目录和一堆文件还在」。
;
; 判据用命令行匹配：应用本体、自带 python 跑 assistant.py/server.py 的子进程、
; 采集的 powershell，命令行里都带 wx-reply-assistant 这段路径。
;
; 注意：NSIS 会把 $ 当变量起始符，PowerShell 里的 $_ 必须写成 $$_，
; 否则报 warning 6000 unknown variable "_.ProcessId"，而 electron-builder
; 把 warning 当 error，整个打包会失败。
!macro customUnInstall
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $$_.CommandLine -like ''*wx-reply-assistant*'' } | ForEach-Object { Stop-Process -Id $$_.ProcessId -Force -ErrorAction SilentlyContinue }"'
  Sleep 2500
!macroend
