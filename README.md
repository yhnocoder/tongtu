# 通途 Tongtu

基于 LaTeX 源码的 arXiv 论文英译中引擎。不解析 PDF——直接取 arXiv e-print 源码，三级掩码后交给 coding agent 翻译，机械校验 + 编译回环保证正确性，产出保真原作排版的中文 PDF 与结构化索引（交互地图）。

> 命名取自《水调歌头·游泳》「一桥飞架南北，天堑变通途」——语言是天堑，翻译使之为通途。

**状态：施工中（零期）。** v2 原型（arxiv-translator）迁入中，尚不可用。

## 规划结构

```
scripts/    确定性流水线（fetch / mask / chunk / validate / unmask / inject_cjk / compile / export）
skill/      翻译 playbook（给 coding agent 的操作手册与文风规则）
agent/      agent 运行时适配层（Claude Code / Codex CLI / opencode / 自建循环，可插拔）
docker/     执行环境镜像（TeX Live full + 通途 + agent 运行时）
fallback/   非 arXiv PDF 的降级流水线（doc2x → markdown）
docs/       设计文档
```

## 核心约定

- **agent 负责判断，脚本负责验证**：翻译、修编译错交给 agent；正确性由机械校验（占位符完整性、控制序列比对、段落数）与「编译通过」这一硬指标裁决。
- **论文工作目录不在仓库内**：默认 `~/.local/share/tongtu/<arxiv_id>/`，`$TONGTU_HOME` 或 `--workdir` 覆盖。
- **产物契约**（每篇论文一个产物包）：`zh.tex` / `zh.pdf` / `blocks.json` / `anchors.json` / `figures/*.png` / `glossary.json` / `report.json` / `zh.synctex.gz`。
- **agent 运行时可插拔**：流水线只依赖薄接口（headless 拉起、读写文件、执行命令、联网、可指定模型），不绑定具体产品。
