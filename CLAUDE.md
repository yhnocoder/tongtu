# 通途（tongtu）

> 若存在 `../AGENTS.md`（内部开发工作区 wenshu-ws 的统一 context），先读它获取项目全景；本文件对外部贡献者自足。

## 我们在做什么

做一个极致的 arXiv 论文翻译工具。名字取自「天堑变通途」：读论文时不想在英文里跋涉，又舍不得原来的排版与公式——那就让别的都留在原处，只把话换成中文。

极致的意思只有一条：打开那份中文 PDF，宁愿读它而不读英文原文。术语准、句子是中文的句子而不是英文的影子、公式图表脚注一个不少、版式跟原文一样好看。做到这一条，工具就成了；没做到，流水线再完备、文档再周全也不算数。

这是自用的项目，没有「一般用户」。判断任何设计选择只问一句：这会不会让译文更好、让人更愿意打开它。开发阶段不为省钱牺牲形态，先做成最想要的样子。

## 我们怎么一起做

这是两个人的合作，不是一个人下令一个人执行。对协作者（包括 AI）的要求：

- 有热情。把它当成自己想用的东西来做，而不是一个要交付的任务。
- 互相 challenge。觉得方向不对、方案不好、文档写错了，直接说，给理由，不要顺从。被 challenge 的一方认真回应，不替文档或既有代码辩护——文档记录的是当时的共识，不是契约；写不通就停下来报告冲突并给替代方案，改不改当场拍板，拍板后文档与代码一起改。
- 可靠。说做完了就是做完了、验过了；没做的、跳过的、不确定的，明说。
- 每一步都朝着那份中文 PDF 走。先让产物存在，再让它变好，再让它可重复。为还没见过的问题写方案是负债。

## 不可动摇的判断

1. **LaTeX 源码是唯一真相源**：翻译 e-print 源码而非解析 PDF；编译通过是机器可验证的硬指标。
2. **agent 负责判断，脚本负责验证**：翻译、修编译错交给 agent；正确性只认校验脚本全绿与 PDF 编译通过，agent 的「我检查过了」无效力。
3. **论文工作目录不在仓库内**：默认 `~/.local/share/tongtu/<arxiv_id>/`（`$TONGTU_HOME` / `--workdir` 覆盖）。测试与试验不要在仓库里创建论文目录。
4. **agent 运行时可插拔**：适配层在 `tongtu/model/`，不绑定具体产品。API 直调还是 Claude Code 拉起，都只是实现路径。

## 当前状态

- V2 原型（`~/Projects/arxiv_trans/arXiv-2606.19348v1/arxiv-translator/`，约 1300 行脚本）已经跑通四篇论文出中文 PDF，是本仓库的参考基线。
- 本仓库 fetch、flatten、precompile、mask、survey、chunk、translate 已实现，但还没有出过一份中文 PDF。这一版封存在 commit `b751db9`（重构前的最后一版）。
- 设计依据只有一份：`docs/proposal/pipeline-v0.html`（下称提案图）。它是 2026-08-20 逐卡、逐字段讨论定下的共识：七个阶段 fetch → precompile → mask → survey → translate → review → compile，每张卡写这个阶段读什么、写什么、做什么，产物的类定义附在卡底；模型调用层在同目录的 `model.html`，命令行与重跑语义在 `cli.html`。人只需记住本文件；AI 实现某个阶段时读对应那张卡（卡片有 `id`，例如 `#translate`）。旧的 docs/ 已删除，不要再建第二份设计文档。
- 下一步是按提案图重构，不是在现有代码上继续建。理由：现状的阶段划分、重跑语义、manifest 形态都与提案不同，在上面继续建 compile 只会扩大要改的面。重构完成的判据是第一份中文 PDF——重构就是到它的最短路。

## 重构怎么做

重构期间的流程规定。重构结束（第一份中文 PDF 出来、所有卡片为「完成」）后删掉本节。

### 总则

