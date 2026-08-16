# fetch —— 论文源码落进工作目录

> 阶段定位见 [ARCHITECTURE.md](../ARCHITECTURE.md) §3 阶段表，本文是 fetch 阶段设计的权威。实现在 `tongtu/stages/fetch.py`；manifest 的字段级权威定义在 `tongtu/artifacts/fetch.py`（pydantic model，文档不复述字段表）。

**要解决的问题**：pipeline 的一切从一棵本地源码树开始，而论文来源有三种形态（arXiv 编号、arXiv 链接、本地目录）；e-print 端点不给文件名也不给可靠的 Content-Type；源还可能根本不是 LaTeX（PDF-only、pdfpages 套壳）。fetch 把这些形态收敛成统一的工作目录布局，并在流水线最前端完成「这篇论文能否走正常翻译路径」的分流。fetch 无 agent 介入，也是唯一写 `src/` 的阶段。

**输入 → 输出**：arXiv 编号 / arXiv 链接 / 本地源码目录 → `src/` 源码树 + `build/e-print.bin`（远程入口的原始下载体，供离线重解包与排查）+ `build/manifests/fetch.json`。出口判据是机械的：源码树落 `src/`，状态与逐文件 sha256 写入 manifest；PDF-only 是分支不是错误，零期到检测标记为止（degraded path 见 [`fallback/README.md`](../../fallback/README.md)）。

## 输入识别

三种形态按顺序识别，解包之后汇合到同一条判定与落盘逻辑：

1. **本地源码目录**：参数在文件系统里存在且是目录（examples 三篇自造论文走这条）。工作目录名取源目录的 basename，`--workdir` 覆盖。
2. **arXiv 链接**：以 `http://` 或 `https://` 开头。主机名须是 `arxiv.org` 或其子域；路径前缀 `/abs/`、`/pdf/`、`/html/` 三种都接受，取前缀之后的整段剩余路径作为编号——编号本身可含斜杠（如 `arxiv.org/abs/hep-th/9901001`），所以不能只取一段；末尾若带 `.pdf` 扩展名则去掉（`/pdf/` 链接的旧写法）；查询串与锚点丢弃。解析出编号后与第 3 种形态完全同路。
3. **arXiv 编号**：其余输入。编号可含斜杠与版本号后缀；用作工作目录名时斜杠替换为下划线，保持目录单层。

链接解析失败（主机不对、路径无上述前缀）与编号不合法（空串、含空白、路径穿越形态）是用法错误，退出码 2；它们在做任何工作之前就能判定，不进入阶段状态。

## 落盘

```
<workdir>/
├── src/                        # e-print 内容的原样落盘，只读不改
├── build/e-print.bin           # 原始下载体（远程入口），随 build/ 可丢
└── build/manifests/fetch.json  # stage manifest
```

- **`src/` 的含义是「e-print 内容的原样落盘」**：tar 解开；单文件落 `src/main.tex`；PDF-only 时 PDF 落 `src/main.pdf`。「怎么分流」只看 manifest，不从 `src/` 里有什么反推——两类信息解耦，degraded path 将来消费这份 PDF 也有固定位置。
- **本地目录拷贝进 `src/`**，不软链不原地使用：工作目录必须自足，流水线也不写用户的目录。跳过 `.git` 等版本控制目录与 `__pycache__`、`.DS_Store`；工作目录嵌在源目录内时不把工作目录自身拷进去。`MANIFEST.json` 这类杂文件原样拷入，不特判——真实 e-print 同样有杂文件。

## 远程下载

- 端点 `https://export.arxiv.org/e-print/<编号>`，httpx，带版本号与仓库地址的 User-Agent，超时 60 秒，跟随重定向。
- **不做自动重试**：任何网络失败转 `download_failed` 状态；`run` 幂等，重跑即重试。重试策略等真实失败形态出现后再加。
- 下载原语可注入（`Callable[[url], bytes]`）：测试与离线复跑不打网络；下载与解包分成两步，`build/e-print.bin` 在手就能离线重放解包。

## 分流判定

下载体按序判定（只能看头几个字节，端点不提供可靠元数据）：

1. `%PDF` → PDF-only，PDF 落 `src/main.pdf`；
2. gzip 魔数 → 按 tar.gz 解包；不是 tar → 按单个 gzip 压缩文件处理，解出后再查一次 `%PDF`，其余落 `src/main.tex`；
3. 裸 tar → 解包；
4. 其余 → 原样落 `src/main.tex`。

解包后统一收尾判定：

- 树里无 `.tex` 有 `.pdf` → `pdf_only`；两者皆无 → `empty`（失败）。
- **套壳检测**：全部 `.tex` 字符总量低于 1000 → 无实质内容；出现 `\includepdf` 且总量低于 5000 → pdfpages 套壳（实例 1412.6980）。两者等同 `pdf_only`。在解包后判而非展平后判，省一次 latexpand 调用。

**解包安全**：只放行普通文件与目录，链接与设备文件一律拒绝；路径安全（绝对路径、`..` 穿越）逐成员交给标准库 `tarfile.data_filter`。被拒成员记入 manifest 的 `rejected`，不中断解包。

## 状态与退出码

六状态（`FetchStatus`，StrEnum）：`ok` / `pdf_only` / `empty` / `download_failed` / `unpack_failed` / `source_missing`。阶段内不抛栈，异常一律转状态，调用方按状态分流——这条约定从 fetch 起适用于全部阶段驱动器。退出码映射（方案见 [CLI.md](../CLI.md)）：`ok` → 0，`pdf_only` → 3（分支不是错误，调度方按此改道），其余 → 1。

## 重跑语义

fetch 的输入在外部世界，没有可 hash 的本地输入，两种入口的重跑语义有意不对称：

**远程入口**：manifest 已存在、可解析、状态是 `ok` 或 `pdf_only`（上次已有结论）→ 跳过，不访问网络；否则从头执行。「manifest 在就跳过」是安全的：arXiv 对带版本号的 e-print 内容不可变，重新下载只会得到同样的字节。

**本地目录入口**：不做跳过判定，每次都重新拷贝、重新判定、重写 manifest。源目录随时可能被编辑（examples 论文在下游阶段的开发中会频繁改动），而拷贝成本可以忽略。源码没变则 manifest 里的 hash 不变，下游照常跳过，所以「每次重拷」不引起多余的下游重算。

`--force` 一律从头执行。从头执行前先清空 `src/`，避免与上次残留混杂。

**初期简化（后续调整）**：跳过判定只看 manifest 的状态字段，不校验 `src/` 内容与 manifest 是否一致（`src/` 本来就约定只读不改）；`build/` 整体删除后 manifest 随之消失，下次运行重新下载——重下得到相同结果，「`build/` 可整体删除」的约定不受影响，将来支持离线重建时再加「`src/` 已有源码树时只重算判定」的分支。

## 产物模型

manifest 即 `FetchManifest`。承担契约职责的字段：`status` 是唯一分流依据；`files`（`src/` 相对路径 → sha256 的排序全量清单）是下游阶段判定「输入未变不重算」时引用的权威记录，下游不必自己重扫源码树；`rejected` 与 `warnings` 供 export 组装 report 时取用。

## 验收与试跑对象

按顺序：三篇自造论文走本地目录入口（`src/` 齐、manifest 落、重拷语义正确）→ `2002.05202` 走编号与链接两种写法（单文件 gzip 形态）→ `1412.6980` 验证套壳分流（退出码 3）→ `1701.06538` 在下游阶段陆续调通后加入。真实论文源码不入库，见 [examples/README.md](../../examples/README.md)。
