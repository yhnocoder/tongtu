# docker/

通途的**参考镜像**：TeX Live full + 图渲染工具 + 通途本体。构建定义在
[`Dockerfile`](Dockerfile)，构建与发布流水线在
[`.github/workflows/image.yml`](../.github/workflows/image.yml)。

`Dockerfile` 分三层，其中两层作为 target 对外：`env` 是环境（TeX Live full、Python 依赖、字体
文件、node 22 与三个 agent CLI，不含仓库代码），CI 的编译层作业以它为 container 跑测试；
`runtime` 是完整镜像，即下表三个角色里说的那个。runtime 阶段写成 `FROM ${ENV_IMAGE}`：
`ENV_IMAGE` 默认指本文件的 env 阶段，本机 compose 构建时指 `ghcr.io/yhnocoder/tongtu:env`
（见 [`remote.html`](../docs/design/pages/remote.html) 第 05 节）。分层的理由是耗时：编译层
作业若在作业内构建镜像，即便 buildx 缓存全部命中，也仍要下载 6GB 级的缓存层并把镜像导出
到本地 docker daemon，实测占去作业八成时间。两个 target 各自何时构建、何时推送，见
[`image.yml`](../.github/workflows/image.yml) 的头部注释。

镜像存在是为了三件事：

| 角色 | 谁在用 | 说明 |
|---|---|---|
| server 与论文容器 | 服务器与 Mac 测试 server | 用同一个镜像，由 compose 启动，见 [`remote.html`](../docs/design/pages/remote.html) 第 04、06 节 |
| CI 环境 | 本仓库的编译层 job | TeX 发行版差异是真实的坑，CI 一律在镜像里跑 |
| **参考环境** | 所有人 | **「编译通过」的权威裁决环境**——见下 |

> **编译通过的权威裁决以镜像内复现为准。**
> 本机编译过了、镜像里过不了，算不通过（八成是本机多装了什么宏包）；本机编译不过、镜像里
> 过得了，是本机环境问题，不进 issue 的「翻译 bug」类目。bug 报告与产物合规请附上镜像
> tag 与镜像内的复现命令。

## 本地构建

本机构建只在 compose 场景下做，`Dockerfile` 的改动一律由 `image.yml` 验证。构建上下文是
**仓库根**，不是本目录：

```bash
# 仓库根执行。ENV_IMAGE 指向已推送的 env 镜像时，BuildKit 只构建 runtime 层，
# 不下载 TeX Live 分片，也不装 node 与 CLI
docker build -f docker/Dockerfile -t tongtu:local \
  --build-arg ENV_IMAGE=ghcr.io/yhnocoder/tongtu:env .

# 从零构建全部三层（换 TeX Live release / Python / uv / node / CLI 版本时各 ARG 都可覆盖）
docker build -f docker/Dockerfile -t tongtu:tl2026 \
  --build-arg TEXLIVE_RELEASE=texlive-2026 .
```

从零构建要下载并解开 release 里的 TeX Live 分片（近 2GB 下载、解开约 6GB），磁盘留够 20GB。
改动涉及 base 或 env 层时，本机拿不到新的 env，先用 `image.yml` 的 `workflow_dispatch` 推 `:dev`
再测。

## 获取已构建好的版本

```bash
docker pull ghcr.io/yhnocoder/tongtu:latest     # 最新发布
docker pull ghcr.io/yhnocoder/tongtu:0.1.0      # 某个 tag（git tag 即版本）
```

镜像 tag 由仓库的 git tag 决定，打 tag 的步骤见仓库根 [`README.md`](../README.md) 的「发版」一节。

## 运行

论文工作目录不在仓库里。镜像内 `TONGTU_HOME=/work`，把宿主的 `~/.tongtu` 挂到 `/work`
即可，论文落在 `/work/papers/<arxiv_id>`；`build/` 与 `out/` 都在挂载卷上，容器随时可丢、
下次原样重跑即断点续跑。

```bash
# 环境自检
docker run --rm ghcr.io/yhnocoder/tongtu:latest tongtu doctor

docker run --rm \
  -v "$HOME/.tongtu:/work" \
  ghcr.io/yhnocoder/tongtu:latest \
  tongtu run 2401.01234

docker run --rm \
  -v "$HOME/.tongtu:/work" \
  -v "$PWD/paper:/paper:ro" \
  ghcr.io/yhnocoder/tongtu:latest \
  tongtu run /paper

docker run --rm -w /opt/tongtu ghcr.io/yhnocoder/tongtu:latest pytest -m compile

docker run --rm -v "$PWD:/src" -w /src ghcr.io/yhnocoder/tongtu:latest \
  bash -c 'uv sync && uv run pytest -m compile'

docker run --rm -it -v "$HOME/.tongtu:/work" \
  ghcr.io/yhnocoder/tongtu:latest bash
```

### agent 运行时在镜像里

env 层装了 node 22 与 codex、claude-code、pi 三个 CLI，版本见 `Dockerfile` 的
`NODE_VERSION`、`CODEX_VERSION`、`CLAUDE_CODE_VERSION`、`PI_VERSION` 四个 ARG。镜像不设
环境开关。claude-code 与 pi 的参数与 Claude 云端相同，Linux 上 claude-code 不开沙箱；codex
在论文容器里改用 `danger-full-access`，由启动器传入的信号决定，属于 T7，在此之前不要在容器里
用 codex（`remote.html` 第 02、10 节）。

凭据不进镜像，经环境变量传入容器：`CLAUDE_CODE_OAUTH_TOKEN`、`TONGTU_CODEX_AUTH`、
`TONGTU_PI_AUTH`（`remote.html` 第 08 节）。`CLAUDE_CODE_OAUTH_TOKEN` 由 claude 自己读取；
tongtu 启动时发现后两者，就分别写到 `~/.codex/auth.json` 与 `~/.pi/agent/auth.json`，没设的不写。

## 已知取舍

- **使用 Tex 全量包：~6GB**：arXiv 论文的宏包不可预测，为省磁盘引入一类新的编译失败不划算。
- **只有 x86_64**：Mac 上经 Rosetta 运行，编译慢 1.3 到 1.6 倍，每篇多几秒（`remote.html` 第 15 节）。
- **TeX Live 来自 release `texlive-2026`**：`.github/workflows/texlive-release.yml` 从 CTAN 的 `texlive2026.iso`
  （发行冻结版）装 scheme-full 后打包，Claude cloud 环境与镜像用同一份；`Dockerfile` 顶部的
  `ARG TEXLIVE_RELEASE=texlive-2026` 指定 release tag。镜像内 `tlmgr option repository` 指到
  `https://texlive.info/tlnet-archive/2026/05/25/tlnet/`（与 ISO 同版本的日期快照），之后 `tlmgr update` 也不会漂移。
  本机装的是同一个 ISO，不需要对齐；若曾 `tlmgr update` 过：
  ```bash
  tlmgr option repository https://texlive.info/tlnet-archive/2026/05/25/tlnet/ && tlmgr update --all
  ```