- 一次完成一个中等大小的 feature，每步一个 PR 进 main。
- 主 agent 大部分情况下只做编排与 review。提案图的改动只随步骤 PR 进 main、由用户 merge 认可，核心开发、测试、验证各交给 subagent 完成，开发任务 subagent 使用 fable，其他任务一律用 Opus；任务书里要求 subagent 先读本文件。
- Git：主 agent 从 main 切分支 `step-<N>-<名字>`，subagent 在工作树里直接改（同一时刻只有一个 subagent 在改代码），主 agent review 通过后提交，用 `gh pr create` 开 PR 并关联步骤 issue，合并由用户做，squash merge，PR 标题即 commit 第一行。commit message 格式 `<type>(<scope>): <中文一句话>`：type 取 `feat` / `fix` / `refactor` / `test` / `ci` / `docs` / `chore`，scope 是阶段名或横切面名（`model`、`cli`、`fetch`…），一句话动词开头、不带句号、不超过 50 字；正文可选，写为什么与关键取舍，三行以内；末尾 `Closes #N`。不加 `Co-Authored-By` 一类署名行。交付报告写进 PR 描述，回复里贴同一份。
- subagent 的边界：可以联网（拉 arXiv、调模型、拉起 agent 运行时）、可以读写 `~/.local/share/tongtu/`；不装系统工具、不改 `~/` 下的配置，需要就停下来问。验证用本机 TeX；`work` 在步骤 1 用本机的 Claude Code（`claude`）验通，opencode / codex 也已安装、作为可选运行时；可用服务商、各角色默认模型与密钥环境变量名由用户在步骤 1 开工时给出，写进 `models.toml`，不写进本文件。docker 镜像（image.yml）在重构完成后再接回。
- 过渡期：步骤 1、2 只建新模块（`tongtu/model/`、新的命令行与工作目录代码），旧的 `tongtu/agent/`、`tongtu/stages/`、`tongtu/artifacts/` 与旧测试在各自阶段的步骤里整体删除；重构期间 main 上的流水线不保证可跑，以各步的测试与验证为准。
- 提案图是实现依据，但不是不能改：觉得它设计不合理、写不通、与代码冲突，停下来向用户说明并给替代方案，由用户拍板；不许隐瞒、不许绕过、不许自己决定偏离。提案图的改动只放进步骤 PR、随代码一起由用户 merge 认可；主 agent 不单独向 main 提交提案图改动。
- 进度与问题用 GitHub Issues 管理，不写文件。每个步骤一个 issue（标签 `step`），PR 描述里写 `Closes #N`，合并即关闭；发现的问题一个 issue（标签 `question`），结论写进 issue 再关，不删。新会话先 `gh issue list --state open`。步骤 issue 的状态：open 且无 PR = 待做或进行中，有 PR 关联 = 待验证，closed = 完成。

### 步骤顺序

0. 流程工具：写 `scripts/comment_lint.py`（只查 `.py`；pre-commit 查暂存文件，CI 查相对 main 的改动文件 `git diff --name-only origin/main...HEAD`；范围 `tongtu/`、`tests/`、`scripts/`；除工具指令外不得有注释与 docstring），接入 pre-commit 与 ci.yml；ci.yml 加 `test` 作业（`uv run pytest -q` 后跟显式路径清单，起步为空则该作业先不加）；删 papers.yml。
1. 模型调用层：`tongtu/model/`（`ask` / `work`）、`models.toml`、`tongtu setup`、`tongtu doctor`。依据是提案图「模型调用层」节与「命令行」节的对应行。`work` 用一个最小 skill 目录验通即可，各角色的 skill 随对应阶段的步骤重写。
2. 命令行与工作目录：`tongtu run [--from]`、`stage`、`status`（只读已存在的 manifest）、全局选项（`--model` / `--effort` / `--glossary` / `--jobs` 等）的解析与传递、manifests 目录与各 manifest 的共用字段。各阶段 manifest 的具体字段与 logs/ 文件留给步骤 3–8。
3. fetch + precompile（确定当前的 skills/precompile 是否可用，可以的话改名并使用）。
4. mask。
5. survey。
6. translate。
7. review。
8. compile → 第一份中文 PDF。

提案图已经点名的依赖（tiktoken、anthropic SDK）视为已拍板，不再按「不新增依赖」那条问。

### 每一步的流程

