# 通途 Tongtu

基于 LaTeX 源码的 arXiv 论文英译中引擎。不解析 PDF——直接取 arXiv e-print 源码，掩码后交给模型翻译，机械校验 + 编译回环保证正确性，产出保真原作排版的中文 PDF。

> 命名取自《水调歌头·游泳》「一桥飞架南北，天堑变通途」——语言是天堑，翻译使之为通途。

**状态：按 `docs/proposal/pipeline-v0.html` 重构中，尚不可用。** 项目约定与重构流程见 `CLAUDE.md`。

## 开发

```
uv sync              # 装依赖（含 dev 组）
make install-hooks   # 装 git pre-commit hook（ruff check / ruff format / diction lint / comment lint）
make lint            # 检查：ruff check + ruff format --check + diction lint + comment lint
make format          # 自动修：ruff check --fix + ruff format
```

## 发版

版本号按 SemVer（`X.Y.Z`），单一来源是 `tongtu/__init__.py` 的 `__version__`；
`pyproject.toml` 声明 `dynamic = ["version"]`，由 hatchling 从该文件读取。
升级版本只改这一处，不在别处重复写版本号。

发一版的步骤：

1. 开一个只改版本号的 PR，把 `tongtu/__init__.py` 的 `__version__` 改成新版本，
   commit message 写 `chore(release): 版本号升到 X.Y.Z`。
2. 由用户 merge 进 main。
3. 在 main 上打 tag 并推送：

```bash
git switch main && git pull
git tag vX.Y.Z
git push origin vX.Y.Z
```

tag 推送后由两个工作流各自完成剩下的事：

- `.github/workflows/image.yml` 构建参考镜像并推 GHCR，runtime 镜像打上
  `:X.Y.Z`、`:X.Y`、`:latest` 三个 tag，env 镜像推 `:env`。镜像的获取与运行方式见
  [`docker/README.md`](docker/README.md)。
- `.github/workflows/release.yml` 先校验 tag 去掉前缀 `v` 后与 `__version__` 相等，
  再用 `gh release create --generate-notes` 建 GitHub Release，标题即 tag 名。

两个工作流的运行状态在仓库的 Actions 页查看，建好的 Release 在仓库的 Releases 页查看。

版本校验失败时该作业失败、不建 Release。此时删掉打错的 tag，改对版本号后重新走一遍上面的步骤：

```bash
git push origin :vX.Y.Z   # 删远端 tag
git tag -d vX.Y.Z         # 删本地 tag
```

`image.yml` 没有版本一致性校验，与 `release.yml` 在同一个 tag 上独立触发，因此校验失败时
错误版本的镜像已经推上 GHCR。重打正确 tag 后 `:latest` 与 `:X.Y` 会被新构建覆盖，
错误版本号的 `:X.Y.Z` 会留在 GHCR，需要到该仓库的 GHCR package 页面手动删除。

尚未发布到 PyPI，相关决定与前置事项记在 issue #55。
