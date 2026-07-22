# 教学案例原始需求提示词

请以 `biomedical-analysis-agent` 的 `run` 模式，在 Windows 上使用 R 4.5.3、task-local renv 1.2.2、Seurat 5.5.0、hdf5r 1.3.12、完整同代 Bioconductor 3.21 closure（BiocVersion 3.21.1、glmGamPoi 1.20.0、SparseArray 1.8.1）与 Posit Public Package Manager `2026-04-23` binary snapshot，复现 Seurat 官方 spatial vignette 对应的 10x Genomics **Mouse Brain Sagittal-Anterior** 单张 Visium 切片分析，并制作 publication-ready spatial QC、cluster 与 `Hpca`/`Ttr` plots，完成 visualization 和 native visual review。SCTransform 必须显式使用 `vst.flavor="v2"` 与 `method="glmGamPoi_offset"`，并验证实际 backend、数值有限性及零 warning。禁止 source compilation、跨 Bioconductor release 混装、hard-exit helper 或版本回退。

输入仅允许从 `input-manifest.json` 声明的 10x 官方地址获取：filtered feature-barcode H5 与 `spatial.tar.gz`。不要下载 raw TIFF，不要把原始二进制数据提交到仓库。仓库 manifest 已冻结真实官方文件的 exact 字节数和 SHA-256；下载前后均不得切换到 null/first-learning freeze，任何不一致必须非零退出。

执行内容：

1. 用 `Load10X_Spatial` 加载数据，并保留 vendor counts、barcodes、array/full-resolution coordinates、histology images 与 scale factors。
2. 对账 H5 matrix、tissue position table 与 Seurat object；另外必须报告 Spatial assay cells、image cells、coordinates 三方计数与六个有向差集，全集不相等即保留证据并停止，不得放宽契约。检查坐标有限性、范围、image assets 和 scale factors。
3. 使用 `SCTransform(assay = "Spatial")`、PCA、expression-neighbor clustering；明确该 cluster 是表达驱动的 spot cluster，不是空间正则化 domain，更不是 cell type。
4. 产出 coordinate-faithful 的 spot/image overlays：QC、cluster、`Hpca`/`Ttr`。同时保存 original 与 final-size PNG。
5. 原生打开 original/final 图逐张复核。视觉参数最多修订三轮；调图不得改变输入、过滤、SCTransform、PCA、neighbors、cluster resolution、feature selection 或任何统计/生物学含义。
6. 生成完整 run tree、checkpoints、manifest、artifact ledger、结果表、`RESULTS.md`、`QA_REPORT.md`、`FIGURE_NOTES.md`、`ARTIFACT_INDEX.md` 与脱敏 `execution-summary.json`。

研究边界：这是单数据集、单切片、spot-level 的描述性教学案例。不得进行或暗示 donor/group inference、群体效应、机制、细胞类型、直接细胞接触、配体受体作用或因果结论；spot 不能称为 cell。
