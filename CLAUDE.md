# 通途（tongtu）

## 目标

通途把 arXiv 论文的 LaTeX 源码翻译成中文，编译出保留原文排版与公式的 PDF。名字取自「天堑变通途」。

译文合格的标准是：用户愿意用这份中文 PDF 代替英文原文来读。具体要求有四条：术语准确；句子按中文习惯组织，不照搬英文语序；公式、图表、脚注完整保留；版式与原文一致。

项目只供用户自己使用，不考虑其他用户的需求。评估一个设计选择时，看它能否提高译文质量、让用户更愿意读中文 PDF。开发阶段不为节省成本降低效果。

## 协作方式

用户与 AI 平等协作。对 AI 的要求：

- 主动提出能改进译文或工具的想法。
- 发现方向、方案或文档有问题，直接指出并给出理由。
- 被质疑时认真回应，不为文档或已有代码辩护。文档记录的是写下时的共识，可以修改。遇到按文档做不下去的情况，停下来报告冲突并给出替代方案，由用户当场决定是否修改；决定修改后，文档与代码一起改。
- 报告为完成的工作必须已经验证过。没做的、跳过的、不确定的，在报告里写明。
- 开发顺序：先让论文产出中文 PDF，再提高译文质量，最后让结果可以稳定复现。只处理已经实际出现过的问题，不为设想中的问题提前设计方案。

## 基本原则

1. **LaTeX 源码是翻译的依据**：翻译 arXiv 提供的 e-print 源码，不解析 PDF。编译能否通过由机器检查，作为硬性指标。
2. **agent 做翻译，脚本做校验**：翻译由 agent 完成，结果是否正确以校验脚本的结果为准。脚本不对翻译过程做细粒度控制，例如不按段重试后再拼接。要提高译文质量，就修改提供给模型的信息（skill、上下文、术语表），不增加脚本逻辑。
3. **修复先用规则，agent 只处理规则处理不了的情况**：precompile 与 compile 的修复会话（`precompile_fix`、`compile_fix`）只在规则处理失败后调用。调用 agent 成本高、耗时长，每次结果也不完全一样，这类流程里应尽量不调用 agent。修复会话的日志用来发现共性问题：同一类修复在多篇论文里重复出现、处理方式不依赖具体论文时，把它写成前面阶段的规则，之后这类问题不再进入修复会话。例如 #140 把 7 篇论文的修复会话都在处理的 pdflatex 遗留写成了 `tongtu/preamble.py` 里的规则。
4. **论文工作目录不在仓库内**：默认路径是 `~/.tongtu/papers/<arxiv_id>/`，可以用 `$TONGTU_HOME`（换整个目录，含 `config.toml` 与 `glossary.json`）、`--dev`（等于 `TONGTU_HOME=~/.tongtu-dev`）或 `--workdir` 修改。测试与试验也不在仓库里创建论文目录。
5. **agent 运行时可以替换**：适配层在 `tongtu/model/`，不依赖具体产品。直接调用 API，或者启动 Claude Code、Codex，都是可选的实现方式。

## 当前状态

- v0.1 已完成。fetch → precompile → mask → survey → translate → review → compile 七个阶段全部实现，`tongtu run <arxiv_id>` 可以在验证集上生成中文 PDF（`out/<工作目录名>.zh.pdf`）。
- v0.2 还在设计，方向未定。候选方向记录在 GitHub Issues 中（`gh issue list --state open`），新会话先查看。

## 工作流程

整体流程是 Design -> Graph[Task(Spec -> Implement -> Test -> Validate)]：先写 Design，再拆出多个 Task，Task 之间可以有依赖；每个 Task 依次经过 Spec、Implement、Test、Validate。

### Design

Design 使用 HTML 文档，放在 `docs/design/`，由用户描述需求和预期，具体内容由用户和 AI 一起维护。文档的格式要求见「文档规范」。

### Task

一个 Task 对应 Design 中的一个模块或功能，也对应一个 GitHub issue。每一步的执行者和是否需要用户确认如下：

| 步骤 | 执行者 | 用户确认 |
|---|---|---|
| 创建 Task issue（label `task`） | 主 agent | 由用户提出 |
| 与用户沟通需求并写 Spec | 主 agent | 用户审核通过后才开始开发 |
| Implement 和 Test | implementer subagent | 不需要 |
| Review | 主 agent | 不需要；同一个 subagent 两轮未通过时报告用户 |
| 偏离 Design Doc、新增 Design Doc 没有的字段或选项、Design Doc 没给数值的常量、增加新依赖 | 主 agent 提出 | 需要 |
| 在 issue 中写 comment | 主 agent | 不需要 |
| Validate | 用户 | AI 提供命令、论文编号和要检查的位置 |
| commit、PR、关闭 issue | 主 agent | 需要 |

补充规则：

- Spec 描述实际的实现细节，要做到自包含，让 subagent 不需要知道其他背景就可以执行；需要其他背景时，在 Spec 中引用。Spec 写在 issue comment 里，不提交到仓库。
- 实现过程中发现 Spec 有误，在 issue comment 中修正。
- Task 之间会形成依赖链，也会发现之前的 Task 有错，这都是正常的，在新 Task 的 comment 中记录。
- Implement 和 Test 的具体规则见 `.claude/agents/implementer.md`。普通任务使用 implementer 默认的 opus 模型；复杂任务在调用时指定 `model: fable`；更复杂的任务可以让多个 fable 和 opus subagent 协作。同一时间只有一个 subagent 修改代码。每个新的 Spec 启动一个新的 subagent；只有 review 退回修改时才继续使用原来的 subagent。
- Review 的依据是本文件和 `.claude/agents/implementer.md`。主 agent 另外检查：diff 只修改了 Spec 列出的文件；仓库里没有新建论文目录；测试输出是 subagent 贴出的原文，且全部通过。退回时指出未通过的条目。
- Implement、Test 与 Review 中需要跑真实论文时，从验证集里按改动范围挑 2 到 3 篇有代表性的论文，不跑全部。跑一篇论文要多次调用模型，消耗大量 token。
- 开发中发现的问题各开一个 issue，加 `question` 标签；得出结论后写进 issue 再关闭，不删除。

