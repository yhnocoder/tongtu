# 通途 Tongtu

基于 LaTeX 源码的 arXiv 论文英译中引擎。不解析 PDF——直接取 arXiv e-print 源码，掩码后交给模型翻译，机械校验 + 编译回环保证正确性，产出保真原作排版的中文 PDF。

> 命名取自《水调歌头·游泳》「一桥飞架南北，天堑变通途」——语言是天堑，翻译使之为通途。

**状态：v0.2，可运行。** `tongtu run <arxiv_id>` 走完 fetch → reconstruct → precompile → mask → translate → compile 六个阶段，translate 由一次 agent 会话整篇翻译，产出 `papers/<编号加版本号>/out/<编号加版本号>.zh.pdf`；未发布到 PyPI。

从哪里进：

- [设计文档](https://yhnocoder.github.io/tongtu/pages/design.html)：六个阶段、模式配置、命令行各是什么、读写哪些文件（源文件在 `docs/design/`）
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

## 启动器插件

`raycast/` 是一个启动器插件，可以装进 Raycast 或 Tinycast。它有三个命令：「翻译论文」查看一篇论文的状态并在后台启动 `tongtu run`，翻译结束时发系统通知；「论文」列出全部论文的状态，打开译文，重试或停止翻译；「设置」修改 `config.toml`。

1. 安装 `tongtu` 命令。插件直接执行 `tongtu`，依次在 `~/.local/bin`、`~/.cargo/bin`、`/opt/homebrew/bin`、`/Library/TeX/texbin`、`/usr/bin`、`/bin` 里查找。从 git 安装：

   ```
   uv tool install git+https://github.com/yhnocoder/tongtu
   ```

   更新时重新安装：

   ```
   uv tool install --reinstall git+https://github.com/yhnocoder/tongtu
   ```

2. 装进 Tinycast：Settings → Extensions → Install → Install from GitHub，填入：

   ```
   https://github.com/yhnocoder/tongtu/tree/main/raycast
   ```

   这种安装方式需要本机有 Node 和包管理器（例如 npm）。用 nvm 安装 Node 时，Tinycast 0.12 查找的是 `~/.nvm/versions/node/current/bin`，nvm 默认不建这个目录；在同一个面板的 Custom search paths 里填入实际的目录，形如 `~/.nvm/versions/node/v<版本号>/bin`。

   这样安装的插件不会自动更新。更新时重新执行一次安装，同时按第 1 步更新 `tongtu` 命令。

修改插件代码时，在本地构建。在仓库根目录执行下面的命令，它安装依赖并构建，最后打印产物目录 `raycast/dist/` 的绝对路径：

```
make raycast
```

产物在 Tinycast 里用 Settings → Extensions → Install → Add from folder 安装，选 `raycast/dist`。装进 Raycast 时在 `raycast/` 下执行 `npm run dev`。

「翻译论文」打开后停在一个带输入框的界面。输入框里预先填好编号，依次取命令参数、浏览器当前标签页的地址、剪贴板，三处都没有时为空，可以随时修改。下面显示这篇论文的标题、摘要、作者和状态。回车的动作随状态变化：还没有翻译的论文开始翻译并进入详情页，其余状态直接进入详情页。它不会自己开始翻译，也不会自己关闭面板。第一次读取浏览器地址时，系统会询问是否允许启动器控制浏览器，选择允许。

摘要可以显示成中文。在「设置」的「摘要翻译」里填入密钥，或执行 `tongtu config set abstract.api_key <密钥>`，「翻译论文」界面和已完成论文的详情页就显示中文摘要；默认使用 DeepSeek 的 `deepseek-flash`，接口地址与模型可以在同一处修改。中文摘要是一份单独的预览译文，用词可能与中文 PDF 里的摘要不同。第一次显示一篇论文时先显示英文摘要，译文返回后换成中文，之后从缓存读取。没有设置密钥时显示英文摘要。命令行用 `tongtu info <编号> --zh` 取得同一份中文摘要。

「论文」在有论文正在翻译时每 5 秒刷新一次，其余时候每 60 秒一次，⌘R 立即刷新。

## 发版

先开 PR 把 `tongtu/__init__.py` 的 `__version__` 升到新版本（SemVer，版本号只写这一处）并 merge，然后在 main 上打 tag：

```bash
git switch main && git pull
git tag vX.Y.Z
git push origin vX.Y.Z
```

tag 推送后自动完成剩下的事：`image.yml` 构建参考镜像并推 GHCR（获取与运行见 [`docker/README.md`](docker/README.md)），`release.yml` 校验 tag 与 `__version__` 一致并建 GitHub Release，结果在仓库的 Actions 页与 Releases 页查看。版本校验失败时不建 Release，删掉打错的 tag（`git push origin :vX.Y.Z && git tag -d vX.Y.Z`）、改对版本号重来。尚未发布到 PyPI，见 issue #55。
