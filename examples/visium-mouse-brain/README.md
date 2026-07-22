# Visium Mouse Brain：Seurat 5.5.0 端到端教学案例

本案例复现 Seurat spatial vignette 中 Mouse Brain Sagittal-Anterior 的基础路线：10x Visium vendor inputs → `Load10X_Spatial` → barcode/coordinate/image QC → `SCTransform` → PCA → expression-neighbor clustering → coordinate-faithful spatial overlays → hash-bound native visual review → auditable reports。

它是一个单切片、spot-level、描述性教学案例。表达聚类称为 **spot cluster**，不是 cell type，也不是空间正则化 domain。

## 安全边界

- `run` / `resume` 必须同时由根 `tutorial_cli.py --authorize-run` 授权，并向本地 driver 传入 `--authorized`。
- 10x 原始输入只写入显式的 task-local external input cache，并由 fresh run 与 resume 直接只读复用；不会复制进 run root。依赖只写入指定的 task-local environment cache。
- 仅支持 reviewed Windows R 4.5 binary 平台。R 必须恰好为 4.5.3，task-local `renv` 必须恰好为 1.2.2，Seurat 必须恰好为 5.5.0，`hdf5r` 必须恰好为 1.3.12。任何不匹配都会以非零状态停止。
- CRAN 依赖来自 Posit Public Package Manager `https://packagemanager.posit.co/cran/2026-04-23`；Bioconductor 依赖来自完整、同代的 3.21/R 4.5 Windows binary closure。`config/bioconductor-3.21-archive-pins.json` 冻结 47 个 archive 的 URL、版本、size、SHA-256 和 DESCRIPTION 元数据；仅 `GenomeInfoDbData 1.2.14` 是 `NeedsCompilation=no` 的平台无关数据 archive。禁止 source compilation、跨 release 混装、切换 snapshot 或回退到 Seurat 5.4.0。
- 不依赖 host/global `renv`。授权事务从固定 snapshot 下载 exact renv 1.2.2 与 BiocManager 1.30.27 Windows binaries，在安装前核对 index repository、basename、committed size/SHA-256（PPM 当前 renv 行未提供 MD5sum），再分别安装到 task-local bootstrap library 和 project library；`renv.lock` 仅由该 task-local 1.2.2 生成。安装始终使用 Windows binary（唯一例外仍是 hash-pinned、`NeedsCompilation=no` 的 GenomeInfoDbData 数据包）；为使 renv 解析该平台无关数据包的 provenance，snapshot/status 子进程只在元数据查询时读取 BioCann source index，并须精确得到 `GenomeInfoDbData 1.2.14`，随后证明 full closure、`renv::status()` 与 restore plan 均一致/为空。
- Python 启动每个 R 子进程前用 `GetNativeSystemInfo` 确认 native X64；若子环境缺少 `PROCESSOR_ARCHITECTURE`，只在该 child 中恢复为 `AMD64`。冲突、未知、ARM64/x86 均 fail closed，父进程环境不修改。环境 provision 与 pipeline 都必须单次 native exit 0；stdout/stderr 分离捕获、hash 绑定并扫描 warning、stack imbalance、iteration/alternation limit、error、access violation、segfault 等禁止信号。没有 helper、retry 或异常退出绕过。
- downloader 在任何下载前即要求 `input-manifest.json` 提供 exact size/SHA-256；H5 或 spatial archive 的任何字节变化都会阻塞，不存在 null/first-learning 模式。
- 调图最多三轮；只允许 `config/visual-params.json` 中列出的纯视觉键变化。

## 根 CLI 推荐调用

从仓库根目录执行：

```powershell
$RunRoot = [IO.Path]::GetFullPath((Join-Path $PWD 'runs/visium-mouse-brain/canonical'))
$CacheRoot = [IO.Path]::GetFullPath((Join-Path $PWD '.cache/tutorials'))
$InputCacheRoot = [IO.Path]::GetFullPath((Join-Path $CacheRoot 'inputs/visium-mouse-brain'))
$Rscript = 'C:\Program Files\R\R-4.5.3\bin\Rscript.exe'

python tutorial_cli.py plan --case visium-mouse-brain
python tutorial_cli.py run --case visium-mouse-brain --authorize-run `
  --run-root $RunRoot --cache-root $CacheRoot `
  --input-cache-root $InputCacheRoot --rscript $Rscript
# 此时状态必须是 awaiting_native_review；实际打开全部 original/final PNG，
# 再据实填写 06_figures/review/review-round-1.json。
python tutorial_cli.py report --case visium-mouse-brain `
  --run-root $RunRoot --input-cache-root $InputCacheRoot
