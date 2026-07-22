# PBMC3K 分析设计

## 研究问题与范围

本案例复现 Seurat 官方 PBMC3K 入门流程，目标是展示一个从已校验公共输入到可审计图表与报告的单细胞教学运行。数据来自一个 10x Genomics PBMC 文库，因此分析单位仅为用于描述性可视化的 cell；没有独立 donor、condition 或重复样本，不能进行群体层面的效应估计。

分析明确排除 donor-level pseudobulk、差异丰度、批次整合、轨迹、CNV、cell communication、CellChat、GSEA、预测建模、机制或因果推断。cluster marker 仅用于描述性注释，不是条件差异表达证据。

## 输入与身份核验

- 来源：10x Genomics PBMC 3k filtered gene/barcode matrices。
- 入口：filtered MEX，reference folder 为 `filtered_gene_bc_matrices/hg19`。
- 归档文件必须恰好为 7,621,991 bytes，SHA-256 必须为 `847d6ebd9a1ec9a768f2be7e40ca42cbfe75ebeb6d76a4c24167041699dc28b5`。
- 解压必须经过路径穿越检查；归档和解压输入放在 run tree 外的 task-local cache，不提交到 Git。
- MEX 初始矩阵预期为 32,738 features × 2,700 barcodes。`CreateSeuratObject(min.cells=3, min.features=200)` 后预期 13,714 features × 2,700 cells。

任何 checksum、文件大小、成员结构、维度或 barcode 唯一性不符都阻断运行。

## 环境与可复现性

运行环境固定为 R 4.5.3、Seurat 5.5.0，并由 task-local `renv` 冻结。环境准备器从固定 snapshot 下载并校验 `renv 1.2.2` binary，将其安装到 run-root 内独立 bootstrap library；不要求或修改 host renv。分析脚本不含安装命令；预检会核对精确 R/Seurat 版本，绝不静默使用 Seurat 5.4.0。改变依赖、分析参数或输入哈希必须产生新的 plan 和 run。

Windows 上每个 R 子进程都用 `GetNativeSystemInfo` 独立确认真实 OS 架构；本版只接受 X64，并把缺失的 `PROCESSOR_ARCHITECTURE` 仅恢复到子进程副本中的 `AMD64`。未知/冲突架构直接阻断，父环境保持不变。环境准备与分析必须依靠 R 自然退出 0，同时保存绑定 stdout/stderr 的结构化进程证据；禁止外部终止 helper，也不允许以完成 marker 或部分产物替代真实返回码。`warning:`、`stack imbalance`、iteration/alternation limit、`execution halted`、R error、access violation、segfault 和 fatal error 任一命中都阻断运行。

分析 R 入口固定 `options(warn=1)`，把单个 warning 与 `Warning messages:` 聚合输出都即时送入 stderr；Python 同时扫描 `warning:` 与 `warning message`，不依赖 R 默认 `warn=0` 的会话结束行为。effective value 写入 UMAP runtime/process evidence。

随机性由显式 seeds 控制：通用 `random_seed=42`、`FindClusters(random.seed=0)`、`RunUMAP(seed.use=42)`。参数在 `params.json` 中冻结。

## 分析 DAG 与 checkpoints

1. `SC01_IMPORT_AND_IDENTITY`：读取 MEX、核对维度与 barcode；在创建 Seurat 对象前显式执行 `_` → `-` feature-name 规范化，输出完整 mapping，并断言转换后无新重复、矩阵维度和 count values 完全不变；随后创建对象并保留 raw counts。
2. `SC04_QC_PER_CAPTURE`：计算 `percent.mt`；应用官方教程的严格边界 `nFeature_RNA > 200`、`nFeature_RNA < 2500`、`percent.mt < 5`；记录排除原因。
3. `SC06_NORMALIZE_AND_HVG_PER_SAMPLE`：`LogNormalize(scale.factor=10000)`，VST 选择 2,000 HVGs，缩放所有 retained features。
4. `SC08_GRAPH_CLUSTER_AND_EMBED`：PCA；前 10 PCs 构图；Louvain algorithm 1、resolution 0.5；UMAP 使用前 10 PCs，并显式冻结 `umap.method="uwot"`、`metric="cosine"` 与 seed。Seurat 5.5 的官方 `Seurat.warn.umap.uwot=FALSE` transition option 只在该调用的最小作用域关闭一次性迁移提示，随后恢复并写入 runtime contract；不使用 `suppressWarnings()`、handler muffling 或 warning allowlist，不改变算法。
5. `SC09_ANNOTATE_AND_REVIEW`：描述性 `FindAllMarkers`；按 Seurat 教学 cluster 顺序给出保守 reference labels，并输出支持/矛盾 marker 证据。标签明确标记为教学参考，不是盲法临床分类。
6. `SC13_FIGURES_AND_INTERPRETATION`：导出 QC、PCA、cluster UMAP、annotation UMAP 和 marker dot plot 的 original/final-size 版本与待原生复核记录。

