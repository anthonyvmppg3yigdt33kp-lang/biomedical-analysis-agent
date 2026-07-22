# PBMC3K：Seurat 5.5.0 全流程教学案例

本目录是一个可独立审计的单文库 scRNA-seq 教学案例。它使用 Seurat 官方 PBMC3K 教程对应的 10x 公共数据，严格核对 `2700 -> 2638 -> 9 clusters`，并把规划、执行、checkpoint、报告和 native visual review 串成一条可复现链。

仓库不包含原始数据。下载器只接受 `input_manifest.json` 中固定的官方 URL、7,621,991 bytes 和 SHA-256；数据按 CC BY 4.0 单独署名。仓库 MIT 许可只覆盖原创代码与文档。

## 文件导览

| 文件 | 作用 |
|---|---|
| `PROMPT.md` | 可复制的原始需求提示词 |
| `request.json` | canonical plan-mode request |
| `route.expected.json` | 路由断言 |
| `workflow.plan.expected.json` | 编译计划的语义断言 |
| `ANALYSIS_DESIGN.md` | 科学设计、DAG、QA 和声明边界 |
| `input_manifest.json` | URL、许可、大小、checksum 与 canonical 数值 |
| `environment-spec.json` | R 4.5.3 / Seurat 5.5.0 精确环境声明 |
| `prepare_environment.py` / `.R` | 授权后 provision、复用与冻结 task-local `renv` |
| `params.json` | 分析参数与独立的 visual parameters |
| `download_inputs.py` | 标准库下载、checksum、大小和安全解压 |
| `case_driver.py` | 根级 CLI 调用的 task-local run/resume/verify/report 驱动 |
| `run_pipeline.R` | 无安装命令、checkpoint-aware 的参数化 R 流程 |
| `export_expected_output.py` | 仅从完整通过门禁的 run 导出 allow-list 公共衍生产物 |
| `verify_expected_output.py` | 独立重算公共参考输出的哈希、图审、表格、许可与泄漏门禁 |
| `expected-output/` | 已验证的精简教学参考输出，不含原始数据、RDS、环境或 cache |
| `TROUBLESHOOTING.md` | 故障分类与安全恢复路径 |

## 推荐入口

根级统一 CLI 是正式接口：

```powershell
python tutorial_cli.py plan --case pbmc3k
python tutorial_cli.py run --case pbmc3k --authorize-run
python tutorial_cli.py resume --case pbmc3k --authorize-run
```

`plan` 只读，不下载、不安装、不执行。`run`/`resume` 必须显式授权，并只在 task-local 目录使用锁定环境。

计算完成后，review JSON 会停在 `awaiting_native_review`。必须实际打开
`06_figures/original/` 和 `06_figures/final/` 下的五组当前 PNG，把查看工具、
两张图的当前 SHA-256、发现和 `keep|revise|reselect|blocked` 决策写入对应的
`06_figures/review/*.review.json`；不得直接复制 `expected-output/` 的 `keep`
结论。完成 hash-bound review 后，先重建报告，再运行 terminal verifier：

```powershell
python tutorial_cli.py report --case pbmc3k
python tutorial_cli.py verify --case pbmc3k
```

若要单独开发本案例，可调用 case-local driver；`--run-root` 必须是尚未存在或已由本案例初始化的 task-local run 目录。根级 CLI 在记录用户的 `--authorize-run` 后还会向 driver 传递第二层 `--authorized` 防误触标记：

```powershell
python examples/pbmc3k/case_driver.py run --run-root C:\task\runs\pbmc3k\dev --cache-root C:\task\cache\pbmc3k --rscript "C:\Program Files\R\R-4.5.3\bin\Rscript.exe" --authorized
python examples/pbmc3k/case_driver.py resume --run-root C:\task\runs\pbmc3k\dev --cache-root C:\task\cache\pbmc3k --rscript "C:\Program Files\R\R-4.5.3\bin\Rscript.exe" --authorized
python examples/pbmc3k/case_driver.py report --run-root C:\task\runs\pbmc3k\dev
python examples/pbmc3k/case_driver.py verify --run-root C:\task\runs\pbmc3k\dev
```

直接使用 driver 前先准备环境；该步骤只写入指定 run/cache 根，不修改全局 R library：

```powershell
python examples/pbmc3k/prepare_environment.py --run-root C:\task\runs\pbmc3k\dev --cache-root C:\task\cache\pbmc3k --rscript "C:\Program Files\R\R-4.5.3\bin\Rscript.exe" --authorized
```

准备器把精确包版本安装到 `<cache-root>/e/<16-char-key>/l`，但在 marker 中保留完整 64 字符 lock hash 并验证短键绑定。它先断言 Posit Package Manager `2026-04-23` snapshot 的 Windows R 4.5 binary index 中 `Seurat == 5.5.0`、`renv == 1.2.2`，再分别核对 `Seurat_5.5.0.zip` 与 `renv_1.2.2.zip` 的固定大小和 SHA-256。经验证的 renv binary 先安装到 `<run-root>/02_environment/bootstrap-library`，环境管理只从该 task-local namespace 运行，不要求、读取或修改 host renv。随后以 `type="binary", dependencies=NA` 安装 Seurat 及硬依赖（不安装 Suggests），并从已验证 archive 重装顶层 Seurat/renv，完成 exact-version/API smoke 后生成 `renv.lock`。相同命令最多重试两次，不回退版本；相同 cache key 可复用，既有 run lock 不一致时明确失败、不覆盖历史。

