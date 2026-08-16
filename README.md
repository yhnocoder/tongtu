# 通途 Tongtu

基于 LaTeX 源码的 arXiv 论文英译中引擎。不解析 PDF——直接取 arXiv e-print 源码，三级掩码后交给 coding agent 翻译，机械校验 + 编译回环保证正确性，产出保真原作排版的中文 PDF 与结构化索引（交互地图）。

> 命名取自《水调歌头·游泳》「一桥飞架南北，天堑变通途」——语言是天堑，翻译使之为通途。

**状态：施工中（零期-重构中）。** v2 原型（arxiv-translator）迁入中，尚不可用。

## 开发

```
uv sync              # 装依赖（含 dev 组：ruff / pre-commit）
make install-hooks   # 装 git pre-commit hook（ruff check / ruff format / diction lint）
make lint            # 检查：ruff check + ruff format --check + diction lint
make format          # 自动修：ruff check --fix + ruff format
uv run tongtu --help # CLI 命令面（当前为占位实现，统一退出码 3）
```

当前没有测试套件：先以 `examples/papers/` 三篇模板论文调通 happy path（mock 运行时
恒等翻译，全流水线跑到底），流水线定型后再补测试。