每个 stage 先写入唯一 `_staging` 目录，再原子提升到 `04_intermediate/<stage_id>`。完成标记绑定 input SHA、参数 SHA 和环境版本。相同签名的 checkpoint 可复用；不匹配的既有 checkpoint 阻断运行，不能就地覆盖。

## QC 与预期值

下列三项是 canonical teaching-case gates，而不是可调目标：

| Gate | 预期 |
|---|---:|
| filtered MEX input cells | 2,700 |
| QC retained cells | 2,638 |
| final clusters | 9 |

此外核对：raw counts 为非负整数样数、barcode 唯一、PCA/UMAP 行数等于 retained cells、所有 cells 均有 cluster 与 teaching label、marker 表含调整后 P 值列。任何 gate 不符返回非零状态；不能为“对齐答案”而改变 QC、归一化、PC 数、resolution 或 seed。

## 注释策略与声明边界

九个教学标签沿用官方 PBMC3K vignette 的 cluster 顺序：`Naive CD4 T`、`CD14+ Mono`、`Memory CD4 T`、`B`、`CD8 T`、`FCGR3A+ Mono`、`NK`、`DC`、`Platelet`。每个标签同时记录正向 marker 和应当低表达的矛盾 marker；输出 cluster top markers 供人工复核。

如果 cluster 数不是 9，或 expected marker 证据明显不一致，注释 stage 必须失败或保留 `Unresolved`，不得通过重排 cluster 或修改 resolution 隐藏差异。教学标签不代表 donor-level prevalence、疾病效应、细胞谱系方向或功能机制。

## 图形与纯视觉优化

R 是唯一绘图后端。analysis parameters 和 `visual` parameters 分离。original 图保留基础绘图输出；final 图仅允许调整尺寸、字体、point size、palette、label repel、legend 位置、留白和导出 DPI。最多三轮，每轮都必须：

v5 canonical 教学输出的每组 original/final 图只记录一个 hash-bound native-review round 1。审阅者在该轮逐张打开两种尺寸：五张 final 均为 terminal `keep`，没有未解决的 blocker/major；annotation original 的直接标签重叠被记录为 major，并由同组 final 的完整 legend 解决。original 与 final 使用相同数据、过滤、归一化、PCA/UMAP、聚类、marker 和声明边界，仅视觉样式不同。

1. 保持 data hash、分析参数与统计含义不变；
2. 打开 original 和 final-size PNG；
3. 记录两者 SHA-256、可见问题、证据层级与 `keep|revise|reselect|blocked`；
4. `revise` 只能由上一轮已登记的视觉问题授权。

过滤、归一化、聚类、阈值、分组、统计、scale midpoint 或声明强度的变化不属于视觉调优；需要新分析 baseline。

## 交付与成熟度

成功执行生成固定 run tree、冻结环境证据、RDS checkpoints、metrics/QC/markers/annotation tables、annotated object、original/final figures、native-review templates、`RESULTS.md`、`FIGURE_NOTES.md`、`QA_REPORT.md`、`ARTIFACT_INDEX.md`、`run_manifest.json` 与 append-only `artifact_ledger.jsonl`。

静态存在仅为 `parse-verified`；在精确锁定环境中跑通数据和 artifact QA 后才是 `data-verified`；逐张打开 original/final 并完成哈希绑定 review 后才是 `native-reviewed`。
