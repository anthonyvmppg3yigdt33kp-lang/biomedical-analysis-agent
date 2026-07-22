# 原始需求提示词

请使用 `biomedical-analysis-agent` 规划并执行一个可公开复现的单细胞 RNA-seq 教学案例：

- 数据：Seurat 官方 PBMC3K 教程对应的 10x Genomics filtered MEX 数据；
- 后端：R 4.5.3、Seurat 5.5.0，必须使用 task-local、冻结的 `renv` 环境；
- 范围：单个 10x 文库的描述性分析，包括导入、QC、`LogNormalize`、VST 高变基因、PCA、聚类、UMAP、cluster marker、保守教学注释和可视化；
- 质量核对：2,700 个输入细胞、QC 后 2,638 个细胞、9 个 cluster；任何不一致都应使验证失败；
- 图形：同时保存 original 与 final-size 版本，最多进行三轮仅视觉参数调整，每轮都要原生打开两种尺寸并记录 review；
- 交付：需求、路由、冻结计划、分析设计、参数化 R 代码、环境证据、checkpoints、结果表、对象、图形、结果报告、QA、artifact index、manifest 和 append-only ledger；
- 边界：这是单文库描述性教学，不进行 donor-level 推断、差异丰度、群体效应、机制或因果声明；不要加入 CellChat、GSEA 或其他高级分支；
- 数据合规：不把原始数据提交到仓库，只保存官方 URL、许可、文件大小和 SHA-256，并在下载后强制校验。

先以 `plan` 模式输出只读规划；只有收到显式 `--authorize-run` 后才允许在 task-local 目录下载数据、准备环境和执行。
