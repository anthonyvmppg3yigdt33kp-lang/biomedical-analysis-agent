# Analysis Design：Visium Mouse Brain Sagittal-Anterior

## 1. 研究问题与证据角色

问题：在一张 10x Visium mouse-brain sagittal-anterior 切片中，expression-derived spot clusters 及 `Hpca`/`Ttr` 的 SCTransform-normalized expression 如何分布于 histology image 上？

证据角色为 descriptive overview 与 pipeline validation，不是 cohort inference。可见的空间定位用于核对组织结构与坐标忠实性，但不把 co-location 解释为 cell contact、interaction、mechanism 或 causality。

## 2. 单位与设计

| 单位 | 定义 |
|---|---|
| assay unit | Visium spot（capture mixture） |
| spatial unit | 具有 vendor pixel/array coordinate 的 spot |
| sampling unit | 一张 sagittal-anterior section |
| inference unit | 不适用；单切片描述性分析 |

一个 spot 可混合多个 cell。spot cluster 不得命名为 cell type。该数据没有 donor/group replication，因此不进行差异丰度、群体效应、组间检验、donor-level uncertainty 或因果推断。

## 3. 输入、许可与不可变性

仅使用 `input-manifest.json` 中的 filtered H5 与 spatial archive。仓库已经冻结真实官方输入的 exact size/SHA-256；下载器在接收文件和写入 `resolved-inputs.json` 前逐字节核对，不允许 null 或 first-learning freeze。原始输入位于 run tree 外的 task-local input cache，fresh/resume 直接只读复用而不复制；pipeline 不改写 vendor files，并在每次复用后记录逐文件 hash 与 canonical cache 未修改证据。

数据按 CC BY 4.0 署名 10x Genomics。仓库 MIT 仅覆盖原创代码与文档。

## 4. 环境

- Windows R 4.5.3（exact；非 Windows 明确阻塞）
- task-local `renv` 1.2.2（exact），同时用于 bootstrap 与 project library；从固定 binary snapshot 下载并在安装前核对 index repository、basename 与 committed size/SHA-256（该 PPM index 行当前没有 MD5sum）；不依赖 host/global `renv`
- Seurat 5.5.0（exact）
- exact `hdf5r` 1.3.12、`SeuratObject 5.4.0`、`sctransform 0.4.3`
- 完整 Bioconductor 3.21 同代 closure：`BiocVersion 3.21.1`、`glmGamPoi 1.20.0`、`SparseArray 1.8.1`
- Posit Public Package Manager CRAN snapshot `2026-04-23` 与 `config/bioconductor-3.21-archive-pins.json`；compiled packages 均为 R 4.5 Windows binary

analysis script 没有 installer。只有在 `case_driver.py run|resume --authorized` 后，`prepare_environment.R` 才能在指定 cache root 中 provision/verify/freeze。CRAN bootstrap 先核对 exact renv 1.2.2、Seurat 5.5.0 与 hdf5r 1.3.12；随后对 47-package Bioconductor 3.21 closure 的每个 archive 核对 URL、basename、size、SHA-256 和 DESCRIPTION，再按 frozen order 安装。仅 `GenomeInfoDbData 1.2.14` 可作为 `NeedsCompilation=no` 的平台无关 data archive；Bioc 3.22/3.23、跨 release 混装、source compilation 与 version fallback 均阻塞。task-local renv 1.2.2 生成包含 CRAN/Bioconductor repositories 的 `renv.lock`，随后对真实 filtered H5 执行 `Read10X_h5` smoke。Python 以 `GetNativeSystemInfo` 证明 native X64，仅在每个 R child 缺失时恢复 `PROCESSOR_ARCHITECTURE=AMD64`；冲突或未知状态阻塞，父环境不修改。环境与 pipeline 均须单次 native exit 0，并通过独立 stdout/stderr 禁止模式扫描和 hash-bound completion marker；不使用 helper 或 retry。

## 5. 阶段与检查点

