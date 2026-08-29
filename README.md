# 通途 Tongtu

基于 LaTeX 源码的 arXiv 论文英译中引擎。不解析 PDF——直接取 arXiv e-print 源码，掩码后交给模型翻译，机械校验 + 编译回环保证正确性，产出保真原作排版的中文 PDF。

> 命名取自《水调歌头·游泳》「一桥飞架南北，天堑变通途」——语言是天堑，翻译使之为通途。

**状态：v0.1，可运行。** `tongtu run <arxiv_id>` 走完 fetch → precompile → mask → survey → translate → review → compile 七个阶段，在验证集的论文上出了中文 PDF；未发布到 PyPI。v0.2 在设计中。

从哪里进：

- 设计文档 https://yhnocoder.github.io/tongtu/ ——七个阶段、模型调用层、命令行各是什么、读写哪些文件（源文件在 `docs/design/`）
- [`CLAUDE.md`](CLAUDE.md)——项目的目标、判断与协作约定
- [Milestone](https://github.com/yhnocoder/tongtu/milestone/2)—— v0.2 的进度

## 开发

```
uv sync              # 装依赖（含 dev 组）
make install-hooks   # 装 git pre-commit hook（ruff check / ruff format / comment lint）
make lint            # 检查：ruff check + ruff format --check + comment lint
make format          # 自动修：ruff check --fix + ruff format
```

## 发版

先开 PR 把 `tongtu/__init__.py` 的 `__version__` 升到新版本（SemVer，版本号只写这一处）并 merge，然后在 main 上打 tag：

```bash
git switch main && git pull
git tag vX.Y.Z
git push origin vX.Y.Z
```

tag 推送后自动完成剩下的事：`image.yml` 构建参考镜像并推 GHCR（获取与运行见 [`docker/README.md`](docker/README.md)），`release.yml` 校验 tag 与 `__version__` 一致并建 GitHub Release，结果在仓库的 Actions 页与 Releases 页查看。版本校验失败时不建 Release，删掉打错的 tag（`git push origin :vX.Y.Z && git tag -d vX.Y.Z`）、改对版本号重来。尚未发布到 PyPI，见 issue #55。
