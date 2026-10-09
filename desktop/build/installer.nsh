; customUnInstall —— 卸载前把这套应用的所有进程收干净，再让 NSIS 删文件。
;
; 一开始的写法是「宏里直接杀一轮 powershell」，实测失败：面板自检（checker_loop
; 每 30s 一次 wx_collector check）会按心跳/锁把采集循环重新拉起，杀掉的和重新
; 拉起的在赛跑，等 2.5s 根本不够，RMDir 对被占文件静默失败，目录清不掉。
;
; 现在分三步：
;   1. 跑 assistant.py shutdown —— 它先 down（收 watch/sync/panel/采集），然后
;      最多 60s 循环核对「命令行含 wx-reply-assistant 的进程」连续两轮为 0，
;      等稳了才退出（panel 被停后就没有自检线程再拉采集了）。
;      注意代码与数据的固定关系：代码装在 $INSTDIR\resources\py（打包时在
;      package.json 里定的），数据在 %APPDATA%\wx-reply-assistant（main.js
;      setPath('userData') 定的）。宏要拼的是两个根：
;        python.exe -> "$INSTDIR\resources\python\python.exe"
;        assistant.py -> "$INSTDIR\resources\py\assistant.py"
;      不要用 $APPDATA 拼 py 路径 —— 那是数据目录，卸载完成时就没了。
;      shutdown 收进程时会把自己（安装目录里的 python.exe）也列出来，它内部
;      排除了查询 powershell 自身；宏里不需要再杀一遍。
;   2. 等 shutdown 退出后再 Sleep 1500ms 收尾（锁文件清理、句柄释放）。
;   3. 之后 NSIS 自己的 RMDir /r 就不会再撞被占文件。
;
; NSIS 把 $ 当变量起始符，PowerShell 的 $_ 必须写成 $$_（warning 6000 会被当
; error 拒掉整个构建）。FileOpen/FileWrite 那四行是「宏真的跑了」的标记文件，
; 留着便于以后验证，上线前可删。
!macro customUnInstall
  nsExec::ExecToLog '"$INSTDIR\resources\python\python.exe" "$INSTDIR\resources\py\assistant.py" shutdown'
  Pop $0
  Sleep 1500
  FileOpen $0 "C:\dl\wxrun\uninstall_macro_fired.txt" w
  FileWrite $0 "fired"
  FileClose $0
!macroend
