# Troubleshooting

## `--authorized` 缺失

`run` 和 `resume` 会以非零状态退出。请先由根 `tutorial_cli.py ... --authorize-run` 完成授权，再让根 CLI 传入本地 `--authorized`。不要直接删除此 gate。

## R 版本不是 4.5.3

driver 会执行 runtime probe 并阻塞。请显式传入 R 4.5.3 的绝对 `Rscript.exe`。不要把 PowerShell 的 `R` alias 当作解释器，不要修改系统 `PATH`。

本环境只支持 reviewed Windows R 4.5 binary 平台。非 Windows 会在安装或下载包之前明确阻塞；不会切换到 source、Conda 或其他 repository。

## Seurat 不是 5.5.0

`prepare_environment.R` 只在 task-local `renv` 中安装/验证 5.5.0。如果 exact version 不可获取、编译失败或 API smoke test 失败，保留 `logs/environment-prepare.log` 并将 release gate 标为 blocked。不得静默使用全局 5.4.0。

`renv::status()` 的 validated library search set 是 task-local project library 加 exact `R.home()/library`。后者只承载与 R 4.5.3 一起分发的 13 个 recommended packages；脚本会先证明 `.Library` 位于当前 `R.home()` 内，不会把 host user/site library 纳入验证，也不会把 recommended packages 复制进 project library。`renv-status.json` 同时记录 project-only 的 13-package mismatch 诊断与最终 combined search set 的 zero-diff/zero-row restore plan。

环境使用 `2026-04-23` PPM snapshot 的 Windows binary pins-first：安装前必须看到 exact renv 1.2.2、hdf5r 1.3.12 与 Seurat 5.5.0；下载 renv binary 后先核对 index repository、basename 与 committed size/SHA-256（该 PPM index 行当前没有 MD5sum），再分别装入 task-local bootstrap/project library。host/global renv 不参与且不要求存在。之后使用 `install.packages(type = "binary", dependencies = NA)` 先安装 direct prerequisites，再单独安装 Seurat。若事务失败，不把半成品环境当作成功；保留完整日志并在同一 binary snapshot 约束下安全重试或新建 cache key，不能 source compile 或降级。

## R 子进程架构变量缺失或冲突

Codex shell 可能缺少标准 Windows `PROCESSOR_ARCHITECTURE`。Python wrapper 用 `GetNativeSystemInfo` 获取真实 OS architecture；只有 native X64 被支持，且仅在 R child 的变量缺失时恢复为 `AMD64`。若已存在的 `PROCESSOR_ARCHITECTURE` 或 `PROCESSOR_ARCHITEW6432` 与真实架构冲突，流程在启动 R 前阻塞。不要在父 shell/global environment 手工伪造，也不要跳过架构 gate。

环境 provision 与 pipeline 均只执行一次，必须 native exit 0。stdout/stderr 分离捕获并扫描 `warning`、`stack imbalance`、iteration/alternation limit、`Error in`、access violation、segfault 和 fatal signal；completion marker 不能把非零返回码或禁止输出变成成功。不存在 hard-exit DLL、helper 或 retry。`inject_environment_fault.py` 会在已验证 cache 的 native R validator 完成 package/lock/status/H5 检查后、completion marker 提升前注入专用错误；负控必须观察到 native/wrapper 非零退出、哨兵存在、marker 不存在且 canonical run 整树指纹不变。

## Bioconductor closure 或 archive 校验失败

确认运行树中的 `bioconductor-3.21-archive-pins.json` 与源码完全一致。每个 archive 的 URL、basename、size、SHA-256、DESCRIPTION Package/Version 都必须吻合；compiled packages 必须来自同一 Bioconductor 3.21/R 4.5 Windows-binary cohort。唯一 source-format 例外是 `GenomeInfoDbData 1.2.14`，且 DESCRIPTION 必须声明 `NeedsCompilation: no`。不要单包混入 Bioc 3.22/3.23，也不要 source compile 或改 pins 迁就下载内容。

## Snapshot index 版本不匹配

