# 通途参考镜像
#
# 三层构建，各层的变化频率差着数量级：
#   base    基底层：debian + TeX Live scheme-full（release texlive-2026 的 ISO 打包产物）+ 图渲染
#           工具 + uv 与受管 Python。几乎不变，层缓存常年命中。
#   env     环境层：Python 依赖、字体文件、node 22 与三个 agent CLI。随 uv.lock、fonts/ 与
#           CLI 版本变，不含仓库代码。
#   runtime 代码层：仓库本体 + 项目安装 + 构建期自检。每次改代码只重跑这一层。
# 各层之间不 COPY 产物（TeX 树 6GB 级，多阶段裁剪没意义），分层的目的就是缓存边界。
#
# runtime 阶段写成 `FROM ${ENV_IMAGE}`。`ENV_IMAGE` 默认是本文件的 env 阶段；本机 compose 构建时
# 传 `--build-arg ENV_IMAGE=ghcr.io/yhnocoder/tongtu:env`，BuildKit 只构建用得到的阶段，所以
# 本机只构建 runtime 层。改动涉及 base 或 env 层时，本机拿不到新的 env，先用 image.yml 推 :dev
# 再测。
#
# env 层单独成 target 是给 CI 用的：编译层作业以 `ghcr.io/<owner>/<repo>:env` 为 container
# 直接跑测试，代码由 checkout 提供，不必每次搬运 6GB 镜像。构建与推送见
# .github/workflows/image.yml。
#
# 构建上下文是**仓库根**，不是 docker/：
#     docker build -f docker/Dockerfile -t tongtu:dev .
# 运行与三角色的用法见 docker/README.md。
#
# 刻意不用 BuildKit 专属语法（heredoc / COPY --link / RUN --mount）：本机构建只在 compose
# 场景下做；Dockerfile 的改动一律由 image.yml 验证。保守写法优先，任何 builder 都能处理这份
# Dockerfile。

# 全局 ARG 才能在 FROM 里引用。
ARG ENV_IMAGE=env

# TeX Live 来自本仓库 release `texlive-2026`：.github/workflows/texlive-release.yml 从 CTAN 的
# texlive2026.iso（发行冻结版，与 MacTeX / 本机同源，latex revision 76924）装 scheme-full（不含
# doc/src）后打成 zstd 分片，Claude cloud 环境用的也是这一份。不用 texlive/texlive 官方镜像：
# 它跟着 tlnet 滚动，没有日期 tag，宏包版本与本机对不上（#88：同一源码只在容器里编译失败）。
ARG TEXLIVE_RELEASE=texlive-2026

# =========================================================== 基底层（几乎不变）
FROM debian:bookworm-slim AS base

# 全程 bash + pipefail：管道左半边失败不能被右半边的退出码盖住（下面的自检都靠退出码
# 说话，静默失败比构建失败危险得多）。代价是 `xxx | head` 这类提前关管道的写法会被
# SIGPIPE 判成失败——本文件因此只在解压那一处用管道（cat 分片 | tar），两端都会因失败退非零。
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV DEBIAN_FRONTEND=noninteractive

