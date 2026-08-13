# fonts/

翻译产物统一使用的中文字体，随仓库分发，**不需要安装到系统**——
`tongtu/stages/inject_cjk.py` 注入的 xeCJK 配置以 `Path = {fonts/}` **相对路径**引用它们，
compile 阶段把本目录链接/拷贝进 build 目录，export 阶段把它拷进自包含产物包，
同一份 `zh.tex` 在开发机与参考镜像内都能编译（架构 §10）。

| 文件 | 用途 |
|---|---|
| `LXGWWenKai-Light.ttf` | 正文（CJK 主字体 / 等宽字体） |
| `LXGWWenKai-Medium.ttf` | 粗体（`BoldFont`） |

无衬线字体不随仓库分发：注入块用 `\IfFontExistsTF` 按平台探测
（Hiragino Sans GB → Noto Sans CJK SC → 回退到本目录的霞鹜文楷）。

## 来源与许可

[霞鹜文楷 LXGW WenKai](https://github.com/lxgw/LxgwWenKai) v1.522（2026-03-17），
**SIL Open Font License 1.1**（<https://openfontlicense.org>），可自由随仓库分发。

字体名表内的版权声明：

> Copyright 2021-2026 LXGW (https://github.com/lxgw/LxgwWenKai)
> Copyright 2020 The Klee Project Authors (https://github.com/fontworks-fonts/Klee)

OFL 的分发要求（保留版权与许可声明、不单独售卖、衍生字体不得使用保留字体名）
由本文件与字体文件内嵌的 name 表条目共同满足；本仓库不修改字体二进制。
