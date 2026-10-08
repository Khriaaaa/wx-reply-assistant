; 安装包完整性由 NSIS 内建 CRC 校验负责：文件被截断或被聊天工具重编码，
; 双击时 NSIS 会在解包前自己拦下（"Installer integrity check has failed"）。
;
; 这里刻意不再自己算 SHA256 去比对 —— 安装包无法内嵌「自己的正确哈希」：
; 把哈希写进 exe 会改变 exe 的哈希，改完又得重算，永远收敛不了。
; 用户侧要可核对的校验值，走同目录 SHA256SUMS.txt + verify-installer.bat。
!macro preInit
  SetRegView 64
!macroend
