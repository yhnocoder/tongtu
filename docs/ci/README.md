# CI 与测试 —— 分层、执行环境与作业结构

> 本文是测试分层与 CI 作业的权威定义。阶段定位与产物契约见 [ARCHITECTURE.md](../ARCHITECTURE.md)，各阶段的验收条目见 [stages/](../stages/) 下对应文档，命令面与退出码见 [CLI.md](../CLI.md)。本文取代 ARCHITECTURE.md §7 的分层定义（见末节）。

**要解决的问题**：fetch、flatten、precompile、mask 四个阶段已实现，各自的验收条目写在设计稿里，执行方式是阶段落地时人工跑一遍，跑完不留下可重复执行的东西。而每个新阶段落地都在改动已完成阶段共用的代码——precompile 那次改了 `fetch.py`、`flatten.py`、`cli.py`，mask 那次改了 `cli.py`、`stages/__init__.py`，agent 适配层那次改了 `cli.py`、`config.py`——这类改动若打坏上游阶段，当前没有任何自动检查会报告。CI 目前只跑 pre-commit。

## 分层判据

**按外部依赖分层，不按测试类型分层。** 一个测试需要 TeX、需要网络、需要模型，决定它能否进 CI、进哪个作业、是否设为合并必过；而单元与集成的区分不改变执行环境，对作业编排没有指导作用。

三层依此界定：

| 层 | 外部依赖 | 执行环境 | 一次耗时 |
|---|---|---|---|
| 文本层 | 无 | runner | 秒级 |
| 编译层 | TeX | 参考镜像 | 分钟级 |
| LLM 层 | TeX、网络、模型 | 本机或镜像，手动 | 分钟级，计费 |

编译层内部按测试对象分两组：**自造论文组**无网络依赖，**真实论文组**需要从 arXiv 下载。两组执行环境相同，确定性等级不同，因此分属不同作业（见「作业结构」节）。

ARCHITECTURE.md §7 把编译层定义为 identity translation（MockAgent 恒等翻译跑完整流水线），这个定义把编译层的起点绑在全部阶段建成之后。改按依赖界定之后，编译层从第一个需要 TeX 的阶段起即成立，并随阶段推进逐步加长：当前到 mask，survey 落地后到 survey，全部阶段建成后即 identity translation。**identity translation 是编译层的终点形态，不是它的定义。**

## 测试内容的来源

**测试用例不重新设计，来自各阶段设计稿的「验收与试跑对象」一节。** 这些条目已经是机械可判的断言，精确到字段取值：[stages/mask.md](../stages/mask.md) 要求 revtex 的 `widetext` 与 conference 的 `sidenote` 记 category `unknown`、decided_by `default`，article 的定理环境与声明的自定义环境记 decided_by `newtheorem` 或 `newenvironment`；[stages/precompile.md](../stages/precompile.md) 要求首编即过的论文 `fix_session` 为 false、`precompile.tex` 与 `flat.tex` 逐字节相同。落成测试是把这些条目从人工执行改为自动执行。

有一处需要拆开：现有验收条目把两类测试对象写在同一句里（「十一篇：三篇自造论文加八篇真实论文」），而两者的外部依赖不同，落测试时分属不同作业。拆分只在测试代码里发生，设计稿的验收条目不改。

## 文本层

无外部依赖，秒级，PR 必过。六项内容：

