# 通途 Tongtu

基于 LaTeX 源码的 arXiv 论文英译中引擎。不解析 PDF——直接取 arXiv e-print 源码，掩码后交给模型翻译，机械校验 + 编译回环保证正确性，产出保真原作排版的中文 PDF。

> 命名取自《水调歌头·游泳》「一桥飞架南北，天堑变通途」——语言是天堑，翻译使之为通途。

**状态：v0.2，可运行。** `tongtu run <arxiv_id>` 走完 fetch → precompile → mask → translate → compile 五个阶段，translate 由一次 agent 会话整篇翻译，产出 `papers/<编号加版本号>/out/<编号加版本号>.zh.pdf`；未发布到 PyPI。

从哪里进：

- [设计文档](https://yhnocoder.github.io/tongtu/pages/design.html)：五个阶段、模式配置、命令行各是什么、读写哪些文件（源文件在 `docs/design/`）
- [`CLAUDE.md`](CLAUDE.md)：项目的目标、判断与协作约定
- [Issues](https://github.com/yhnocoder/tongtu/issues)：后续方向

## 安装

```sh
uv tool install git+https://github.com/yhnocoder/tongtu
tongtu doctor
```

默认安装 main 分支，地址后加 `@分支名`、`@tag` 或 `@提交号` 安装其他版本。升级用 `uv tool upgrade tongtu`。`~/.local/bin` 不在 `PATH` 里时运行一次 `uv tool update-shell`。

`tongtu doctor` 检查下面的依赖，缺什么就在那一行给出安装命令：

- TeX Live 2026 的完整安装（xelatex、latexmk、latexpand）。macOS 用 `brew install --cask mactex-no-gui`；Linux 用 [install-tl](https://tug.org/texlive/quickinstall.html) 装 scheme-full。
- claude-code 及其登录，由 `agentrun doctor` 检查。
- Linux 上的 bubblewrap 与 socat，同样由 `agentrun doctor` 检查。
- agentrun 不需要手动安装：`PATH` 与 `~/.local/bin` 里都没有时，通途从它的 GitHub release 下载固定版本到 `~/.local/bin/agentrun`。

## 开发

```
uv sync              # 装依赖（含 dev 组）
make install-hooks   # 装 git pre-commit hook（ruff check / ruff format / comment lint）
make lint            # 检查：ruff check + ruff format --check + comment lint
make format          # 自动修：ruff check --fix + ruff format
```

agent 会话经 [agentrun](https://github.com/yhnocoder/agentrun) 启动，安装方式见上一节。`uv run tongtu doctor` 对配置里用到的运行时运行 `agentrun doctor`。

## 发版

先开 PR 把 `tongtu/__init__.py` 的 `__version__` 升到新版本（SemVer，版本号只写这一处）并 merge，然后在 main 上打 tag：

```bash
git switch main && git pull
git tag vX.Y.Z
git push origin vX.Y.Z
```

tag 推送后自动完成剩下的事：`image.yml` 构建参考镜像并推 GHCR（获取与运行见 [`docker/README.md`](docker/README.md)），`release.yml` 校验 tag 与 `__version__` 一致并建 GitHub Release，结果在仓库的 Actions 页与 Releases 页查看。版本校验失败时不建 Release，删掉打错的 tag（`git push origin :vX.Y.Z && git tag -d vX.Y.Z`）、改对版本号重来。尚未发布到 PyPI，见 issue #55。