python tutorial_cli.py verify --case visium-mouse-brain `
  --run-root $RunRoot --input-cache-root $InputCacheRoot
```

`verify` 不是自动图像审核器。计算完成后直接执行它会因 native review 尚未完成而失败；只有真实查看、填写 review record 并执行 `report` 后，terminal verification 才应通过。

完成首次交付后，再单独验证 checkpoint 与环境缓存复用。`resume` 会把运行态重新置为 `awaiting_native_review`，因此复用验证后须再次执行 `report` 和 `verify`：

```powershell
python tutorial_cli.py resume --case visium-mouse-brain --authorize-run `
  --run-root $RunRoot --cache-root $CacheRoot `
  --input-cache-root $InputCacheRoot --rscript $Rscript
python tutorial_cli.py report --case visium-mouse-brain `
  --run-root $RunRoot --input-cache-root $InputCacheRoot
python tutorial_cli.py verify --case visium-mouse-brain `
  --run-root $RunRoot --input-cache-root $InputCacheRoot
python scripts/validate_tutorial_ci_output.py `
  --case visium-mouse-brain `
  --run-root $RunRoot
```

真实 run 完成后可执行 task-local 损坏缓存负控；脚本只复制输入到临时目录并翻转 copied H5 的一个字节，不修改 canonical inputs：

```powershell
python examples/visium-mouse-brain/inject_corrupted_cache.py `
  --run-root $RunRoot `
  --input-cache-root $InputCacheRoot `
  --output (Join-Path $RunRoot 'logs/corrupted-cache-negative-control.json')
```

只有 verifier 以非零状态报告 `SHA-256 mismatch for filtered_h5` 才记为通过。

环境非零负控在验证过的 input/environment cache 上执行 native R cache validator，并在 completion marker 提升前注入专用故障；同时核对 canonical run 整树指纹不变：

```powershell
$FailureRoot = [IO.Path]::GetFullPath((Join-Path $PWD 'runs/visium-mouse-brain/fault-before-completion-marker'))
python examples/visium-mouse-brain/inject_environment_fault.py `
  --canonical-run-root $RunRoot --failure-run-root $FailureRoot `
  --cache-root $CacheRoot --input-cache-root $InputCacheRoot `
  --rscript $Rscript `
  --output (Join-Path $FailureRoot 'fault-injection-evidence.json')
```

仓库中的公开教学快照由通过 current-code fresh/resume、external input/environment cache reuse、两个负向门禁、native review、report 与 verify 的真实 Seurat 5.5.0 运行导出。它只包含结果表、报告、三组 original/final PNG、native-review record 和脱敏验证证据；不包含 10x 原始文件、RDS、task-local library、cache、checkpoint 或 runtime binary。独立复核 exact inventory、hash、图像尺寸、barcode 对账与路径脱敏：

```powershell
python examples/visium-mouse-brain/verify_expected_output.py
```

## 案例 driver 接口

根 CLI 先调用幂等环境 wrapper，再调用下列稳定 driver 接口。wrapper 会初始化固定树、校验外部只读 input cache，并在真实 H5 smoke 后冻结 task-local renv；driver 会验证同一 marker 并跳过重复 provision：

```powershell
$RunRoot = [IO.Path]::GetFullPath((Join-Path $PWD 'work/runs/visium-mouse-brain'))
$CacheRoot = [IO.Path]::GetFullPath((Join-Path $PWD 'work/cache'))
$InputCacheRoot = [IO.Path]::GetFullPath((Join-Path $CacheRoot 'inputs/visium-mouse-brain'))
$Rscript = '<ABSOLUTE_PATH_TO_RSCRIPT_EXE>'

python examples/visium-mouse-brain/prepare_environment.py `
  --authorized `
  --run-root $RunRoot `
  --cache-root $CacheRoot `
  --input-cache-root $InputCacheRoot `
  --rscript $Rscript

python examples/visium-mouse-brain/case_driver.py run `
  --authorized `
  --run-root $RunRoot `
  --cache-root $CacheRoot `
  --input-cache-root $InputCacheRoot `
  --rscript $Rscript

python examples/visium-mouse-brain/case_driver.py resume `
  --authorized `
  --run-root $RunRoot `
  --cache-root $CacheRoot `
  --input-cache-root $InputCacheRoot `
  --rscript $Rscript

python examples/visium-mouse-brain/case_driver.py report `
  --run-root $RunRoot `
  --input-cache-root $InputCacheRoot

python examples/visium-mouse-brain/case_driver.py verify `
  --run-root $RunRoot `
  --input-cache-root $InputCacheRoot
```

