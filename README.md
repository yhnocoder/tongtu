# 本地公式表格识别试跑

用本地 OCR 模型识别 Transformer、Kimi K2、Kimi k1.5 三篇论文的公式与表格截图，并用通途的校验判定是否通过。截图导出与校验来自通途 main（cd50322），由 uv 自动安装。

需要：uv、TeX Live（xelatex、pdflatex）。

    git clone -b claude/project-thread-s54srr https://github.com/yhnocoder/tongtu ocr-trial
    cd ocr-trial
    ./run.sh                    # 默认 MinerU2.5-Pro 与 TeleOCR
    ./run.sh mineru             # 指定模型：paddle mineru glm minicpm ovis tele
    ONLY=formula ./run.sh       # 只跑公式

对已启动的 OpenAI 兼容服务发请求，写成「结果名:模型族」，模型族是 mineru、tele、paddle、glm、minicpm 之一：

    API_BASE=http://127.0.0.1:8000/v1 API_MODEL=TeleOCR-oQ8 ./run.sh tele_mlx:tele

默认同时发 4 个请求，用 `CONCURRENCY=8` 修改；服务端要能并行处理，例如 llama-server 加 `-np 4`（与并发数一致）。llama-server 还要加 `--special`，否则表格的结构记号会被去掉。

结果汇总在 `work/summary.tsv`，每个截图的输出与判定在 `work/<论文>/verdict_<模型>.json`。已跑过的截图会跳过。
