# 通途 Tongtu

基于 LaTeX 源码的 arXiv 论文英译中引擎。不解析 PDF——直接取 arXiv e-print 源码，掩码后交给模型翻译，机械校验 + 编译回环保证正确性，产出保真原作排版的中文 PDF。

> 命名取自《水调歌头·游泳》「一桥飞架南北，天堑变通途」——语言是天堑，翻译使之为通途。

**状态：按 `docs/proposal/pipeline-v0.html` 重构中，尚不可用。** 项目约定与重构流程见 `CLAUDE.md`。

## 开发

```
uv sync              # 装依赖（含 dev 组）
make install-hooks   # 装 git pre-commit hook（ruff check / ruff format / diction lint）
make lint            # 检查：ruff check + ruff format --check + diction lint
make format          # 自动修：ruff check --fix + ruff format
```
