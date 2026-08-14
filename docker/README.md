# docker/

通途的**参考镜像**：TeX Live full + 图渲染工具 + 通途本体。构建定义在
[`Dockerfile`](Dockerfile)，发布流水线在
[`.github/workflows/release-image.yml`](../.github/workflows/release-image.yml)。

## 三个角色（架构 §10）

docker **不是 CLI 的运行前提**——本地原生（PATH 里有 xelatex / latexmk / latexpand 与一款
中文字体）是主形态，`tongtu doctor` 负责弥合环境差异。镜像存在是为了三件事：

| 角色 | 谁在用 | 说明 |
|---|---|---|
| 云部署单元 | wenshu（一期） | Cloudflare Containers 只认镜像；按 git tag 引用某一版 |
| CI 环境 | 本仓库的编译层 job | TeX 发行版差异是真实的坑，CI 一律在镜像里跑 |
| **参考环境** | 所有人 | **「编译通过」的权威裁决环境**——见下 |

> **编译通过的权威裁决以镜像内复现为准。**
> 本机编译过了、镜像里过不了，算不通过（八成是本机多装了什么宏包）；本机编译不过、镜像里
> 过得了，是本机环境问题，不进 issue 的「翻译 bug」类目。bug 报告与产物合规请附上镜像
> tag 与镜像内的复现命令。

## 镜像里有什么

- **TeX Live full**（不裁，架构 §13）：xelatex / latexmk / latexpand / epstopdf 齐全。
- **图渲染工具**（figures 阶段真渲染要用）：`pdftocairo`（poppler-utils）、ghostscript
  （epstopdf 的后端）、ImageMagick（位图分支）。
- **Python + uv**：解释器由 uv 托管（不吃发行版 python3 的版本运气），依赖按 `uv.lock`
  锁定；venv 在 `/opt/tongtu/.venv`，已在 `PATH` 上，故直接有 `tongtu` 与 `pytest`。
- **中文字体**：`fonts-noto-cjk`（探测链第二档）+ 仓库自带的霞鹜文楷（第三档，经
  `/etc/fonts/conf.d/99-tongtu-fonts.conf` 注册进 fontconfig，`fc-list` 真能查到）。
- **仓库本体**在 `/opt/tongtu`（editable 装），工作目录默认 `/work`（`TONGTU_HOME=/work`）。

构建的最后一步是 `tongtu doctor`：三件套或字体链有一样不在，镜像根本构建不出来。

## 本地构建

构建上下文是**仓库根**，不是本目录：

```bash
# 仓库根执行
docker build -f docker/Dockerfile -t tongtu:dev .

# 锁 TeX 版本 / 换 Python 或 uv 版本（三个 ARG 都可覆盖）
docker build -f docker/Dockerfile -t tongtu:tl2025 \
  --build-arg TEXLIVE_TAG=TL2025-historic .
```

首次构建要拉 TeX Live full，**约 6GB、十几分钟起步**，磁盘留够 20GB。之后改代码只重跑
代码层（依赖也只在 `pyproject.toml` / `uv.lock` 变了才重装）。

## 拉现成的

```bash
docker pull ghcr.io/yhnocoder/tongtu:latest     # 最新发布
docker pull ghcr.io/yhnocoder/tongtu:0.1.0      # 某个 tag（git tag 即版本）
```

## 跑

论文工作目录不在仓库里（架构 §5）。镜像内 `TONGTU_HOME=/work`，把宿主的目录挂到 `/work`
即可——`build/` 与 `out/` 都落在挂载卷上，容器随时可丢、下次原样重跑即断点续跑。

```bash
# 环境自检
docker run --rm ghcr.io/yhnocoder/tongtu:latest tongtu doctor

# 跑一篇（真 agent 要另外注入凭据，见下一节；默认 agent=mock 只做恒等翻译）
docker run --rm \
  -v "$HOME/.local/share/tongtu:/work" \
  ghcr.io/yhnocoder/tongtu:latest \
  tongtu run 2401.01234 --json

# 本地源码目录：把它一起挂进去，target 用容器内路径
docker run --rm \
  -v "$HOME/.local/share/tongtu:/work" \
  -v "$PWD/paper:/paper:ro" \
  ghcr.io/yhnocoder/tongtu:latest \
  tongtu run /paper

# 编译层 e2e 在参考环境里复现（架构 §12 层 2；仓库在 /opt/tongtu）
docker run --rm -w /opt/tongtu ghcr.io/yhnocoder/tongtu:latest pytest -m compile

# 用镜像里的 TeX，跑**本机当前工作树**的代码（改一行立刻在参考环境里验）。
# 镜像把 UV_PROJECT_ENVIRONMENT 指向 /opt/tongtu/.venv，所以这个 uv sync 建的 venv 在
# 容器里，不会往挂进来的宿主工作树里塞一个 Linux venv。
docker run --rm -v "$PWD:/src" -w /src ghcr.io/yhnocoder/tongtu:latest \
  bash -c 'uv sync && uv run pytest -m compile'

# 进去排查
docker run --rm -it -v "$HOME/.local/share/tongtu:/work" \
  ghcr.io/yhnocoder/tongtu:latest bash
```

