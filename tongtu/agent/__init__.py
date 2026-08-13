"""agent 运行时适配层（架构 §9）——零期 M3 填充，此处仅占位。

两个原语，运行时可插拔（首发 Codex CLI，见架构 §13）：

    complete(prompt, text, model) -> text
        # 无状态判断：逐块翻译、术语决策。可由纯 API 调用或 agent 运行时实现。

    session(prompt, workdir, model, budget) -> {done, transcript_path}
        # 有状态修复：修构建环境、修编译错、documentclass 适配。
        # 要求：headless 拉起、读写 workdir、执行命令、联网、可指定模型。

纪律：
- `session` 的 `done` 只表示会话结束，**裁决权在事后的校验脚本与编译**，永不信 agent 自述。
- 所有会话转录落 `logs/`——既是审计，也是促升规则的数据来源（report.json 的干预统计）。
- MockAgent（M2）：`complete` 恒等返回、`session` no-op——编译层 CI（恒等翻译 e2e）的钥匙。

六个关节：①主文件 ②构建环境 ③环境分类 ④通读与术语 ⑤翻译 ⑥适配与修复。
"""

from __future__ import annotations

#: 六个 agent 关节的稳定标识符，report.json 的干预记录与事件流共用这套命名。
JOINTS: tuple[str, ...] = (
    "main_file",      # ① flatten：主文件歧义 → 判定主文件
    "build_env",      # ② baseline：原文编译失败 → workdir 内修构建环境
    "env_classify",   # ③ mask：未知环境 → 散文/重环境分类
    "survey",         # ④ survey：全文通读 → brief + 术语预扫决策
    "translate",      # ⑤ translate：单块翻译
    "fixup",          # ⑥ compile：documentclass 适配与编译修复
)

__all__ = ["JOINTS"]