1. **S00_INTAKE**：冻结 request、URLs、许可、输入 hashes、analysis/visual configs。
2. **S10_INGEST**：用 `Read10X_h5` 读取全 filtered matrix，用 `Load10X_Spatial(filter.matrix = TRUE)` 构建 Seurat object。
3. **S20_COORD_IMAGE_QC**：读取 tissue positions；核对 matrix、in-tissue table 与 Seurat object，并强制 Spatial assay cells、image cells、coordinate barcodes 全集相等（六个有向差集均为 0）；检查坐标为有限数值、无重复、scale factors 和 low/high-resolution image 存在。Seurat 5.5.0 返回的 full-resolution pixel coordinates 必须用 vendor `lowres` scale factor 映射到当前加载的 low-resolution image 后再核对边界，并在 QC JSON 中同时保留变换前后 bounds。任何差集或变换后越界都会在 staging 中保留完整表并阻塞，不允许自动删交集或放宽契约。
4. **S30_UNIT_QC**：记录 `nCount_Spatial`、`nFeature_Spatial` 和 attrition。不再施加示例阈值。
5. **S40_PREPROCESS**：显式执行 `SCTransform(assay = "Spatial", vst.flavor = "v2", method = "glmGamPoi_offset")` 后运行 PCA。SCTModel 必须实际记录 `v2`、`glmGamPoi_offset` 与 `glmGamPoi_check=true`；3,000 variable features、spot 数不变、SCT 三层及 PCA embeddings/loadings/stdev 全有限。版本、loaded path、code/config/environment/pins hashes 均写入 evidence。
6. **S60_CORE_DISCOVERY**：在 PCA space 建 kNN 并聚类。该 graph 不是 spatial geometry graph，该聚类不是 spatially regularized domain。
7. **S80_ADVANCED**：仅执行 histology overlay；不执行 SVG、deconvolution、mapping、neighborhood 或 communication。
8. **S90_INFERENCE_QA**：机器检查报告中不得出现 population/mechanism/causality claims。
9. **S95_VISUALIZE_INTERPRET**：保存 original/final PNG、hash-bound review template、reports、ledger 与 summary。

每个 executed stage 先写 `_staging/<stage>`，验证后再 promote；checkpoint 绑定 input/config/code/environment hashes。resume 仅接受完全匹配的 checkpoint。

## 6. 科学参数

`config/analysis-params.json` 是科学基线：seed、SCTransform `vst.flavor`/`method`/variable-feature count、PCA dimensions、k、clustering algorithm/resolution 和 plotted features。它们不能在视觉调优中改变。

本案例加载 Space Ranger 标记的 in-tissue spots，不再加入 post-load QC filter。若实际数据/API 要求更改科学参数，必须产生新 baseline，而不是在 visual round 中静默修正。

## 7. 可视化与复核

三个证据图：

1. `spatial_qc`：`nCount_Spatial` 与 `nFeature_Spatial`；
2. `spatial_clusters`：expression-derived spot clusters；
3. `spatial_features_hpca_ttr`：`Hpca` 与 `Ttr` 的 SCT data overlay。

每图同时导出 original 与 final physical size，保持 coordinate aspect、crop、image scale 和 coordinate/image transform。R pipeline 登记 hashes 后停止在 `awaiting_native_review`；reviewer 必须真实打开两种尺寸。

最多三轮。纯视觉调整只允许 point size、opacity、label size/repel、font size 和 export dimensions。feature scale/cutoff、crop、image scale、过滤、归一化、PCA、cluster 或 feature 变更不是纯视觉调整。

## 8. 支持与禁止的结论

支持：

- 本次执行中 H5、coordinates、object 与 image barcodes 是否完成对账；
- 本切片中 spot QC metric、expression cluster、`Hpca`/`Ttr` normalized expression 的可视空间分布；
- pipeline、checkpoint、hash、report 和 export 是否满足契约。

禁止：

- spot cluster 是 cell type；
- spot 数等于独立样本数；
- 任意 group/donor/population effect；
- gene expression overlay 证明 cell-cell interaction、directionality、mechanism 或 causality；
- 单切片 pattern 可普遍推广到 mouse brain population。
