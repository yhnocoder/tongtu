# 零期工作清单

> 状态：v0.1（2026-08-13）。本文是零期（v2 原型迁入 → 通途引擎可用）的施工清单，工程设计依据为 [ARCHITECTURE.md](ARCHITECTURE.md)（v0.7）；冲突时以架构文档为准。参考实现：
>
> - **v2**（[arxiv_translator_v2](https://github.com/yhnocoder/arxiv_translator_v2)）：LaTeX 主线直接前身——三级掩码、机械校验、编译回环、xeCJK 注入均已验证可用，是零期迁移的主要来源。
> - **v1**（[arxiv_translator_v1](https://github.com/yhnocoder/arxiv_translator_v1)）：doc2x PDF→markdown 路线，对应本仓库 `fallback/` 降级流水线的参考，零期只占位不实现。

## 1. 零期目标与验收判据

零期交付「通途引擎在本机可用」，验收全部是机械判据：

1. **恒等翻译 e2e 全绿**（架构 §12 层 2）：MockAgent 原样返回源文，三篇 fixture 论文全流水线跑到底，产出 PDF + anchors 并通过 schema 校验——这是零期交付判据的 CI 化身，进 PR 门禁。
2. **文本层测试全绿**（§12 层 1）：mask/unmask/validate/chunk 的 golden-file 测试 + mask/unmask 往返恒等性质测试，进 PR 门禁。
3. **真实论文跑通**：`tongtu run <arxiv-id>` 用真 agent 翻译至少 1 篇真实论文，产物包契约文件齐全、schema 校验通过、`zh.pdf` 在参考镜像内编译通过。
4. **检验页可验收**：`tongtu preview` 打开 `report.html`，anchors 热区画得出来、点击可见原始 TeX（anchors 不可视化就无法验收，见 §11）。
5. **`--json` 事件流 schema 草案落地**（一期容器调度前冻结，零期先出草案）。

## 2. v1/v2 资产盘点

### 2.1 v2 → 通途 迁移映射

| v2 资产 | 处置 | 说明 |
|---|---|---|
| `scripts/fetch.py` | **拆分改造** | 拆为 fetch（下载解包 + PDF-only / pdfpages 套壳检测）与 flatten（latexpand + `--expand-bbl`）两阶段；主文件识别从「第一个含 `\documentclass` 的文件」升级为歧义时拉起关节①判定 |
| `scripts/mask.py` | **迁移 + 审计 + 扩展** | 零依赖词法状态机继承（选型已决）；迁入时审计。新增：环境完备枚举、数据文件形式的分类表、`\newtheorem` 声明自动归类、未知环境走关节③（不确定则保守整块掩码）、**运行时往返自检**（`unmask(mask(x)) == x` 恒等，不过不放行）、blocks.json 扩展出类型 / label / 源码位置字段 |
| `scripts/chunk.py` | **重写** | v2 按字符数（2000–4500）+ 段落边界切；通途要求章节树优先（`\section` 首选单元、小节聚合、超大节下分）、以掩码后散文 token 计数、软目标 ~4k / 硬上限 ~8k（fixture 校准） |
| `scripts/validate.py` | **基本直迁** | 四层机械校验（占位符 multiset / 控制序列 multiset / 括号与 `$` 计数 / 段落数）原样保留 |
| `scripts/unmask.py` | **迁移 + 扩展** | 新增按块类型**参数化回填**：survey 的选择性回填视图（数学类回填原文、表格/图/代码保持占位符）复用同一机制 |
| `scripts/inject_cjk.py` | **迁移 + 扩展** | xeCJK + 字体探测链（Hiragino → Noto → 霞鹜文楷）保留；documentclass 适配表数据文件化，供关节⑥沉淀经验 |
| `scripts/compile.sh` | **重写为阶段驱动器** | 并入 unmask 回填 + inject_cjk（assemble 已并入 compile，决策 13）；新增失败分诊（坏段 vs 全局问题）、块 → 段落两级二分、坏段重译一次再失败回退原文、关节⑥适配与修复会话 |
| `scripts/package.py` | **改造** | 「zh.tex 自包含 pack」思路进 export 阶段（产物契约要求 `zh.tex` 自包含） |
| `scripts/export.sh` / `export_pre.py` / `typst-style.typ` | **不迁** | pandoc → md/typst 有损导出不在产物契约内；export 阶段职责改为产物包组装 + anchors 合成 + 检验页 + schema 自校验 |
| `scripts/run.sh` | **替换** | `claude -p` + SKILL 驱动的编排撤销（决策 1）；控制流移入 `tongtu run` 编排器，agent 只在六个关节被有界拉起 |
| `scripts/stream_filter.py` | **替换** | → `--json` 机器可读事件流（schema 零期出草案） |
| `skill/SKILL.md` | **迁移降级** | 编排职责删除（流程步骤、目录约定移入代码）；翻译规则、术语 schema、文风约定、常见坑保留为 prompt 资产落 `skill/`，加版本号（`prompt_version` 入缓存 key） |
| `docker/Dockerfile` | **改造** | 双层构建：TeX Live full 基底层（不裁，继承 v2 结论）+ 通途代码层；GHCR 发布，git tag 即版本 |
| `docker/texsvc.py`（远端编译服务） | **不迁** | 通途不做服务化；docker 只当云部署单元 / CI 环境 / 参考环境三角色 |
| `worker/`（Cloudflare 信箱）、`mailbox.sh`、`install-launchd.sh` | **不迁** | 云调度、任务队列、上传全部是 wenshu 侧职责（决策 4，通途不感知云） |
| `fonts/`（霞鹜文楷 Light/Medium） | **直迁** | 随仓库分发，编译与打包用相对路径 |
| 工作目录约定（`<repo>/<arxiv_id>/` + `inter/`/`output/`） | **替换** | 论文目录移出仓库：`~/.local/share/tongtu/<arxiv_id>/` 下 `src/` / `build/`（可整体删除）/ `out/` / `logs/`（§5） |

### 2.2 v1 → 通途

| v1 资产 | 处置 | 说明 |
|---|---|---|
| `download.py`（arXiv 各类 URL → id 解析与 PDF 下载）、`translate_pdf.sh`（doc2x PDF→md→翻译） | **零期占位** | 对应 `fallback/` 非 arXiv PDF 降级流水线；零期只做 PDF-only 检测报错并标记降级路线，fallback 实现后置 |
| `upload_images_to_r2.py` / `upload_images.sh` | **不迁** | R2 上传是 wenshu 侧职责 |
| markdown 路线的问题清单（PDF/EPS 图不可用、排版错乱） | **经验输入** | 已内化为架构决策 9（figures 独立阶段）与「不解析 PDF」的立项动机 |

## 3. 工作分解

标注：〔迁〕自 v2 迁移或改造，〔新〕零期新建。

### 3.1 工程骨架〔新〕

- [x] Python 3 + uv 项目骨架（v2 零第三方依赖传统尽量保持），`tongtu` 入口
- [x] 目录落地：`tongtu/`（含 `stages/` 与 `agent/` 子模块）`skill/` `docker/` `fallback/`（占位）`docs/schemas/`——流水线代码落 Python 包 `tongtu/` 而非 `scripts/`，README 规划结构已同步
- [x] 工作目录解析：`$TONGTU_HOME` / `--workdir` / 默认 `~/.local/share/tongtu/<arxiv_id>/`，四区布局 `src/ build/ out/ logs/`

### 3.2 确定性流水线（`scripts/`，按阶段）

- [x] **fetch**〔迁〕：e-print 下载解包、PDF-only 与 pdfpages 套壳检测（报错并标记降级路线）
- [x] **flatten**〔迁〕：latexpand 展开 + `--expand-bbl`；主文件歧义 → 关节①
- [x] **baseline**〔新〕：latexmk 原样编译原文，隔离环境问题；失败 → 关节②（workdir 内修构建环境），仍失败终止——这是最早的编译门控，编译不过的论文不花一分钱 LLM
- [x] **mask**〔迁 + 扩展〕：见 §2.1 映射行；分类表数据文件 + 关节③ + 保守默认 + 往返自检
- [x] **survey**〔新〕：masked.tex 选择性回填视图（剔除附录与参考文献）一次通读 → `brief.json`（abstract 照录 + 章节树 + 记号约定 + 文风基调）+ 术语预扫决策（关节④）；术语表三层合并（全局 XDG → 论文目录内 `<workdir>/glossary.json` → `--glossary`，`tongtu/glossary.py`）；产物过 schema 校验（`tongtu/schema_check.py`，与 e2e 共用同一校验器）。模型输出防御性解析、失败重试一次、再失败降级为确定性骨架（章节树 + 原文摘要 + 零术语增补）——**survey 失败不阻塞流水线**，brief 是增益不是门禁
- [x] **chunk**〔重写〕：章节树优先分块，软目标 / 硬上限以 token 计，绝不切入环境或段落
- [x] **translate**〔新驱动 + 迁校验〕：块循环、上下文组装（brief + 命中术语 + 邻域原文，刻意不传前块译文）、缓存查询（`tongtu/memory.py` 的翻译记忆，命中即免调用）、validate 内环重试、关节⑤（`complete` 原语）；内环抽成 `translate_body`，compile 的坏段重译（`retranslate_segment`）复用同一份 validate 出口判据
- [x] **compile**〔重写〕：unmask 回填 → inject_cjk（查适配表）→ latexmk -xelatex 回环；失败分诊、块 → 段落二分、坏段重译一次再回退原文（保证永远出 PDF）、关节⑥
- [x] **figures**〔新〕：EPS/PDF/位图 → PNG 预渲染（长边 ≈1568px 定 DPI，位图只缩不放）、caption 与引用段落收集；仅依赖 `src/`，与翻译轨并行，逐图 hash 缓存（渲染器可注入，本机无 pdftocairo / ImageMagick 时如实记 `skipped` 降级，少一张图从不拦流水线）
- [x] **export**〔新，吸收 v2 package.py〕：产物包组装（`out/` 契约文件 + `zh-pack/` 自包含编译包，资产取自编译目录、不猜名字）、anchors 三来源合成（`tongtu/anchors.py`：零依赖 synctex 解析 + blocks 行号映射 + PDF 页面扫描；无 synctex 即退化为页级锚点并如实标注来源）、`report.json` 落盘（含新加的 `compile.inject` 段，契约 bump 至 0.2）、检验页生成、**契约 schema 自校验即出口判据**（任一不过判 failed）
- [x] **增量模型**〔新〕：阶段级 `build/manifests/<stage>.json`（输入 hash → 跳过）；块级翻译缓存（key = 块源码 + 邻域 + 命中术语 + brief_hash + style_version + prompt_version + model_id），权威翻译记忆落产物 `chunks.json`——`tongtu/memory.py` 负责装载（`out/` 权威 + `build/` 工作副本）、写回与失效，`build/` 整体删除后仍能从 `out/chunks.json` 全量命中；`--force` 连块级缓存一起无视

### 3.3 agent 适配层（`agent/`）

- [x] 两原语接口〔新〕：`complete(prompt, text, model)` / `session(prompt, workdir, model, budget)`；`session` 的 done 不作裁决依据，转录一律落 `logs/`
- [x] **MockAgent**〔新〕：`complete` 恒等返回、`session` no-op——编译层 CI 的钥匙
- [x] **Codex CLI 适配器**〔新，选型已决〕（`tongtu/agent/codex.py`）：`codex exec` headless 拉起、`--sandbox` + `-C` 圈权限（继承 v2 run.sh 的 allowlist 思路）、指定模型、超时、转录落 `logs/`；`complete` 首发同走运行时（read-only 沙箱 + 输出清洗）；argv 段模板可覆盖、runner 可注入；`get_agent(name)` 工厂 + `tongtu run --agent`
- [x] 六关节接线（全部在 `tongtu/pipeline.py`，阶段驱动器只声明回调）：①主文件（flatten 的 arbiter，提示词内联）②构建环境（baseline 的 session ← `as_session_fn` / `skill/repair.md`）③环境分类（mask 的 arbiter + `skill/classify.md`，结论进 blocks.json 的 `decided_by="agent"`）④通读与术语（survey 的 complete）⑤翻译（块循环 + compile 坏段重译）⑥适配与修复（compile 的 session）；每次拉起记一条干预（形状 = `report.schema.json` 的 `agent_interventions`），攒在 `PipelineResult.interventions`，**outcome 一律由事后的校验与编译裁决**，落盘属 M4

### 3.4 prompt 资产（`skill/`）

- [x] SKILL 瘦身迁移〔迁〕：删编排，留翻译规则 / 占位符纪律 / 术语与文风 / 常见坑；按关节拆成 `skill/translate.md`（⑤）、`skill/repair.md`（②/⑥，含适配表沉淀指引）、`skill/classify.md`（③）、`skill/survey.md`（④）
- [x] `prompt_version` / `style_version` 版本化，进缓存 key（单一来源 `tongtu/prompts.py`，装载 + wheel 打包链）

### 3.5 产物契约与 schemas（`docs/schemas/`）〔新〕

- [x] 逐文件 schema 草案：`blocks.json` `anchors.json` `chunks.json` `brief.json` `glossary.json` `report.json` + figures 元数据；`contract_version` 字段与变更流程（先改 schema 再改码）
- [x] `report.json` 含 agent 关节干预统计（六关节 enum + promotable 标记）——促升规则的数据来源
- [x] `--json` 事件流 schema 草案（阶段起止、块进度、最终结果）

### 3.6 静态检验页（`report.html`）〔新〕

- [x] PDF.js vendor 随包渲染 `zh.pdf`（pdfjs-dist 3.11.174 legacy 经典构建，1.5 MB，落 `tongtu/data/report_page/vendor/pdfjs/`）；anchors 热区半透明覆盖（页级降级画虚线），点击显示原始 TeX；侧栏回退块与校验统计、agent 干预、figures 索引；数据走 data-as-JS（`report-data.js` 内嵌 zh.pdf 的 base64），file:// 双击可开，http(s) 下走相对路径 fetch 快路
- [x] 红线：无网络可开（零外链有测试扫描把关），永不加需要 server / LLM 的功能——写进模板注释与 `app.js` 抬头

### 3.7 测试与 CI〔新〕

- [x] fixtures：自造最小模板论文三篇（article / revtex / 双栏会议）入仓库
- [x] 文本层：golden-file + 往返恒等性质测试（PR 门禁）
- [x] 编译层：MockAgent 恒等翻译 e2e（M2 已落双形态：本机假工具 + CI texlive 镜像 pytest -m compile，PR 门禁；M4 补齐产物包断言——契约文件齐全 + 全部过 schema + anchors 页级降级路径 + 检验页含 vendor 引用）
  ——**伪翻译变体已补齐**（零期收尾）：`get_agent("pseudo")` 给每个散文段前缀一句固定中文
  （零 LLM、零随机，删掉前缀即逐字节回到原文，validate 四层一层不动），
  `tests/test_e2e_pseudo.py` 与恒等 e2e 同参数化跑三篇 fixture；**真 TeX 形态即 xeCJK 断行
  与 CJK 字体链的覆盖点**（日志里出现 `There is no 以 in font` 即豆腐，当场红）。附录 B
  开放问题 2 就此落定为「伪翻译变体」而非另造中文 fixture。CI 镜像切换见下一条
- [x] LLM 层：手动触发 workflow，真模型 1–3 篇，report.json 入质量看板（监控不门禁）——
  `.github/workflows/llm-quality.yml`：只绑 `workflow_dispatch`（不绑 PR 事件，**结构上**
  不可能标红 PR）、最多 3 篇的预算闸、单篇失败不 fail job；输入 = arXiv id 列表 / 模型 /
  运行时（默认 codex）/ 镜像；汇总表（回退块、校验重试、agent 干预、编译警告）进
  `$GITHUB_STEP_SUMMARY`，report.json 缺席时降级读 `--json` 事件流。**首跑验证待 dispatch**
  （需先配 `OPENAI_API_KEY` secret，见 `docker/README.md`）
- [ ] CI 一律跑参考镜像；镜像层缓存——发布侧的层缓存已落地（release-image 的 `type=gha`）；
  ci.yml 的编译层仍跑 `texlive/texlive:latest`，**待首个 tag 把参考镜像推上 GHCR 后改
  `container:` 一行**切过去（llm-quality 已可用 `image` 输入先行验证参考镜像）

### 3.8 docker（`docker/`）〔迁 + 改〕

- [x] 双层 Dockerfile：TeX Live full 基底 + 通途代码层；含 latexmk / latexpand / 字体 / Python
  ——`docker/Dockerfile`：基底层 `texlive/texlive:latest`（TeX Live full 不裁）+ poppler-utils /
  ghostscript / ImageMagick（figures 真渲染）+ fonts-noto-cjk + uv 托管的 Python；代码层
  `uv sync --frozen` + 仓库字体经 fontconfig 注册（探测链第三档真命中）+ 构建末尾跑一次
  `tongtu doctor` 自检。三角色说明与运行命令见 `docker/README.md`。**构建验证待首跑**
  （本机无 docker daemon，首次构建在 release-image 的 `push=false` 手动触发上做）
- [x] GHCR 发布流水线，git tag 即版本——`.github/workflows/release-image.yml`：push tag `v*`
  → `{{version}}` / `{{major}}.{{minor}}` / `latest` 推 `ghcr.io/yhnocoder/tongtu`；另有
  workflow_dispatch（可 `push=false` 只构建）；buildx `type=gha` 层缓存 + `packages: write`。
  **首跑验证待 tag / dispatch**

### 3.9 CLI 命令面〔新〕

- [x] `tongtu run`（幂等，`--force` / `--json` / `--glossary` / `--workdir`；退出码 0 = 出包）
- [x] `tongtu retranslate`（`--chunks` / `--term` / `--all`，走块级失效重算：删缓存条目 → translate 必算 → compile 及下游按 manifest 判；上游一律装载不重算；退出码语义同 `run`）
- [x] `tongtu stage`（单阶段调试入口）、`tongtu doctor`（探测 xelatex / latexmk / latexpand / 字体）、`tongtu preview`（定位 `out/report.html` 并用 `webbrowser` 打开；`--serve` 起本地 http.server；headless 打不开浏览器则打印路径退 0）

## 4. 建议施工顺序

依赖关系驱动，每个里程碑有独立验收：

| 里程碑 | 内容 | 验收 |
|---|---|---|
| **M0 骨架** | §3.1 + schemas 草案（§3.5）+ CI 空跑 | `tongtu doctor` 可运行；CI 绿 |
| **M1 文本层** | mask 迁移审计 / validate / unmask / chunk 重写 + golden 测试 | 文本层 PR 门禁生效；真实论文 `mask→unmask` 往返恒等 |
| **M2 编译链** | fetch / flatten / baseline / inject_cjk / compile 回环 + fixtures 三篇 + MockAgent | **恒等翻译 e2e 门禁生效**（零期核心判据） |
| **M3 真翻译** | agent 适配层（Codex CLI）+ survey + translate + glossary + 缓存与 manifests | 真实论文出 `zh.pdf`；`retranslate --term` 只重翻命中块 |
| **M4 产物闭环** | figures / anchors / export / report.html / preview | 产物包契约齐全过 schema；检验页热区可点 |
| **M5 镜像与收尾** | docker 双层 + GHCR + LLM 层手动 workflow + 真实论文批量试跑 | 镜像内复现全流程；`--json` schema 草案定稿 |

M1 与 M2 之间可并行推进 fixtures 制作；figures（M4）只依赖 `src/`，可提前。

## 5. 零期不做（边界）

- `fallback/` 降级流水线实现（v1 路线重写）——只留 PDF-only 检测与降级标记
- 一切云侧：R2 上传、任务队列、容器调度、信箱（wenshu 职责）
- md / typst 有损导出（不在产物契约）
- nightly LLM 回归（待朝晖接入后挂真实论文流）
- 交互阅读器 / 任何需要 server 或 LLM 的展示功能（归文枢）
- 修复 agent 的沙箱隔离选项（文档言明信任姿态，后续再加）

## 6. 开放问题（实测校准，非设计阻塞）

同 [ARCHITECTURE.md](ARCHITECTURE.md) 附录 B：chunk 软/硬上限数值（M2–M3 用 fixture 校准）、`--json` schema、anchors 叠加次序与热区容差（M4 拿真实论文实测）、brief 字段粒度与邻域段数。恒等翻译的中文路径覆盖方式已落定为**伪翻译变体**（零期收尾，见 §3.7 编译层）。