1. **掩码往返恒等**。`unmask(mask(x)) == x` 是 [stages/mask.md](../stages/mask.md) 的出口判据之一，判定实现 `masking.verify_roundtrip` 已在生产路径上（每篇论文跑 mask 都执行）。测试不新增断言，只是把输入来源从论文源码换成 hypothesis 生成的随机输入，另加三篇自造论文的源码文件作固定输入。
2. **artifact model 的写入读回往返**。`tongtu/artifacts/` 下各 manifest 模型写出 JSON 再读回，字段与取值不变。
3. **CLI 冒烟**。`tongtu --version`、`--help`、`stage --help`、`doctor` 能够启动并返回预期退出码。ruff 的 F 规则查不出运行时的导入错误，这一项补上。
4. **退出码映射**。各阶段状态到退出码的映射符合 [CLI.md](../CLI.md) 的退出码表，其中 `pdf_only` 沿链退 3 是跨子命令的约定，改动错误处理时容易被无声破坏。
5. **CLI 调用约定**。论文参数的三种形态（编号、链接、本地目录）、用法错误退 2、`--json` 当前被忽略、`--workdir` 与 `$TONGTU_HOME` 的优先级。链接解析与编号合法性判定是纯函数；本地目录入口的重拷语义只做文件拷贝，同样不需要 TeX 与网络。
6. **例子清单与源码的两端校验**。三篇自造论文 `MANIFEST.json` 声明的覆盖点在源码里确有对应，源码里的形态也都在覆盖矩阵中登记，两端不得单边漂移（[examples/README.md](../../examples/README.md) 的原有约定）。

这一层价值最高的理由：`masking.py` 是当前代码里唯一一处出错不会当场报告的地方。掩码丢掉一个字符，译文照常 backfill、照常编译通过、PDF 照常产出，只是少一段文本，没有任何下游环节会报错，而人工核对二十页论文的字符级完整性不可行。其余各处的失败都由非零退出码或编译失败直接暴露。

## 编译层

需要 TeX，不需要模型。

### 自造论文组

`examples/papers/` 三篇跑 fetch（本地目录入口）→ flatten → precompile → mask，逐阶段核对 manifest 的验收字段。无网络依赖、结果确定，PR 必过。

本机实测三篇全部通过，累计十几秒：

```
article     precompile ok  5 页  fix_session=False  flat.tex 与 precompile.tex 逐字节相同
            mask ok  blocks=11 captions=3  声明驱动的环境四个（newtheorem 三个、newenvironment 一个）
revtex      precompile ok  2 页  fix_session=False  逐字节相同
            mask ok  blocks=14 captions=2  widetext 记 (unknown, default)
conference  precompile ok  2 页  fix_session=False  逐字节相同
            mask ok  blocks=12 captions=2  sidenote 记 (unknown, default)
```

### 真实论文组

[examples/README.md](../../examples/README.md) 八篇中首编即通过的六篇（`2002.05202`、`2106.04426`、`2409.19606`、`2512.02556`、`2512.24880`、`2604.15804`），加 `1412.6980` 验证 PDF-only 的退出码 3 分流。跑同样四个阶段，核对同样的验收字段。需要网络，不设为合并必过（理由见下节）。

另有 `2412.19437` 只跑到 flatten。这一篇首编失败、须经修复会话修到通过，整篇属 LLM 层；但它承担的 flatten 判据——`main.tex` 里生效的与注释掉的 `\bibliography` 并存，注释掉的那行不参与 bbl 内联——在编译之前就能判定，随整篇推到 LLM 层会白丢一处覆盖。

**真实论文的覆盖价值不低于自造论文。** [examples/README.md](../../examples/README.md) 的分工是自造论文覆盖版式与语法、真实论文覆盖 e-print 的下载形态与源码杂质。主文件不叫 `main.tex`、两级嵌套 `\input`、正文直接使用 @-命令、生效与注释掉的 `\bibliography` 并存，这些形态只有真实论文覆盖，而回归容易发生在没有预想到的形态上。

## LLM 层

需要模型，手动触发，不进 CI。当前内容是八篇中首编失败的两篇：`1701.06538`（`\pdfoutput` 原语与缺失的 eps 图源）与 `2412.19437`（CJKutf8 机制），两篇都须经修复会话修到复验通过，会话轮数与耗时的实测值用于校准预算上限（`2412.19437` 编译之前的那部分判据在编译层，见上节）。survey 起各阶段的模型调用同样归入本层。

