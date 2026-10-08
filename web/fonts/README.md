# 字体说明

面板不自带商业字体，用的是三套 OFL（SIL Open Font License 1.1）开源字体，
按 Claude 移动端那套视觉语言挑了最接近的近似体，子集化后自托管（不联网取字）：
子集只留面板实际用到的字，所以体积小、离线也能正常显示。

| 文件 | 实际字体 | 顶替谁 | 许可 |
| --- | --- | --- | --- |
| `ClaudeSerif-400/600.woff2` | Source Serif 4 | 标题衬线体 | OFL 1.1 |
| `ClaudeSans-400/500/600.woff2` | Hanken Grotesk | 正文无衬线体 | OFL 1.1 |
| `ClaudeSerifSC-400.woff2` | Noto Serif CJK SC（子集） | 中文衬线体 | OFL 1.1 |

三套字体均来自 Google Fonts，许可文本见
<https://openfontlicense.org/> 与各字体在 Google Fonts 上的页面。
OFL 允许随软件一起再分发（含子集化），保留本说明即可。

生成方式见 `resync.py` 的 `fetch_fonts.py`（面板 HTML 中 `@font-face` 由它拼出）。