# perl：tlmgr / latexmk / latexpand / epstopdf 都是 perl 脚本
# zstd：解 release 里的 TeX Live 分片
# fontconfig：xelatex 与 xdvipdfmx 链接 libfontconfig
# ghostscript：epstopdf 的后端，EPS/PS 链缺它就断
# poppler-utils / imagemagick：图片格式转换，供修复会话与将来的图处理使用
# git：修复会话在编译树里干活时常要它
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fontconfig \
        ghostscript \
        git \
        imagemagick \
        perl \
        poppler-utils \
        zstd \
    && rm -rf /var/lib/apt/lists/*

# 分片清单就是 SHA256SUMS（texlive-release.yml 这样约定）：先取它，按行下载、校验、拼接解压，
# 得到 /usr/local/texlive/2026。分片单个近 2GB，下载与删除放在同一层，层里只留解压结果。
# 装完把 tlmgr 的仓库指到与 ISO 同版本的 tlnet 日期快照（latex 也是 76924），之后有人
# `tlmgr update` 也拿不到更新的包。
ARG TEXLIVE_RELEASE
ARG TEXLIVE_RELEASE_URL=https://github.com/yhnocoder/tongtu/releases/download/${TEXLIVE_RELEASE}
ARG TEXLIVE_TLNET=https://texlive.info/tlnet-archive/2026/05/25/tlnet/
RUN mkdir -p /tmp/texlive \
    && cd /tmp/texlive \
    && curl -fL --retry 3 -o SHA256SUMS "${TEXLIVE_RELEASE_URL}/SHA256SUMS" \
    && while read -r _ part; do curl -fL --retry 3 -o "${part}" "${TEXLIVE_RELEASE_URL}/${part}"; done < SHA256SUMS \
    && sha256sum -c SHA256SUMS \
    && cat ./*.part* | tar -C /usr/local -I zstd -xf - \
    && cd / \
    && rm -rf /tmp/texlive \
    && /usr/local/texlive/2026/bin/x86_64-linux/tlmgr option repository "${TEXLIVE_TLNET}"

ENV PATH="/usr/local/texlive/2026/bin/x86_64-linux:${PATH}"

# Python 与依赖管理走 uv。解释器也交给 uv 装：镜像内不依赖发行版的 python3
# 版本（texlive 基底跟着 Debian 走，python3 是 3.11 还是 3.13 不该影响我们），
# only-managed 保证 `uv sync` 挑中的就是下面这一版。
ARG UV_VERSION=0.8.17
ARG PYTHON_VERSION=3.12
ENV UV_INSTALL_DIR=/usr/local/bin \
    UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PYTHON=${PYTHON_VERSION} \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/tongtu/.venv

# 安装脚本先落盘再执行（不 `curl | sh`）：一来版本固定的那个 URL 万一挪了位置能当场
# 回退到不带版本号的入口，二来失败点看得清楚是「下载」还是「安装」。
RUN curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" -o /tmp/uv-install.sh \
      || curl -LsSf "https://astral.sh/uv/install.sh" -o /tmp/uv-install.sh \
    && INSTALLER_NO_MODIFY_PATH=1 sh /tmp/uv-install.sh \
    && rm -f /tmp/uv-install.sh \
    && uv --version \
    && uv python install "${PYTHON_VERSION}" \
    && uv python find "${PYTHON_VERSION}"

# =========================================================== 环境层（随 uv.lock、fonts/ 与 node、CLI 版本变）
FROM base AS env

WORKDIR /opt/tongtu

# 依赖先行：只有 pyproject.toml / uv.lock 变了才重装依赖，改流水线代码不重装。
# --frozen = 不改 uv.lock（镜像里锁定的就是仓库锁定的）；dev 组一并装，镜像里能直接
# `pytest -m compile`。
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# 字体随环境层进镜像，tongtu 编译时经 kpathsea 搜索路径找到它们（tongtu/fonts.py）。
# CI 的 papers.yml 以本层为 container，代码与字体都由 checkout 提供，这份字体只对 runtime
# 镜像有意义；runtime 层 `COPY . .` 会再拷一次，分层缓存不受影响。
COPY fonts/ ./fonts/

# node 从 nodejs.org 的发行 tarball 装到 /usr/local，不用发行版仓库：版本由 ARG 固定，校验和按官方 SHASUMS256.txt 核对
ARG NODE_VERSION=22.23.3
RUN mkdir -p /tmp/node \
    && cd /tmp/node \
    && curl -fL --retry 3 -o node.tar.gz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.gz" \
    && curl -fL --retry 3 -o SHASUMS256.txt "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt" \
    && grep " node-v${NODE_VERSION}-linux-x64.tar.gz\$" SHASUMS256.txt > node.sha256 \
    && sed -i 's# node-v.*# node.tar.gz#' node.sha256 \
    && sha256sum -c node.sha256 \
    && tar -xzf node.tar.gz -C /usr/local --strip-components=1 \
         --exclude="node-v${NODE_VERSION}-linux-x64/CHANGELOG.md" \
         --exclude="node-v${NODE_VERSION}-linux-x64/LICENSE" \
         --exclude="node-v${NODE_VERSION}-linux-x64/README.md" \
    && cd / \
    && rm -rf /tmp/node \
    && node --version \
    && npm --version

# 三个 agent CLI 装成全局 npm 包，可执行文件落在 /usr/local/bin：论文容器的 HOME 是 tmpfs（remote.html 第 08 节），装在 HOME 下的会丢
ARG CODEX_VERSION=0.160.0
ARG CLAUDE_CODE_VERSION=2.1.287
ARG PI_VERSION=1.0.0
RUN npm install -g --no-fund --no-audit \
      "@openai/codex@${CODEX_VERSION}" \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
      "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && npm cache clean --force \
    && codex --version \
    && claude --version \
    && pi --version

# 入口 = PATH 上的 `tongtu`（venv 的 bin 前置）。工作目录默认 /work，论文目录落 /work/papers/<arxiv_id>，宿主挂进来即可续跑。
# TONGTU_ISOLATED=1 放在 env 层：papers.yml 以 :env 为 container 时同样处于隔离环境，与云端一致。
ENV PATH="/opt/tongtu/.venv/bin:${PATH}" \
    TONGTU_HOME=/work \
    TONGTU_ISOLATED=1

# =========================================================== 代码层（每次改码重跑）
FROM ${ENV_IMAGE} AS runtime

WORKDIR /opt/tongtu

# 仓库本体。.dockerignore 挡掉 .git / .venv / 各种缓存（尤其 .venv：开发机的 venv 混进
# 镜像会让 uv 面对一个解释器对不上的环境）。
COPY . .
RUN uv sync --frozen

WORKDIR /work

# 构建时自检：xelatex / latexmk / latexpand 三件套 + 打包的字体文件齐全，这个镜像才成立。
# 三个程序的在位判断本来就是 doctor 干的活（tongtu/cli.py 的 TOOLCHAIN_CHECKS），
# 这里不另抄一遍 `xxx --version`：那既重复又容易被某个工具「--version 退非零」判成假失败。
# doctor 的退出码只对工具链与字体那一组负责，密钥与登录缺失不计入（docs/design/pages/remote-cli.html）：
# 这个镜像是要推 GHCR 的可分发产物，构建它的机器不该需要运行期凭证。
RUN tongtu --version \
    && tongtu doctor

LABEL org.opencontainers.image.title="tongtu" \
      org.opencontainers.image.description="通途：基于 LaTeX 源码的 arXiv 论文英译中引擎（TeX Live full 参考环境）" \
      org.opencontainers.image.source="https://github.com/yhnocoder/tongtu"

# 不设 ENTRYPOINT：这个镜像同时是 CI 环境与参考环境，除了 `tongtu run` 还要能直接
# `pytest -m compile`、`bash` 进去排查。CMD 给个无副作用的默认。
CMD ["tongtu", "--help"]