不进合并路径的理由沿用 ARCHITECTURE.md 附录 A.7：模型输出的波动会变成 PR 阻塞，而质量回归不需要卡在合并路径上。

## 真实论文进入 CI 的前提

四条前提均已核实：

- **分发义务不受影响**。仓库公开、会被 clone，所以真实论文源码不入库（[examples/README.md](../../examples/README.md) 的约定）。CI 在 runner 上下载、跑完即弃，不构成分发；GitHub Actions 缓存同样不对外分发。约定针对的是仓库内容，不限制运行时下载。
- **体积与耗时可忽略**。实测四篇：`2002.05202` 的 e-print 8K、下载 1.6 秒，`1701.06538` 296K、4 秒，`2412.19437` 1.1M、2 秒，`2106.04426` 52K、2 秒；四篇解包后累计 4.5M。八篇不超过 10M，半分钟内下完。
- **下载频次受控**。用 `actions/cache` 缓存 e-print，key 取论文清单所在文件（`tests/compile/test_real_papers.py`）的 hash，命中则不向 arXiv 发请求；该文件变动才重新下载。
- **失败的确定性有限**。arXiv 不可用或论文出新版本会造成与代码无关的失败，因此这一组不设为合并必过，且走定时触发而非 PR 触发。

## 执行环境

编译层两组都在参考镜像内执行，跟随 ARCHITECTURE.md §6 「CI 一律用镜像」：TeX 发行版差异会造成实际的行为差别，而真实论文的宏包需求不可预测，这也是镜像选 TeX Live full 不裁剪的原因。

**镜像来源**：编译层的两个作业都以 `ghcr.io/<owner>/<repo>:env` 为 container 直接执行，作业内不构建镜像。`docker/Dockerfile` 分三层——`base`（TeX Live full 与受管 Python）、`env`（Python 依赖与字体注册，不含仓库代码）、`runtime`（加上仓库代码与构建期自检）。CI 用的是 `env`，代码由 checkout 提供，作业第一步 `uv sync --frozen` 把项目本体装进镜像里那个 venv，依赖已就位因而是秒级的。

**为什么不在作业内构建**：实测一次 compile 作业 292 秒，其中真正跑测试只有 37 秒，`Build reference image` 一步占去 187 秒——而那还是 buildx 的 gha 缓存全部命中的情况。缓存命中省掉的只是重新执行 `RUN` 指令，省不掉两项固定开销：从缓存服务下载 6GB 级的层，以及 `load: true` 把镜像全量导出再导入本地 docker daemon。这两项与代码改没改无关，每次都付。改成拉现成镜像后这部分消失。

**镜像何时重建**：`.github/workflows/image.yml` 在 push main 与 push tag `v*` 时构建，两个 target 一次出——`env` 每次都推，`runtime` 只在 tag 与手动触发时推。push main 时 `runtime` 只构建不推：编译层已不在合并路径上构建完整镜像，这一步替它保住代码层（`COPY` 仓库、`uv sync`、`tongtu doctor` 自检）的验证，否则那部分要等到打 tag 发布才发现被改坏。

两个 target 不拆成两个 workflow，因为 `runtime` 就建在 `env` 之上，拆开会让同一套 6GB 基底层构建两遍并抢同一个 buildx 缓存 scope。

## 作业结构

| 作业 | 触发 | 环境 | 内容 | 合并必过 |
|---|---|---|---|:---:|
| `lint` | push main、PR | runner | pre-commit（ruff check、ruff format、diction lint） | 是 |
| `test` | push main、PR | runner | 文本层 | 是 |
| `compile` | push main、PR | container：`:env` 镜像 | 编译层自造论文组 | 是 |
| `papers` | 每日定时、手动 | 同上 | 编译层真实论文组 | 否 |
| `image` | push main、push tag `v*`、手动 | runner | 构建并推参考镜像的两个 target | 否 |

