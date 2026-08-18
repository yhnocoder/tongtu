# docker/

通途的**参考镜像**：TeX Live full + 图渲染工具 + 通途本体。构建定义在
[`Dockerfile`](Dockerfile)，构建与发布流水线在
[`.github/workflows/image.yml`](../.github/workflows/image.yml)。

`Dockerfile` 分三层，其中两层作为 target 对外：`env` 是环境（TeX Live full + Python 依赖 +
字体，不含仓库代码），CI 的编译层作业以它为 container 跑测试；`runtime` 是完整镜像，即下表
三个角色里说的那个。分层的理由是耗时：编译层作业若在作业内构建镜像，即便 buildx 缓存全部
命中，也仍要下载 6GB 级的缓存层并把镜像导出到本地 docker daemon，实测占去作业八成时间。
两个 target 各自何时构建、何时推送，见 [`image.yml`](../.github/workflows/image.yml) 的头部注释。

## 三个角色（架构 §10）

镜像存在是为了三件事：

| 角色 | 谁在用 | 说明 |
|---|---|---|
| 云部署单元 | wenshu（一期） | Cloudflare Containers 只认镜像；按 git tag 引用某一版 |
| CI 环境 | 本仓库的编译层 job | TeX 发行版差异是真实的坑，CI 一律在镜像里跑 |
| **参考环境** | 所有人 | **「编译通过」的权威裁决环境**——见下 |

> **编译通过的权威裁决以镜像内复现为准。**
> 本机编译过了、镜像里过不了，算不通过（八成是本机多装了什么宏包）；本机编译不过、镜像里
> 过得了，是本机环境问题，不进 issue 的「翻译 bug」类目。bug 报告与产物合规请附上镜像
> tag 与镜像内的复现命令。

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

## 获取已构建好的版本

```bash
docker pull ghcr.io/yhnocoder/tongtu:latest     # 最新发布
docker pull ghcr.io/yhnocoder/tongtu:0.1.0      # 某个 tag（git tag 即版本）
```

## 运行

论文工作目录不在仓库里（架构 §5）。镜像内 `TONGTU_HOME=/work`，把宿主的目录挂到 `/work`
即可——`build/` 与 `out/` 都落在挂载卷上，容器随时可丢、下次原样重跑即断点续跑。

```bash
# 环境自检
docker run --rm ghcr.io/yhnocoder/tongtu:latest tongtu doctor

docker run --rm \
  -v "$HOME/.local/share/tongtu:/work" \
  ghcr.io/yhnocoder/tongtu:latest \
  tongtu run 2401.01234 --json

docker run --rm \
  -v "$HOME/.local/share/tongtu:/work" \
  -v "$PWD/paper:/paper:ro" \
  ghcr.io/yhnocoder/tongtu:latest \
  tongtu run /paper

docker run --rm -w /opt/tongtu ghcr.io/yhnocoder/tongtu:latest pytest -m compile

docker run --rm -v "$PWD:/src" -w /src ghcr.io/yhnocoder/tongtu:latest \
  bash -c 'uv sync && uv run pytest -m compile'

docker run --rm -it -v "$HOME/.local/share/tongtu:/work" \
  ghcr.io/yhnocoder/tongtu:latest bash
```

### agent 运行时不在镜像里

镜像只管确定性的那一半（TeX + 通途）。agent 运行时（Codex CLI 等）是每次运行的选择，
凭据也各不相同，故不打进镜像：真跑时把运行时装进容器或换用带运行时的派生镜像，并把
API key 以环境变量注入（`-e OPENAI_API_KEY=...`）。

## 已知取舍

- **~6GB 不裁**：arXiv 论文的宏包不可预测，为省磁盘引入一类新的编译失败不划算（继承 v2
  结论，架构 §13）。
- **`latest` 基底**：`TEXLIVE_TAG` 默认 `latest`，图的是宏包新；要可复现到某年某月就锁
  TL 快照 tag 重新发一版。