镜像**不设 ENTRYPOINT**：它同时是 CI 环境与参考环境，除了 `tongtu run` 还得能直接跑
`pytest` 与 `bash`，所以命令一律写全。

### agent 运行时不在镜像里

镜像只管确定性的那一半（TeX + 通途）。agent 运行时（Codex CLI 等）是每次运行的选择，
凭据也各不相同，故不打进镜像：真跑时把运行时装进容器或换用带运行时的派生镜像，并把
API key 以环境变量注入（`-e OPENAI_API_KEY=...`）。CI 里的做法见
[`llm-quality.yml`](../.github/workflows/llm-quality.yml) 的「装 agent 运行时」一步。

## 发布（GHCR）

镜像发布由 [`release-image.yml`](../.github/workflows/release-image.yml) 负责，
**git tag 即版本**（架构 §10）：

| 触发 | 打出的镜像 tag |
|---|---|
| push tag `v1.2.3` | `1.2.3`、`1.2`、`latest` |
| 手动 workflow_dispatch | `dev`（或输入里给的名字）；`push=false` 时只构建不推，用来验证 Dockerfile |

层缓存走 GitHub Actions cache（`cache-from/to: type=gha`），基底层命中后每次发布只重跑
代码层。撞上 GH 10GB 缓存配额时，把 `cache-to` 换成 registry 缓存（workflow 里有注释写
明改法）。推送权限来自 workflow 的 `packages: write` + 内置 `GITHUB_TOKEN`，不需要额外
secret。

## CI 与 LLM 质量层

- **编译层**（[`ci.yml`](../.github/workflows/ci.yml)）：目前跑官方 `texlive/texlive:latest`。
  参考镜像发布后改 `container:` 一行即可切过来——那个 job 对镜像的全部要求就是 PATH 里有
  latexmk 与 latexpand。
- **LLM 质量层**（[`llm-quality.yml`](../.github/workflows/llm-quality.yml)）：**手动触发**，
  真模型跑 1–3 篇，把 report.json 的关键指标（回退块数、校验重试、agent 干预数、编译警告）
  汇总进 job summary。**是质量监控不是门禁**：只绑 `workflow_dispatch`，不绑 PR 事件，单篇
  跑挂也不 fail job。默认镜像是 `texlive/texlive:latest`（参考镜像发布前的 fallback），
  dispatch 时填 `ghcr.io/yhnocoder/tongtu:latest` 即可切换。

真跑要在仓库配 secrets（Settings → Secrets and variables → Actions）：

| secret | 何时需要 |
|---|---|
| `OPENAI_API_KEY` | `agent=codex`（当前默认运行时）；**不配就跑不出有意义的数据** |
| `ANTHROPIC_API_KEY` | 将来接 Claude Code 一类运行时时 |

没配 key 也不会炸：agent 拉起失败 → 流水线按设计回退原文出包 → 表格里是「全回退」，那是
一次冒烟，不是质量数据。

## 已知取舍

- **~6GB 不裁**：arXiv 论文的宏包不可预测，为省磁盘引入一类新的编译失败不划算（继承 v2
  结论，架构 §13）。
- **`latest` 基底**：`TEXLIVE_TAG` 默认 `latest`，图的是宏包新；要可复现到某年某月就锁
  TL 快照 tag 重新发一版。
- **ImageMagick 的 Debian 策略**：`policy.xml` 默认禁 PDF/PS 的读写。通途只用它处理位图
  （PDF/EPS 走 pdftocairo + epstopdf），故不受影响——真要在容器里手动 `convert` 一张 PDF
  会被拒，那是策略不是缺工具。
- **不做服务化**：v2 的 `texsvc`（远端编译服务）不迁（PHASE0 §2.1）。镜像只当三角色，
  通途永远只读写本地工作目录、stdout 与退出码。
