# 通途参考镜像
#
# 三层构建，各层的变化频率差着数量级：
#   base    基底层：TeX Live full + 图渲染工具 + uv 与受管 Python。几乎不变，层缓存常年命中。
#   env     环境层：Python 依赖 + 字体注册。随 uv.lock 与 fonts/ 变，不含仓库代码。
#   runtime 代码层：仓库本体 + 项目安装 + 构建期自检。每次改代码只重跑这一层。
# 各层之间不 COPY 产物（TeX 树 6GB 级，多阶段裁剪没意义），分层的目的就是缓存边界。
#
# env 层单独成 target 是给 CI 用的：编译层作业以 `ghcr.io/<owner>/<repo>:env` 为 container
# 直接跑测试，代码由 checkout 提供，不必每次搬运 6GB 镜像。构建与推送见
# .github/workflows/image.yml。
#
# 构建上下文是**仓库根**，不是 docker/：
#     docker build -f docker/Dockerfile -t tongtu:dev .
# 运行与三角色的用法见 docker/README.md。
#
# 刻意不用 BuildKit 专属语法（heredoc / COPY --link / RUN --mount）：本机没有 docker，
# 首次构建验证在 CI 上做，保守写法优先——任何 builder 都能吃下这份 Dockerfile。

ARG TEXLIVE_TAG=latest

# =========================================================== 基底层（几乎不变）
# 起点选官方 texlive/texlive：已含 TeX Live **full**、
# latexmk、latexpand、epstopdf 与 perl，一次 pull 就是一个稳定的基础层。
# 曾考虑 debian + `apt install texlive-full`：装的是 Debian 冻结的 TL 版本（比 arXiv 论文
# 常用的宏包旧一年以上，凭空多出一类编译失败），且 apt 装 full 比拉现成镜像层慢得多。
# 要锁死 TeX 版本时把 TEXLIVE_TAG 换成 TL 快照 tag（如 TL2025-historic），不必改别处。
FROM texlive/texlive:${TEXLIVE_TAG} AS base

# 全程 bash + pipefail：管道左半边失败不能被右半边的退出码盖住（下面的自检都靠退出码
# 说话，静默失败比构建失败危险得多）。代价是 `xxx | head` 这类提前关管道的写法会被
# SIGPIPE 判成失败——本文件因此一处管道也不留，改用临时文件。
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV DEBIAN_FRONTEND=noninteractive

# fontconfig  —— `tongtu doctor` 的字体探测（fc-list）与 xelatex 按字体名取字体都靠它
# fonts-noto-cjk —— 探测链第二档（Hiragino 只有 macOS 有；第三档霞鹜文楷在代码层注册）
# ghostscript  —— epstopdf 的后端，EPS/PS 链缺它就断
# poppler-utils / imagemagick —— 图片格式转换，供修复会话与将来的图处理使用
# git          —— 修复会话在编译树里干活时常要它
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fontconfig \
        fonts-noto-cjk \
        ghostscript \
        git \
        imagemagick \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

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

# =========================================================== 环境层（随 uv.lock 与 fonts/ 变）
FROM base AS env

WORKDIR /opt/tongtu

# 依赖先行：只有 pyproject.toml / uv.lock 变了才重装依赖，改流水线代码不重装。
# --frozen = 不改 uv.lock（镜像里锁定的就是仓库锁定的）；dev 组一并装，镜像里能直接
# `pytest -m compile`。
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# 字体随环境层进镜像而不是留给代码层：字体文件是几乎不变的二进制，与依赖同属「环境」。
# CI 的编译层以本层为 container 跑测试，代码由 checkout 提供、`/opt/tongtu` 下只有环境，
# 因此 fontconfig 指向的这份必须在本层就位。
COPY fonts/ ./fonts/

# 字体注册进 fontconfig，使探测链第三档（LXGW WenKai）在镜像内真实可命中。编译时 xeCJK
# 用的仍是 `Path={fonts/}` 相对路径那条链（fonts/README.md），两者互不干扰。
# 末尾的 fc-list 断言让「注册失败」在构建时就炸，而不是等到某篇论文排出一页豆腐。
# 断言走临时文件而不是 `fc-list | grep -q`：SHELL 开了 pipefail，grep -q 命中即退出会
# 给左边发 SIGPIPE，好好的自检会变成假失败。
RUN printf '%s\n' \
      '<?xml version="1.0"?>' \
      '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">' \
      '<fontconfig>' \
      '  <dir>/opt/tongtu/fonts</dir>' \
      '</fontconfig>' > /etc/fonts/conf.d/99-tongtu-fonts.conf \
    && fc-cache -f \
    && fc-list > /tmp/fc-list.txt \
    && grep -qi 'lxgw wenkai' /tmp/fc-list.txt \
    && rm -f /tmp/fc-list.txt

# 入口 = PATH 上的 `tongtu`（venv 的 bin 前置）。工作目录默认 /work，论文目录落 /work/<arxiv_id>，宿主挂进来即可续跑。
ENV PATH="/opt/tongtu/.venv/bin:${PATH}" \
    TONGTU_HOME=/work

# =========================================================== 代码层（每次改码重跑）
FROM env AS runtime

WORKDIR /opt/tongtu

# 仓库本体。.dockerignore 挡掉 .git / .venv / 各种缓存（尤其 .venv：开发机的 venv 混进
# 镜像会让 uv 面对一个解释器对不上的环境）。
COPY . .
RUN uv sync --frozen

WORKDIR /work

# 构建时自检：xelatex / latexmk / latexpand 三件套 + 中文字体探测链全绿，这个镜像才成立。
# 三个程序的在位判断本来就是 doctor 干的活（tongtu/cli.py 的 DOCTOR_TOOLCHAIN_CHECKS），
# 这里不另抄一遍 `xxx --version`——那既重复又容易被某个工具「--version 退非零」坑成假失败。
# doctor 的退出码只对工具链与字体那一组负责，OpenCode 密钥缺失不计入（docs/CLI.md）：这个
# 镜像是要推 GHCR 的可分发产物，构建它的机器不该需要运行期凭证。
RUN tongtu --version \
    && tongtu doctor

LABEL org.opencontainers.image.title="tongtu" \
      org.opencontainers.image.description="通途：基于 LaTeX 源码的 arXiv 论文英译中引擎（TeX Live full 参考环境）" \
      org.opencontainers.image.source="https://github.com/yhnocoder/tongtu"

# 不设 ENTRYPOINT：这个镜像同时是 CI 环境与参考环境，除了 `tongtu run` 还要能直接
# `pytest -m compile`、`bash` 进去排查。CMD 给个无副作用的默认。
CMD ["tongtu", "--help"]