前三个作业在 `.github/workflows/ci.yml`，`papers` 单独在 `.github/workflows/papers.yml`，`image` 在 `.github/workflows/image.yml`——三者的触发条件不同，一个 workflow 只能声明一组。

`papers` 走定时而非 PR 触发：真实论文的回归不需要在合并前知道，每日一次足够；放进 PR 路径会把 arXiv 的可用性与下载耗时接入合并路径，而这一组的失败有相当比例与代码无关。

`lint` 作业沿用现有 `ci.yml` 的内容，仅从当前名为 `text` 的单作业中拆出——该作业名与其内容（只跑 pre-commit）不符。

## 目录布局与标记

```
tests/
├── conftest.py                    # 仓库路径、环境分类表、三篇论文的 MANIFEST
├── text/                          # 文本层，无外部依赖
│   ├── test_masking_roundtrip.py  # 往返恒等：hypothesis 随机输入 + 三篇源码
│   ├── test_artifact_models.py    # manifest 写入读回，load_manifest 的返回约定
│   ├── test_cli_smoke.py          # 命令能启动、退出码登记在册
│   ├── test_exit_codes.py         # 各阶段状态到退出码的映射，逐状态穷举
│   ├── test_cli_contract.py       # 论文参数三种形态、用法错误退 2、工作目录优先级
│   └── test_example_manifests.py  # MANIFEST 覆盖点与源码的两端校验
└── compile/                       # 编译层，需要 TeX
    ├── conftest.py                # 经 CLI 子进程跑流水线、读产物
    ├── test_fixture_papers.py     # 自造论文组，无网络依赖
    └── test_real_papers.py        # 真实论文组，需要网络
```

pytest 标记在 `pyproject.toml` 声明，`addopts` 默认排除需要外部依赖的两类：

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "compile: 需要 TeX 与参考镜像，容器内 `pytest -m compile` 执行",
    "network: 需要访问 arXiv，与 compile 叠加使用",
    "llm: 需要接入模型，计费，手动执行",
]
addopts = "-m 'not compile and not llm'"
```

三条选择线因此是：本机直接跑 `pytest` 得到文本层；`-m "compile and not network"` 是自造论文组；`-m "compile and network"` 是真实论文组。`network` 不进 `addopts` 的排除项——它总与 `compile` 叠加，排除 `compile` 时已一并排除。`docker/Dockerfile` 的注释已经引用 `pytest -m compile` 这一约定，此处将其落定。

## 暂不做的

- **golden-file 快照**（syrupy 已在开发依赖内）。`blocks.json` 的字段会随 survey view 按 block 类型参数化 backfill 的需求变动，契约未冻结时写快照会退化成记录当前行为，每次改动都要跟着更新快照。等 export 的 artifact model 自校验落地、契约冻结之后再补。
- **跨全流水线的 identity translation**。需要 survey 起各阶段建成；MockAgent 届时获得第一个消费者（当前无消费者，见 [AGENTS.md](../../AGENTS.md) 当前状态节）。

## 对 ARCHITECTURE.md §7 的修改

本文取代 §7 的分层定义，§7 缩减为一段指向本文的说明。两处实质改动：

1. **编译层的定义**由 identity translation 改为按外部依赖界定，identity translation 降为该层的终点形态。
2. **测试内容与阶段设计稿的关系**由 §7 未表述改为在本文说明：测试用例来自各阶段的验收条目，不重新设计。

附录 A 增一条决策记录，记原分层的来由与本次修改的理由。附录 A.7 不改（条目只增不改）：它否决的是「LLM 层也设为 PR 必过」，这一实质不变，只是当前承担 PR 必过的具体对象从 identity translation 变为编译层自造论文组——两者同为零模型成本覆盖编译链路的测试，identity translation 是它建成后的形态。附录 B 第 2 条（identity translation 的中文路径覆盖）不受影响。

上述改动已执行：§7 改为按外部依赖分层并指向本文，附录 A 新增第 26 条决策记录。