### Validate

Validate 由用户执行：跑整篇论文，读中文 PDF。主 agent 给出从零开始的命令、论文编号、预期结果，以及这次改动影响到的位置（哪些页、哪类内容、哪个 manifest 字段）。

验证集：`1412.6980`、`1701.06538`、`2002.05202`、`2106.04426`、`2409.19606`、`2412.19437`、`2512.02556`、`2512.24880`、`2604.15804`，以及 `examples/papers/` 下的 article、conference、revtex 三篇模板论文。主 agent 按改动范围从中挑选，不默认跑全部。

### 小任务

经用户明确同意，小任务可以跳过上面的流程，也可以把多个小任务合并到一个 PR。

## 代码规范

- 代码里不写注释和 docstring，代码的含义靠命名与结构表达。唯一例外是代码看起来可以删除或修改、实际上不能改的地方，写一行 `why(#N): 原因`，必须带 issue 编号，尽量少用。工具指令（`# noqa`、`# type: ignore`、`# pragma: no cover`）不算注释。检查由 `scripts/comment_lint.py` 执行，它只检查相对 main 改动过的文件。
- `make lint` 运行 ruff check、ruff format 与 comment lint，三项都要通过。
- 从 main 切分支。implementer 在独立的 worktree 里修改，因为主工作树上可能有别的会话。
- commit message 的格式是 `<type>(<scope>): <中文一句话>`。type 取 `feat`、`fix`、`refactor`、`test`、`ci`、`docs`、`chore` 之一；scope 是阶段名，或 `model`、`cli` 这类模块名；描述以动词开头，不加句号，不超过 50 字。不加 `Co-Authored-By` 之类的署名行。
- 用户用 squash merge 合并，PR 标题就是 commit 的第一行。PR 描述写该 Task 的输入输出（完善已有功能时分别列出旧的和新的），末尾写 `Closes #N`；其他内容写在 GitHub issue 中。设计文档的改动和代码放在同一个 PR 里。

## 文档规范

### Design Doc

写以下内容：

- 用户能看到的行为：命令、选项、配置格式、重跑规则；
- 每个阶段的任务、状态值，以及每个状态在什么条件下出现；
- 读写的文件与产物的类定义；
- 影响结果的常量，例如阈值、超时、上限、默认值；
- 改动时容易被破坏的设计决定，附一句理由。

以下内容不写：

- 代码里能直接读到的实现步骤、正则、规则名单的具体条目；
- 对 agent 行为的要求，这些写在对应的 `skill/<角色>/SKILL.md`；
- 修改历史、实测过程、调研依据，这些写在 issue 与 commit message；
- 还没决定要做的设想；
- 配置文件模板全文与安装排障，这些放在模板注释或 README。

章节按读者会问的问题划分。上面列的几类内容写在相关章节里，和它们解释的机制放在一起，不单独成章；常量不在文末另做汇总。

文件组织：`index.html` 是索引；`style.css` 是共用样式，组件写法见 `pages/_template.html`，项目新增的组件追加在 `style.css` 末尾；项目文档放在 `pages/`，新文档复制 `_template.html` 开始。`pipeline.html` 暂留根目录，使用 `legacy.css`，等迁移完成后移入 `pages/`。改动 `docs/design/` 后运行 `uv run scripts/check_design.py`，脚本截取桌面、375px、深色三种截图并报告机械性错误，布局仍然要看截图。脚本默认用 Chrome；没有 Chrome 的环境加 `--browser chromium`（Playwright 自带的 Chromium）或 `--browser <浏览器可执行文件路径>`。

### Intro Doc

除了 Design Doc 外，还需要提供给用户看的 Intro Doc。Intro Doc 同样采用 HTML，共享 Design Doc 的样式代码，在 v1.0 之前完成，开始时间由用户指定。

## 行文

平实：

1. 不用比喻或拟人来命名、解释机制，直接写它是什么、做什么。
2. 不用口语衬词和轻佻语气，写动作本身。例如写「同时写出」，不写「顺手落一份」；写「检查环境」，不写「探一眼」。
3. 不过度缩写，不自造缩写，不省略主语，不为了省字写压缩句。把意思写完整。
4. 不用「不是 X，而是 Y」的对照句、破折号、排比和口号式的句子。

文档、README、标识符、CI 与测试的命名、Artifact 和 GitHub issue 都按以上规则写。

## 调研记录

调研外部项目、方案或工具时：

1. 报告正文发布成 Artifact（claude.ai 上的私有页面），不写进仓库。之后的 agent 会搜索和读取仓库里的文件，调研过程写进仓库会混进这些结果。
2. 同时开一个 issue，加 `question` 标签，写明调研动机、Artifact 链接与结论摘要。结论确定后关闭 issue。
3. 调研结论中需要实施的改动，写进 Design Doc 或 feature issue，注明来源 issue 编号。
