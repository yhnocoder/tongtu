# fonts/

翻译产物统一使用的中文字体，随仓库分发，**不需要安装到系统**。
precompile 注入的 xeCJK 块只写字体文件名，不写路径；tongtu 启动 xelatex 时经
`TTFONTS` / `OPENTYPEFONTS` 把本目录交给 kpathsea，修复会话的环境里也设这两个变量。
同一份 `zh.tex` 在任何机器上都用这一套字体编译，论文目录可以在机器之间直接搬动。

| 文件 | 用途 |
|---|---|
| `LXGWWenKai-Light.ttf` | 正文（CJK 主字体 / 等宽字体） |
| `LXGWWenKai-Medium.ttf` | 粗体（`BoldFont`） |
| `SourceHanSansSC-Regular.otf` | 无衬线 |
| `SourceHanSansSC-Bold.otf` | 无衬线粗体（`BoldFont`） |

## 来源与许可

### 霞鹜文楷

[霞鹜文楷 LXGW WenKai](https://github.com/lxgw/LxgwWenKai) v1.522（2026-03-17），
**SIL Open Font License 1.1**（<https://openfontlicense.org>），可自由随仓库分发，
许可证全文见本目录的 `LICENSE-LXGWWenKai.txt`（取自该仓库 tag v1.522 的 `OFL.txt`）。

字体名表内的版权声明：

> Copyright 2021-2026 LXGW (https://github.com/lxgw/LxgwWenKai)
> Copyright 2020 The Klee Project Authors (https://github.com/fontworks-fonts/Klee)

### 思源黑体

[思源黑体 Source Han Sans](https://github.com/adobe-fonts/source-han-sans) 2.005R，
简体中文子集（SC）的 OTF 版，Adobe 发布，**SIL Open Font License 1.1**，
许可证全文见本目录的 `LICENSE-SourceHanSans.txt`（取自该仓库 tag 2.005R 的 `LICENSE.txt`）。

字体名表内的版权声明：

> © 2014-2025 Adobe (http://www.adobe.com/), with Reserved Font Name 'Source'.

OFL 的分发要求（每份拷贝附带许可证本身、保留版权与许可声明、不单独售卖、衍生字体不得使用保留字体名）
由本文件、`LICENSE-LXGWWenKai.txt`、`LICENSE-SourceHanSans.txt` 与字体文件内嵌的 name 表条目共同满足；
本仓库不修改字体二进制。