1. 主 agent 读本文件、open 的 issues 与对应卡片，写任务书：目标、读哪张卡（`docs/proposal/pipeline-v0.html` 的 `#<id>`）、允许改动的文件、完成判据、验证命令。如果有不确定的地方，停下来询问用户；提案图没给数值的常量先查询 b751db9 旧值，并向用户询问应该怎么决策。任务书必须自足，subagent 没有主会话的上下文。任务书先用 `gh issue comment` 贴到步骤 issue，等用户 Review 通过后，再以原文作为 subagent 的 prompt；退回 subagent 的补充要求同样追加为评论。任务书不进仓库。
2. 开发 subagent：按提案图实现该部分功能，重写该模块的测试；删光改动文件里的注释与 docstring；跑 `make lint` 与 pytest；返回改动文件清单与测试输出原文。
3. 主 agent review：按下面的清单逐条判定，读 diff、读 subagent 的汇报、自己跑命令都可以，判据是清单。不过就退回同一个 subagent 并指明条目；两轮不过，停下来报告用户。
4. 验证 subagent（新开上下文，与开发 subagent 无关）：按任务书的验证命令实际运行，报告命令、退出码、产物路径与关键内容。步骤 0–2 没有论文可跑，验证就是实际执行命令（`make lint`、`tongtu setup`、`tongtu doctor`、`tongtu status` 等）；步骤 3 起验证集 12 篇全跑，每篇状态与耗时写进报告，没有硬性通过线，由用户看报告定。以运行结果为准，不以读代码为准。
5. 主 agent：开 PR 并关联步骤 issue，写交付报告；提案图的完成标记与本步已拍板的改动一并放进这个 PR。
6. 用户验证过程中新拍板的改动，在合并前追加进 PR（含提案图的对应改动）；用户合并即步骤完成。来不及在合并前追加的，事后以补丁 PR 处理，属例外而非常态。

### 代码规范

- `make lint` 是唯一入口：ruff check、ruff format、diction lint、comment lint（步骤 0 建）全绿。
- 改动过的文件不留注释与 docstring，原有的也删。例外只有工具指令：`# noqa`、`# type: ignore`、`# pragma: no cover`。代码靠命名与结构自明；觉得非解释不可，说明设计没写清楚，按总则回提案图讨论。
- 类名、字段名、枚举值、产物文件名与提案图一字不差。提案图没有的字段、阶段、命令、选项一律不加；觉得该加，按总则停下来问。
- 文件布局沿用现状：阶段代码 `tongtu/stages/<stage>.py`，产物 model `tongtu/artifacts/<stage>.py`，跨阶段共用的 model（CompileReport、FixSession）放 `tongtu/artifacts/common.py`，模型调用层 `tongtu/model/`。状态值用 `StrEnum`。函数签名全部带类型标注；产物只经 pydantic model 读写，不手写 dict。
- 不新增依赖。确有必要，在任务书里先写理由，由用户拍板。
- 预期中的失败（编译失败、校验不过、模型超时）进 manifest 的 status 与 message，不抛异常；只有程序错误才抛。
- 测试：每个模块一个测试文件，按外部依赖分目录：`text/`（无外部依赖）、`compile/`（需要 TeX）、`llm/`（需要接模型）。断言产物文件与 manifest 字段，不断言日志文本。论文目录建在 `tmp_path`，不在仓库内。替身只用 monkeypatch（`tongtu.model.ask` / `work`、`subprocess.run`），不建 Fake 类。ci.yml 只挂 `text/`；`compile/` 与 `llm/` 本机用 `pytest -m` 手动跑，结果贴进交付报告。
- 写法取最短能用的那条路，按顺序问：这段要不要存在（卡片没要求就不建）→ 仓库里已有的函数、model、写法先复用，旧代码里能用的部分保留 → 标准库能做的不自写 → 已装的依赖能做的不自写 → 最后才写新代码。不写只有一个实现的抽象、只产一种产品的工厂、为将来准备的脚手架、只有一处会变的配置项。与 ponytail 工作方式同义，开着它即可。

### 架构与开发策略