所有 Python 启动的 R 进程都从 `GetNativeSystemInfo` 读取真实 Windows 架构，并只在复制出的子进程环境缺少 `PROCESSOR_ARCHITECTURE` 时恢复为规范值；当前正式支持范围仅为 `X64 -> AMD64`。未知架构、ARM64/X86 或与既有 `PROCESSOR_ARCHITECTURE`/`PROCESSOR_ARCHITEW6432` 冲突时立即失败，父 Python 进程环境不被修改。R 必须自然返回 `0`；不再编译、加载或记录外部退出 DLL。

同一 run/cache 再次执行时，准备器仅在 `renv.lock`、probe、`shutdown_mode=native_exit` completion marker、结构化进程证据、bootstrap DESCRIPTION 和六个 task-library package DESCRIPTION 的记录哈希全部一致时走 fast reuse，并写出脱敏的 `logs/environment-cache-reuse.json`。分析 R 入口显式设置 `options(warn=1)`，使普通和复数 R warnings 都立即进入 stderr；进程证据绑定返回码、架构、脱敏 stdout/stderr 哈希以及禁止模式扫描。任一文件缺失、越界、哈希不符、返回码非零，或命中 `warning:`、`warning message(s)`、`stack imbalance`、`execution halted`、access violation 等禁止模式都会 fail closed，不会用耗时或“目录存在”冒充缓存命中。

case-local runner 不安装 R 包。运行前，上层 environment manager 必须已建立并冻结 task-local `renv`，且所传 `Rscript` 能看到精确的 Seurat 5.5.0。全局 Seurat 5.4.0 会被明确拒绝。

PBMC3K 原始 feature names 含下划线。runner 在 `CreateSeuratObject` 前显式执行与 Seurat 一致的 `_` → `-` 规范化，拒绝任何转换后重复，并用 `feature_name_mapping.csv` 与 summary 证明 32,738 行映射、矩阵维度及 count values 未改变；因此不会依赖 Seurat 的隐式 coercion warning。UMAP 同样显式固定为 R-native `uwot` + `cosine`。由于 Seurat 5.5 即使参数已显式传入仍会发出一次性默认迁移提示，runner 仅在该调用的最小作用域设置官方 `Seurat.warn.umap.uwot=FALSE` option 并保证恢复；证据明确记录未使用 `suppressWarnings()`、handler muffling 或 warning allowlist，算法不变。

## 输出树

```text
<run-root>/
  00_request/{intent.yaml,input_manifest.json,request.json}
  01_plan/{ANALYSIS_DESIGN.md,workflow.plan.json}
  02_environment/{bootstrap-library/,environment-spec.json,environment_manifest.json,environment.probe,provision.complete,renv.lock}
  03_scripts/{run_pipeline.R,params.json,analysis_signature.txt}
  04_intermediate/<SC-stage>/{*.rds,stage.complete.json}
  05_results/{tables/,objects/}
  06_figures/{original/,final/,review/}
  07_reports/{RESULTS.md,FIGURE_NOTES.md,QA_REPORT.md,ARTIFACT_INDEX.md}
  logs/{environment-process-evidence.json,r-pipeline-process-evidence.json,...}
  manifest/{run_manifest.json,artifact_ledger.jsonl,execution-summary.json}
```

下载 cache 默认位于 `<run-root>/_input_cache`，不属于分发 artifact，也不应提交。

## 已验证公共参考输出

`expected-output/` 已从 `pbmc3k-native-v1-20260722-v5` 的 clean native-exit run 整体导出：fresh run 与六节点 checkpoint resume 均通过，task-local 环境 cache 复用和禁止模式扫描有效，五组原图/终稿均在单个 hash-bound native-review round 1 中完成终态 `keep`。annotation original 的标签重叠在同组 final 中由完整 legend 解决，所有图均保持相同数据与分析含义。canonical 结果为 2,700 input cells、2,638 QC-retained cells 和 9 clusters；独立 checksum mismatch 与 completion-marker fault 注入也均按预期 fail closed。公共包共 38 个 allow-listed 文件，其 manifest/ledger 均由 exporter 重算，并可由下述 verifier 独立验证；未手工改写执行证据。公共参考不包含 10x archive、解压矩阵、cell-level 导出、RDS、checkpoint、R library、cache 或绝对工作站路径。

从仓库根目录独立验证已提交参考输出：

```powershell
python examples/pbmc3k/verify_expected_output.py
```

维护者只有在源 run 的 `verify` 为 clean pass、成熟度为 `native-reviewed` 且存在显式环境复用证据时，才能在尚无 `expected-output/` 的干净工作树中执行：

```powershell
python examples/pbmc3k/export_expected_output.py --run-root C:\task\runs\pbmc3k\verified-run
```

导出器不会覆盖既有参考输出；需要更新时必须先人工审阅旧版本并显式移除。`ARTIFACT_INDEX.md` 提供人读索引，`manifest/artifact_ledger.jsonl` 对除自身外的全部文件绑定 byte size 和 SHA-256。

## 成功标准

- 输入归档大小与 SHA-256 精确匹配；安全解压结构正确；
- R 为 4.5.3，Seurat 为 5.5.0；
- 2,700 input cells、2,638 QC retained cells、9 clusters；
- 所有 required checkpoints 和 artifact 存在，ledger 的 SHA-256 可重算；
- R 子进程由 AMD64 子环境启动并自然退出 0；非零退出或 stdout/stderr 禁止模式命中即失败，即使留下部分 PNG；
- 每张 final figure 有 original 配对和 native-review 记录；未完成 native review 时 maturity 必须停在 `data-verified`/`rendered_pending_native_review`；
- 声明始终限定为单文库描述性结果。

请同时阅读 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) 和 [ANALYSIS_DESIGN.md](ANALYSIS_DESIGN.md)。
