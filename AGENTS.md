# 通途（tongtu）仓库 Context

> 若存在 `../AGENTS.md`（内部开发工作区 wenshu-ws 的统一 context），先读它获取项目全景；本文件对外部贡献者自足。

- 本仓库是基于 LaTeX 源码的 arXiv 论文英译中引擎：确定性流水线脚本 + coding agent 翻译 + 机械校验 + 编译回环。规划结构与产物契约见 [README.md](README.md)。
- **总原则：agent 负责判断，脚本负责验证。** 翻译、修编译错交给 agent；正确性只认校验脚本全绿与 PDF 编译通过，agent 的「我检查过了」无效力。
- **论文工作目录不在仓库内**：默认 `~/.local/share/tongtu/<arxiv_id>/`（`$TONGTU_HOME` / `--workdir` 覆盖）。测试与试验不要在仓库里创建论文目录。
- agent 运行时可插拔：流水线只依赖薄接口（headless 拉起、读写文件、执行命令、联网、可指定模型），适配层在 `tongtu/agent/`，不绑定具体产品。
- 状态：零期施工中，v2 原型（arxiv-translator）迁入中；设计文档随迁移落地到 `docs/`。