- 依赖方向单向：阶段模块依赖产物 model、模型调用层与共用模块；阶段之间不互相 import；共用模块（`tongtu/` 顶层按名词命名的文件）不依赖任何阶段。
- 第一次写不抽象：只有一个调用者的逻辑写在当地。第二次出现相近需求时停下来判断：确是同一逻辑，抽到共用模块，两处都改用，必要时改结构；相似但不同，各写各的，不硬凑参数化。不许为省事直接 import 另一个阶段的内部函数。判断写进任务书或 PR 描述。改结构的改动不算越界，是该做的改动。
- 阶段之间只靠文件通信：输入输出都在工作目录，阶段之间不传内存对象，因此每个阶段能单独重跑、单独测。
- 每个阶段一个入口：`run(workdir, …) -> Manifest`，其余都是模块内部函数。
- 纯函数与 IO 分开：文本到文本的逻辑写成纯函数直接测；读写文件、跑子进程、调模型集中在入口附近，测试用 `tmp_path` 与 monkeypatch。
- 失败只有一条线：预期失败进 manifest 的 status 与 message，程序错误抛异常。
- 先读后写：写一个阶段前先读它的卡片、仓库里对应的旧代码、V2 原型里对应的脚本；有已经做了这件事或相近事的代码，就在它基础上改，不另写一份。
- 失败分支与重试在 V0 范围内随主路径一起做完，同一个 PR 交付，不留 TODO。
- 输出符合卡片写了的 output 结构体与状态值，不悄悄修改。

### Review 清单

正确性：

1. diff 只动了任务书列出的文件，没有附带改别处。
2. 改动文件里没有注释与 docstring（工具指令除外）。
3. 名字与字段与提案图那张卡一致，没有多出来的东西；有偏离的地方已经按总则问过用户。
4. 测试存在，覆盖卡片写的每个出口与每个状态值；测试输出是 subagent 贴的原文且全绿。
5. `make lint` 全绿。
6. 没有新依赖；没有在仓库里建论文目录；没有让 agent 会话拿到 `tongtu` 可执行文件。

设计与复用：

7. 代码中是否已有能做这件事或相近事的代码的地方？当前实现是否是在它基础上改进或是抽象，而不是另写的一份。改进或抽象的方向是否正确？
8. 没有为卡片没写的情况预留结构；没有无意义的抽象与一次性的配置项。
9. 依赖方向合规：没有跨阶段 import；出现第二个相近需求的地方有明确的抽或不抽的判断。
10. 失败路径按规范走 manifest 的 status，没有用异常替代。

### 交付报告

每步结束主 agent 给用户（按需提供）：

1. 完成了什么：对应哪张卡，改了哪些文件，删了哪些。
2. 怎么运行：从零开始的命令，带一个验证集里的论文编号作示例，预期的结果；如果可以的话提供 agent 在测试中已经跑好的路径；也可以加上如何构造一个错误case，展现报错场景。
3. 输入输出在哪：工作目录下哪些是输入、哪些是产物、各打开看什么；manifest 各字段怎么读。
4. 验证集结果：跑了哪几篇、状态、耗时；失败的列原因。
5. 没做的与待验证的。

### 验证集

agent 用的论文，用户另留自己的论文做测试：`1412.6980`、`1701.06538`、`2002.05202`、`2106.04426`、`2409.19606`、`2412.19437`、`2512.02556`、`2512.24880`、`2604.15804`，加 `examples/papers/` 的 article / conference / revtex 三篇模板论文。旧版产物已移到 `~/.local/share/tongtu.v0.0820.bak`，新代码从 fetch 重跑，不兼容旧 manifest。

## 调研怎么记录

对外部项目、方案、工具的调研，流程固定：

1. 报告正文发布成 Artifact（claude.ai 的私有页面），不写进仓库——仓库里的文件会成为之后所有 agent 的默认上下文，调研过程不该占这个位置。
2. 同时开一个 issue（标签 `question`），写明调研动机、Artifact 链接与结论摘要；结论定下后关闭 issue，不删，需要回溯时从 issue 找链接。
3. 调研得出的、需要落地的改动，按既有流程改提案图或写进步骤 issue，并注明出处 issue 编号；报告原文不搬进仓库。

## 行文与命名规范

技术文档、代码注释、标识符、CI 与测试命名一律平实直述：

1. 禁止用比喻、拟人来命名或解释机制。说它是什么、做什么，不说它「像什么」。
2. 禁止口语衬词与轻佻语气。写动作本身：「同时写出」而非「顺手落一份」，「检查环境」而非「探一眼」。
3. 不要过于缩写，不要自造缩写，不要为了省事写压缩句或不写主语。多写几个字，写成完整的语句，让含义更清晰。
4. 措辞自查无效力，判据是 `scripts/diction_lint.py`（denylist 在 `scripts/diction_denylist.toml`，本地 hook 与 CI 跑同一份）。抓到新口癖就往 denylist 加一条，清单只增不删；存量违例记录在 `scripts/diction_baseline.json` 基线，命中数只许减少。