若 index 中 Seurat 不是 5.5.0（例如错误选择 `2026-04-22` 会得到 5.4.0），provisioner 会在安装前阻塞。不要修改 expected version 迁就 index；确认 URL 必须精确为 `https://packagemanager.posit.co/cran/2026-04-23`。

## `Read10X_h5` smoke test 失败

这通常意味着 `hdf5r` 缺失/ABI 不兼容、H5 损坏或 Seurat H5 API 不可用。环境 helper 会在 freeze 前对本次真实 filtered H5 调用 `Seurat::Read10X_h5` 并要求非空。失败会阻塞；不能仅凭 `requireNamespace("hdf5r")` 成功就跳过实际 reader 测试。

## 下载失败或 checksum mismatch

- 网络失败：保留 `.partial` 之外的已冻结文件；对同一 URL 可安全重试。
- 与仓库 exact manifest 中 hash/size 不符：视为输入完整性失败，不覆盖原文件，也不允许把错误值学习成新基线。另建新的 run root 或人工核查来源。
- `spatial.tar.gz` 含绝对路径、`..`、symlink/hardlink：安全解压器会拒绝。

Visium 输入缓存必须通过 `--input-cache-root` 位于 fresh run root 之外。根 CLI 默认使用 `<cache-root>/inputs/visium-mouse-brain`。若把旧 run-local `inputs/` 当作默认来源，校验会明确失败；请传入已冻结的外部 input cache，不要复制或移动 canonical bytes 来绕过契约。

发布验证使用 `inject_corrupted_cache.py` 在临时 task-local copy 中翻转 filtered H5 的首字节；canonical inputs 不会被修改。负控必须观察到非零退出和 `SHA-256 mismatch for filtered_h5`，否则 release gate 阻塞。

## `Load10X_Spatial` 失败

确认 input root 同时具有 H5 文件和 `spatial/`，且包含 `scalefactors_json.json`、tissue positions 及 low/high-resolution image。driver 不下载 raw TIFF；此案例不需要 raw TIFF。

## Barcode reconciliation 失败

这是 blocker。不要通过丢弃 unmatched barcodes 来“让流程跑通”。流程会报告 Spatial assay cells、image cells、coordinates 三方计数，并保存六个有向差集；任一非零即停止。检查 H5 与 spatial archive 是否来自同一 dataset/version，以及 archive 是否被部分替换。

## `Hpca` 或 `Ttr` 不存在

pipeline 会阻塞，因为案例科学基线明确要求这两个 features。不要自动替换 gene symbol，也不要为生成漂亮图而改 feature。

## 部分图已生成但 R 返回非零

整次 stage 仍为失败。未通过 validation 的 `_staging` 文件不能登记为 checkpoint 或 data-verified artifact。修复后使用 `resume`。

## `pipeline-warnings.json` 为 `blocked`

pipeline 使用 `options(warn = 1)` 将每条 warning 即时写入日志，同时按 stage 聚合到 `logs/pipeline-warnings.json`；warning 不会被静默 suppress。API compatibility、non-finite/convergence/iteration-limit 等 numerical integrity、barcode/coordinate/image integrity，以及尚未分类的 warning 均 fail closed，发生后不得提升该 stage checkpoint。

旧默认 `sctransform` S40 的 `theta.ml`/`glm.nb` warnings 不是当前候选证据，也不能复用其 checkpoint。当前 S40 必须显式为 `vst.flavor=v2`、`method=glmGamPoi_offset`，SCTModel 必须确认 `glmGamPoi_check=true`。任何 warning 仍 fail closed；即使矩阵有限、对象可读或图可生成，也不能自动 allowlist。

## Resume 报 hash mismatch

分析参数、视觉参数、代码、输入或环境 lock 改变后，旧 checkpoint 不可复用。新建 run/baseline；不要手工编辑 checkpoint hash。

## Native review 尚未完成

生成 PNG 不等于视觉复核。必须实际打开每个 original/final pair，填写 review JSON，并验证 terminal decision。若无法原生查看，应明确标记 `blocked`，不能填 `keep`。
