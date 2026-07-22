# 故障排查

## checksum 或大小失败

症状：下载器报告 `size mismatch` 或 `SHA-256 mismatch`。

处理：停止。不要解压、不要复用该文件、不要修改 manifest 来适配下载结果。检查代理/CDN 是否返回 HTML、是否被中断，删除或隔离 task-local `.partial`/错误归档后从同一官方 URL 重试。若官方文件确实更新，应先独立核实来源和许可，并通过新的 plan 更新固定 hash。

## 安全解压失败

症状：`unsafe archive member`、缺少 `filtered_gene_bc_matrices/hg19` 或缺少 `matrix.mtx`/`genes.tsv`/`barcodes.tsv`。

处理：停止并保留日志。不要跳过路径穿越检查，不要用不明镜像补文件。

## R 或 Seurat 版本不符

症状：`R_VERSION_MISMATCH` 或 `SEURAT_VERSION_MISMATCH`。

处理：使用 environment manager 在 task-local 目录 provision/restore 精确的 R 4.5.3 + Seurat 5.5.0 环境，然后重新 `verify -> freeze -> execute`。不得回退到全局 Seurat 5.4.0，也不得在 `run_pipeline.R` 中加入安装命令。

## task-local renv bootstrap 失败

症状：`RENV_BINARY_PIN_MISMATCH`、`TASK_LOCAL_RENV_BOOTSTRAP_FAILED`，或 marker 缺少 `renv_bootstrap`。

处理：停止执行并保留日志。核对固定 snapshot、`renv_1.2.2.zip` 的大小与 SHA-256，以及 `<run-root>/02_environment/bootstrap-library` 的写权限。不得改用 host/global renv，不得跳过 archive 校验；若上游 archive 合法变更，必须先更新公开 pin、环境计划和验证基线。

## Windows R 子进程架构或原生退出失败

症状：`PROCESSOR_ARCHITECTURE conflicts`、`unsupported Windows native architecture`、`R_PROCESS_ARCHITECTURE_EVIDENCE_INVALID`、`NATIVE_EXIT_COMPLETION_*`，或进程返回 0 但 `forbidden_scan` 失败。

处理：检查 `logs/environment-process-evidence.json` 或 `logs/r-pipeline-process-evidence.json` 及其绑定的 stdout/stderr。正式范围仅支持由 `GetNativeSystemInfo` 识别为 X64 的 Windows，缺失的 `PROCESSOR_ARCHITECTURE` 只能在 R 子进程副本中恢复为 `AMD64`；不要修改父 shell、系统环境变量或绕过冲突门禁。不得恢复 DLL/hard-exit helper，也不得因完成 marker 已存在而接受非零退出、stack imbalance、warning、`execution halted`、access violation 或其他禁止模式。修复环境根因后使用新的 run root 重跑。

若警告来自可显式冻结的默认行为，不得将其 allowlist，也不得使用 `suppressWarnings()` 或 handler muffling。例如 feature-name 下划线必须在 `CreateSeuratObject` 前按 `_` → `-` 显式转换并输出 mapping/无重复/矩阵不变证据；UMAP 必须显式传入 `umap.method="uwot"` 与 `metric="cosine"`。Seurat 5.5 对显式 `uwot` 仍会发出一次性迁移提示，因此只允许在该调用的最小作用域使用官方 `Seurat.warn.umap.uwot=FALSE` transition option，并必须记录 option 应用、先前状态和恢复成功。分析入口固定 `options(warn=1)`；`Warning messages:` 同样是 release blocker。修改源码后使用全新 run/cache，不能复用已生成 warning 的 checkpoints。

## canonical 数值不符

症状：输入细胞不等于 2,700、QC retained 不等于 2,638 或 clusters 不等于 9。

处理：这是 release blocker。先核对输入 hash、读取目录、Seurat 版本、参数 hash 和 seeds。不要为了对齐答案临时改变 QC、PC 数、resolution、聚类算法或 seed；任何科学参数变化都需要新 plan 和新 baseline。

## checkpoint 签名不匹配

症状：既有 stage 的 `analysis_signature` 与当前输入/参数/环境不一致。

处理：不要覆盖旧 checkpoint。创建新的 run root。相同签名的已完成 stage 才能在 `resume` 中复用。

## R 子进程失败但存在图形

处理：非零退出或 stdout/stderr 禁止模式命中都会使整个 stage 失败；部分图形不能升级为成功或 native-reviewed。检查 `logs/r-pipeline.stdout.log`、`logs/r-pipeline.stderr.log` 与 `logs/r-pipeline-process-evidence.json`，修复后从最后一个 hash-valid checkpoint 恢复。

## native visual review 未完成

代码检查、PNG metadata 或自动图像脚本都不能代替原生复核。必须实际打开每张 original 与 final-size PNG，并在 `06_figures/review/` 中提交绑定两者 SHA-256 的结构化记录。无法打开时如实标记 `blocked`，不要写 `keep`。

## 调图需要改变数据或统计

禁止在 visual-only loop 中修改过滤、归一化、HVG、PC 数、聚类、阈值、分组或统计含义。若确有科学原因，结束本轮 review，产生新的分析计划和 data baseline；不要把它记录成“视觉优化”。