`run` 要求不存在已开始的 analysis manifest；如前次运行中断，使用 `resume`。driver 会：

1. 复制 immutable tutorial request/plan/config 到固定 run tree；
2. 按仓库已冻结的 exact size/SHA-256 校验 H5 与 spatial archive，安全解压 vendor spatial assets；
3. 先断言 CRAN index 中 renv=1.2.2、Seurat=5.5.0、hdf5r=1.3.12，下载并预安装校验 task-local renv binary（不读取 host renv）；随后逐个核对并安装 frozen Bioconductor 3.21 closure（`glmGamPoi 1.20.0`、`SparseArray 1.8.1`、`BiocVersion 3.21.1`），用真实 H5 执行 `Read10X_h5` smoke test；
4. 调用参数化 R pipeline，逐阶段验证并生成 checkpoints；
5. 验证报告、表格、对象、图像及 ledger 的一致性。

## 固定 run tree

```text
<run-root>/
  00_request/{PROMPT.md,request.json,input-manifest.json,resolved-inputs.json}
  01_plan/{ANALYSIS_DESIGN.md,route.json,workflow.plan.json}
  02_environment/{environment-spec.json,bioconductor-3.21-archive-pins.json,environment.locked.json,environment.probe.json,environment-provision.complete.json,environment-evidence.json,renv-status.json,renv.lock}
  03_scripts/{run_pipeline.R,analysis-params.json,visual-params.json}
  04_intermediate/<stage_id>/
  05_results/{tables/,objects/}
  06_figures/{original/,final/,review/}
  07_reports/{RESULTS.md,FIGURE_NOTES.md,QA_REPORT.md,ARTIFACT_INDEX.md}
  logs/{pipeline-warnings.json,...}
  manifest/{run_manifest.json,artifact_ledger.jsonl,execution-summary.json}
```

原始输入位于 run tree 外的 `<input-cache-root>/{filtered H5,spatial archive,spatial/}`。`logs/input-cache-reuse.json` 记录 direct-read/no-copy、逐文件 hash 与 canonical cache 未修改证据。

## 教学数字的冻结规则

仓库不预写 matrix spot 数或 in-tissue spot 数。首次真实运行只有在 H5、tissue positions、Seurat object、Spatial assay cells、image cells 与 coordinates 完成对账后，才把实际数量写入。assay/image/coordinates 六个有向差集必须全部为 0；否则流程保留差集证据并阻塞，不放宽契约：

- `05_results/tables/barcode_reconciliation.csv`
- `05_results/tables/barcode_set_reconciliation.json`
- `05_results/tables/barcode_set_differences.csv`
- `05_results/tables/attrition.csv`
- `manifest/execution-summary.json`
- `07_reports/RESULTS.md`

`verify` 会再次核对这些数字；文档数字不能单独充当数据验证。

## Native visual review

R pipeline 只会把图形状态登记为 `awaiting_native_review`。必须用本地原生图像查看器实际打开每张 original 与 final-size PNG，再根据 `06_figures/review/review-round-1.template.json` 填写复核记录。最多三轮；`keep` 前不得存在 blocker/major finding。代码、metadata、Pillow 或 OpenCV 均不能替代 native review。

## 当前候选与旧证据边界

旧 canonical run 使用默认 `sctransform` 拟合并产生 192 次数值 warning，因此永久排除在当前候选和 expected-output 之外。当前流程显式执行 `SCTransform(vst.flavor = "v2", method = "glmGamPoi_offset")`，并要求 SCTModel 实际记录 `v2`、`glmGamPoi_offset`、`glmGamPoi_check=true`；同时断言 3,000 variable features、spot 数不变、SCT 三层和 PCA embeddings/loadings/stdev 全有限。

当前公开 snapshot 的源运行完成 2,695 个 matrix/in-tissue/loaded/retained spots、六个 assay/image/coordinate 有向差集均为 0、11 个 expression-derived spot clusters、30 个有限 PCs、零 structured/external warning，并通过 fresh、8 个 checkpoint resume、direct-read input cache reuse、native R environment cache revalidation、checksum/非零退出负控以及三组 original/final native review。该本地教学证据不替代 remote Actions、匿名 clone、许可/泄漏扫描或 commit-bound release evidence；任一后续门禁缺失时仍不得打 tag 或创建 Release。

常见故障见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)，科学设计与声明边界见 [ANALYSIS_DESIGN.md](ANALYSIS_DESIGN.md)，第三方数据许可见 [DATA_LICENSE.md](DATA_LICENSE.md)。
