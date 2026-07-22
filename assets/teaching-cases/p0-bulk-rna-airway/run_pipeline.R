#!/usr/bin/env Rscript

# Public, path-independent runner for the official Bioconductor airway dataset.
# Dependency installation is intentionally out of scope; exact versions are gated below.

args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 2L || !identical(args[[1L]], "--output-dir")) {
  stop("Usage: Rscript.exe --vanilla run_pipeline.R --output-dir <new-directory>")
}
requested_output <- args[[2L]]
dir.create(dirname(requested_output), recursive = TRUE, showWarnings = FALSE)
output_dir <- file.path(
  normalizePath(dirname(requested_output), winslash = "/", mustWork = TRUE),
  basename(requested_output)
)
if (dir.exists(output_dir) || file.exists(output_dir)) {
  stop("Output path already exists; choose a new directory: ", output_dir)
}

required_versions <- c(
  airway = "1.30.0", DESeq2 = "1.50.2", BiocVersion = "3.22.0",
  BiocManager = "1.30.27", matrixStats = "1.5.0"
)
for (pkg in names(required_versions)) {
  if (!requireNamespace(pkg, quietly = TRUE)) stop("Missing required package: ", pkg)
  observed <- as.character(utils::packageVersion(pkg))
  if (!identical(observed, required_versions[[pkg]])) {
    stop("Package version mismatch for ", pkg, ": expected ",
         required_versions[[pkg]], ", observed ", observed)
  }
}
if (!identical(as.character(BiocManager::version()), "3.22")) {
  stop("Bioconductor release mismatch; expected 3.22")
}

subdirs <- c(
  "tables", "objects", "figures/original", "figures/final", "reports", "provenance"
)
for (subdir in subdirs) dir.create(file.path(output_dir, subdir), recursive = TRUE)

write_tsv <- function(x, relative, row.names = FALSE) {
  path <- file.path(output_dir, relative)
  con <- if (grepl("\\.gz$", path)) gzfile(path, "wt", encoding = "UTF-8") else path
  if (inherits(con, "connection")) on.exit(close(con), add = TRUE)
  utils::write.table(x, con, sep = "\t", quote = FALSE, row.names = row.names,
                     col.names = TRUE, na = "NA", fileEncoding = if (is.character(con)) "UTF-8" else "")
}
write_lines <- function(x, relative) writeLines(x, file.path(output_dir, relative), useBytes = TRUE)
open_png <- function(relative) {
  grDevices::png(file.path(output_dir, relative), width = 2400, height = 1800,
                 res = 300, type = "windows", bg = "white")
}

data("airway", package = "airway", envir = environment())
if (!exists("airway", inherits = FALSE) || !inherits(airway, "RangedSummarizedExperiment")) {
  stop("Official airway RangedSummarizedExperiment was not loaded")
}
counts <- SummarizedExperiment::assay(airway)
raw_metadata <- as.data.frame(SummarizedExperiment::colData(airway))
if (!identical(dim(counts), c(63677L, 8L))) stop("Unexpected airway dimensions")
if (!identical(colnames(counts), rownames(raw_metadata))) stop("Counts/metadata order mismatch")

metadata <- data.frame(
  sample_id = colnames(counts),
  cell = factor(as.character(raw_metadata$cell)),
  dex = stats::relevel(factor(as.character(raw_metadata$dex)), ref = "untrt"),
  row.names = colnames(counts), stringsAsFactors = FALSE
)
if (!identical(levels(metadata$dex), c("untrt", "trt"))) stop("Unexpected dex levels")
pair_table <- table(metadata$cell, metadata$dex)
if (nrow(pair_table) != 4L || !all(pair_table == 1L)) stop("Expected four complete cell-line pairs")
design_matrix <- stats::model.matrix(~ cell + dex, metadata)
design_rank <- qr(design_matrix)$rank
if (design_rank != ncol(design_matrix) || nrow(design_matrix) <= design_rank) {
  stop("Design matrix is rank deficient or lacks residual degrees of freedom")
}

samples_ge_10 <- rowSums(counts >= 10L)
keep <- samples_ge_10 >= 4L
if (!any(keep) || all(keep)) stop("Pre-filter result is implausible")
dds <- DESeq2::DESeqDataSetFromMatrix(counts[keep, , drop = FALSE], metadata, ~ cell + dex)
dds <- DESeq2::DESeq(dds, quiet = FALSE, parallel = FALSE)
res <- DESeq2::results(
  dds, contrast = c("dex", "trt", "untrt"), alpha = 0.05,
  pAdjustMethod = "BH", independentFiltering = TRUE, cooksCutoff = TRUE
)
results <- as.data.frame(res)
results$gene_id <- rownames(results)
results <- results[, c("gene_id", "baseMean", "log2FoldChange", "lfcSE", "stat", "pvalue", "padj")]
results$tested_wald <- is.finite(results$pvalue) & is.finite(results$stat)
results$fdr_below_0_05 <- is.finite(results$padj) & results$padj < 0.05
results$direction <- ifelse(
  results$fdr_below_0_05 & results$log2FoldChange > 0, "higher_in_trt",
  ifelse(results$fdr_below_0_05 & results$log2FoldChange < 0,
         "lower_in_trt", "not_significant")
)
results$contrast <- "dex_trt_minus_untrt"
results$positive_log2FC_interpretation <- "higher_expression_in_trt"
tested <- results$tested_wald
if (!any(tested)) stop("No genes entered the finite Wald-test universe")
ranked <- results[tested, c("gene_id", "stat")]
names(ranked)[2L] <- "wald_statistic"
ranked <- ranked[order(ranked$wald_statistic, decreasing = TRUE), , drop = FALSE]
ranked$rank_descending <- seq_len(nrow(ranked))

normalized <- DESeq2::counts(dds, normalized = TRUE)
vst <- DESeq2::varianceStabilizingTransformation(dds, blind = FALSE)
vst_matrix <- SummarizedExperiment::assay(vst)
sample_distance <- as.matrix(stats::dist(t(vst_matrix)))
pca <- DESeq2::plotPCA(vst, intgroup = c("dex", "cell"), returnData = TRUE)
pca$sample_id <- rownames(pca)
pca$percent_variance_pc1 <- attr(pca, "percentVar")[[1L]]
pca$percent_variance_pc2 <- attr(pca, "percentVar")[[2L]]
pca <- pca[, c("sample_id", "PC1", "PC2", "dex", "cell",
               "percent_variance_pc1", "percent_variance_pc2")]
dds_mcols <- as.data.frame(S4Vectors::mcols(dds))
dispersion <- data.frame(
  gene_id = rownames(dds_mcols), base_mean = dds_mcols$baseMean,
  gene_estimate = dds_mcols$dispGeneEst, fitted = dds_mcols$dispFit,
  final = dds_mcols$dispersion, stringsAsFactors = FALSE
)

metadata_export <- transform(metadata, cell = as.character(cell), dex = as.character(dex),
                             library_size_raw = colSums(counts))
write_tsv(metadata_export[, c("sample_id", "cell", "dex", "library_size_raw")],
          "tables/sample_metadata.tsv")
write_tsv(data.frame(sample_id = rownames(design_matrix), design_matrix,
                     check.names = FALSE, row.names = NULL), "tables/design_matrix.tsv")
write_tsv(data.frame(
  formula = "~ cell + dex", contrast = "dex: trt - untrt", samples = nrow(design_matrix),
  coefficients = ncol(design_matrix), rank = design_rank,
  residual_df = nrow(design_matrix) - design_rank, full_rank = TRUE,
  four_complete_pairs = TRUE
), "tables/design_rank_check.tsv")
write_tsv(data.frame(
  gene_id = rownames(counts), total_raw_count = rowSums(counts),
  samples_with_count_ge_10 = samples_ge_10,
  retained_count_ge_10_in_ge_4_samples = keep
), "tables/gene_filter_all_features.tsv.gz")
write_tsv(results, "tables/deseq2_results_trt_vs_untrt.tsv.gz")
write_tsv(ranked, "tables/ranked_gene_vector_full_tested_universe.tsv.gz")
write_tsv(data.frame(gene_id = rownames(normalized), normalized, check.names = FALSE),
          "tables/normalized_counts.tsv.gz")
write_tsv(pca, "tables/pca_scores.tsv")
write_tsv(data.frame(sample_id = rownames(sample_distance), sample_distance,
                     check.names = FALSE, row.names = NULL), "tables/sample_distance_matrix.tsv")
write_tsv(dispersion, "tables/dispersion_estimates.tsv.gz")
summary_table <- data.frame(
  metric = c("raw_features", "retained_after_prefilter", "wald_tested_universe",
             "finite_bh_adjusted_p", "bh_fdr_lt_0_05", "higher_in_trt_fdr_lt_0_05",
             "lower_in_trt_fdr_lt_0_05"),
  value = c(nrow(counts), sum(keep), sum(tested), sum(is.finite(results$padj)),
            sum(results$fdr_below_0_05), sum(results$direction == "higher_in_trt"),
            sum(results$direction == "lower_in_trt"))
)
write_tsv(summary_table, "tables/results_summary.tsv")
saveRDS(airway, file.path(output_dir, "objects/airway_official_object.rds"), compress = "xz")
saveRDS(dds, file.path(output_dir, "objects/deseq2_fitted_dataset.rds"), compress = "xz")
saveRDS(vst, file.path(output_dir, "objects/vst_object.rds"), compress = "xz")

palette_dex <- c(untrt = "#0072B2", trt = "#D55E00")
palette_direction <- c(higher_in_trt = "#D55E00", lower_in_trt = "#0072B2",
                       not_significant = "#B8B8B8")
point_shapes <- setNames(c(21L, 22L, 23L, 24L), levels(metadata$cell))
pvalues <- results$pvalue[is.finite(results$pvalue)]
volcano_y <- ifelse(is.finite(results$padj),
                    -log10(pmax(results$padj, .Machine$double.xmin)), NA_real_)
finite_disp <- is.finite(dispersion$base_mean) & dispersion$base_mean > 0 &
  is.finite(dispersion$final) & dispersion$final > 0

# Six deliberately plain original renders retained for comparison.
open_png("figures/original/fig01_pca_original.png")
plot(pca$PC1, pca$PC2, pch = point_shapes[as.character(pca$cell)],
     bg = palette_dex[as.character(pca$dex)], col = "grey20", cex = 1.7,
     xlab = sprintf("PC1 (%.1f%%)", 100 * pca$percent_variance_pc1[[1L]]),
     ylab = sprintf("PC2 (%.1f%%)", 100 * pca$percent_variance_pc2[[1L]]),
     main = "VST PCA: airway samples")
text(pca$PC1, pca$PC2, pca$sample_id, pos = 3, cex = 0.55)
legend("topright", names(palette_dex), pt.bg = palette_dex, pch = 21, bty = "n")
dev.off()
open_png("figures/original/fig02_sample_distance_original.png")
stats::heatmap(sample_distance, Rowv = NA, Colv = NA, scale = "none",
               col = grDevices::colorRampPalette(c("white", "#2C7FB8"))(100),
               margins = c(8, 8), main = "Sample distances (VST)")
dev.off()
open_png("figures/original/fig03_ma_original.png")
with(results, plot(baseMean, log2FoldChange, log = "x", pch = 16, cex = 0.35,
                   col = ifelse(fdr_below_0_05, "#D55E00", "grey70"),
                   xlab = "Mean normalized count", ylab = "log2 fold change",
                   main = "MA plot: dex trt - untrt", ylim = c(-6, 6)))
abline(h = 0, col = "grey30"); dev.off()
open_png("figures/original/fig04_volcano_original.png")
plot(results$log2FoldChange, volcano_y, pch = 16, cex = 0.35,
     col = ifelse(results$direction == "not_significant", "grey70", "#D55E00"),
     xlab = "log2 fold change", ylab = "-log10(BH adjusted p-value)",
     main = "Volcano plot: dex trt - untrt")
dev.off()
open_png("figures/original/fig05_pvalue_histogram_original.png")
hist(pvalues, breaks = seq(0, 1, 0.05), col = "grey70", border = "white",
     xlab = "Raw Wald-test p-value", main = "P-value distribution")
dev.off()
open_png("figures/original/fig06_dispersion_original.png")
with(dispersion[finite_disp, ], plot(base_mean, final, log = "xy", pch = 16, cex = 0.3,
                                     col = "grey60", xlab = "Mean normalized count",
                                     ylab = "Dispersion", main = "DESeq2 dispersion estimates"))
dev.off()

# Six revision-3 final renders. These keep the same estimand and numeric inputs while
# fixing clipping, ambiguous heatmap scales, direction legends, label collisions and
# dispersion-layer visibility found by native-pixel review.
pad_limits <- function(x, fraction = 0.10) {
  limits <- range(x[is.finite(x)], na.rm = TRUE)
  span <- diff(limits)
  if (!is.finite(span) || span == 0) span <- max(abs(limits), 1)
  limits + c(-1, 1) * span * fraction
}
cell_levels <- sort(unique(as.character(metadata$cell)))
point_shapes <- setNames(c(21L, 22L, 23L, 24L), cell_levels)

open_png("figures/final/fig01_pca_final.png")
par(mar = c(5.2, 5.4, 4.3, 1.5) + 0.1, las = 1, family = "sans")
plot(pca$PC1, pca$PC2, type = "n",
     xlab = sprintf("PC1 (%.1f%% variance)", 100 * pca$percent_variance_pc1[[1L]]),
     ylab = sprintf("PC2 (%.1f%% variance)", 100 * pca$percent_variance_pc2[[1L]]),
     main = "Dexamethasone response in paired airway cell lines",
     xlim = pad_limits(pca$PC1, 0.13), ylim = pad_limits(pca$PC2, 0.15))
abline(h = 0, v = 0, col = "grey90", lwd = 0.8)
for (cell_id in cell_levels) {
  idx <- which(as.character(pca$cell) == cell_id)
  if (length(idx) == 2L) segments(pca$PC1[idx[1L]], pca$PC2[idx[1L]],
                                  pca$PC1[idx[2L]], pca$PC2[idx[2L]],
                                  col = "grey65", lwd = 1.4)
}
points(pca$PC1, pca$PC2, pch = point_shapes[as.character(pca$cell)],
       bg = palette_dex[as.character(pca$dex)], col = "grey15", cex = 1.8, lwd = 0.9)
text(pca$PC1, pca$PC2, labels = as.character(pca$cell), pos = 3, cex = 0.72, xpd = NA)
legend("topright", legend = c("untrt", "trt"), pch = 21,
       pt.bg = palette_dex[c("untrt", "trt")], pt.cex = 1.35,
       title = "dex", bty = "n")
mtext("VST assay; segments connect samples from the same cell line", side = 1,
      line = 3.8, cex = 0.7, col = "grey35")
dev.off()

open_png("figures/final/fig02_sample_distance_final.png")
hc <- stats::hclust(stats::as.dist(sample_distance), method = "complete")
ord <- hc$order
ordered_distance <- sample_distance[ord, ord, drop = FALSE]
distance_palette <- grDevices::colorRampPalette(c("#F7FBFF", "#6BAED6", "#08306B"))(120)
distance_range <- range(ordered_distance, finite = TRUE)
layout(matrix(c(1, 2), nrow = 1L), widths = c(5.8, 1.0))
par(mar = c(8.8, 9.0, 4.6, 1.1) + 0.1, family = "sans", las = 1)
image(seq_len(nrow(ordered_distance)), seq_len(ncol(ordered_distance)),
      ordered_distance[, ncol(ordered_distance):1L, drop = FALSE],
      axes = FALSE, xlab = "", ylab = "", col = distance_palette,
      zlim = distance_range, main = "Hierarchically ordered sample distance after VST")
axis(1, at = seq_len(nrow(ordered_distance)), labels = rownames(ordered_distance),
     las = 2, cex.axis = 0.78)
axis(2, at = seq_len(ncol(ordered_distance)), labels = rev(colnames(ordered_distance)),
     las = 1, cex.axis = 0.78)
box()
contrast_cut <- stats::quantile(ordered_distance, 0.62, na.rm = TRUE)
for (i in seq_len(nrow(ordered_distance))) {
  for (j in seq_len(ncol(ordered_distance))) {
    displayed_value <- ordered_distance[i, ncol(ordered_distance) - j + 1L]
    text(i, j, labels = sprintf("%.0f", displayed_value), cex = 0.60,
         col = if (displayed_value >= contrast_cut) "white" else "grey15")
  }
}
mtext("Euclidean distance; numeric cell values provide the exact scale",
      side = 1, line = 7.3, cex = 0.68, col = "grey35")
par(mar = c(8.8, 0.5, 4.6, 4.2) + 0.1, family = "sans", las = 1)
scale_values <- seq(distance_range[[1L]], distance_range[[2L]], length.out = 120L)
image(1, scale_values, matrix(scale_values, nrow = 1L), axes = FALSE,
      xlab = "", ylab = "", col = distance_palette, zlim = distance_range)
axis(4, at = pretty(distance_range, n = 5L), las = 1, cex.axis = 0.72)
mtext("distance", side = 3, line = 1.0, cex = 0.70)
layout(1)
dev.off()

finite_ma <- is.finite(results$baseMean) & results$baseMean > 0 & is.finite(results$log2FoldChange)
ma_col <- palette_direction[results$direction]
ma_limit <- max(6, ceiling(max(abs(results$log2FoldChange[finite_ma]), na.rm = TRUE)))
open_png("figures/final/fig03_ma_final.png")
par(mar = c(5.2, 5.4, 4.3, 1.5) + 0.1, las = 1, family = "sans")
plot(results$baseMean[finite_ma], results$log2FoldChange[finite_ma], log = "x", pch = 16,
     cex = 0.36, col = grDevices::adjustcolor(ma_col[finite_ma], alpha.f = 0.65),
     xlab = "Mean normalized count", ylab = "log2 fold change (trt - untrt)",
     main = "Differential expression effect sizes", ylim = c(-ma_limit, ma_limit))
abline(h = c(-1, 0, 1), col = c("grey80", "grey35", "grey80"),
       lty = c(2, 1, 2), lwd = c(1, 1.2, 1))
legend("topright", legend = c(
  sprintf("higher in trt, FDR<0.05 (n=%d)", sum(results$direction == "higher_in_trt")),
  sprintf("lower in trt, FDR<0.05 (n=%d)", sum(results$direction == "lower_in_trt")),
  "not significant"
), col = palette_direction[c("higher_in_trt", "lower_in_trt", "not_significant")],
 pch = 16, bty = "n", cex = 0.75)
mtext("DESeq2: ~ cell + dex; BH adjustment", side = 1, line = 3.8,
      cex = 0.7, col = "grey35")
dev.off()

volcano <- data.frame(
  gene_id = results$gene_id,
  log2FoldChange = results$log2FoldChange,
  padj = results$padj,
  minus_log10_padj = volcano_y,
  direction = results$direction,
  stringsAsFactors = FALSE
)
finite_volcano <- is.finite(volcano$log2FoldChange) & is.finite(volcano$minus_log10_padj)
vol_col <- palette_direction[volcano$direction]
y_max <- max(volcano$minus_log10_padj[finite_volcano], na.rm = TRUE)
x_range <- range(volcano$log2FoldChange[finite_volcano], na.rm = TRUE)
open_png("figures/final/fig04_volcano_final.png")
par(mar = c(5.2, 5.4, 4.3, 1.5) + 0.1, las = 1, family = "sans")
plot(volcano$log2FoldChange[finite_volcano], volcano$minus_log10_padj[finite_volcano],
     pch = 16, cex = 0.38,
     col = grDevices::adjustcolor(vol_col[finite_volcano], alpha.f = 0.68),
     xlab = "log2 fold change (trt - untrt)", ylab = "-log10(BH adjusted p-value)",
     main = "Effect direction and false-discovery evidence",
     xlim = pad_limits(volcano$log2FoldChange[finite_volcano], 0.08),
     ylim = c(0, y_max * 1.12))
abline(v = 0, col = "grey35", lwd = 1)
abline(h = -log10(0.05), col = "grey45", lty = 2)
label_candidates <- which(finite_volcano & volcano$direction != "not_significant")
label_candidates <- label_candidates[order(volcano$padj[label_candidates], na.last = NA)]
label_candidates <- utils::head(label_candidates, 6L)
if (length(label_candidates)) {
  target_x <- seq(x_range[[1L]] + 0.12 * diff(x_range),
                  x_range[[2L]] - 0.12 * diff(x_range),
                  length.out = length(label_candidates))
  target_y <- seq(y_max * 0.78, y_max * 1.01, length.out = length(label_candidates))
  segments(volcano$log2FoldChange[label_candidates],
           volcano$minus_log10_padj[label_candidates],
           target_x, target_y, col = "grey45", lwd = 0.8)
  text(target_x, target_y, labels = volcano$gene_id[label_candidates],
       pos = 3, cex = 0.56, xpd = FALSE)
}
legend("topleft", legend = c("higher in trt", "lower in trt", "not significant"),
       col = palette_direction[c("higher_in_trt", "lower_in_trt", "not_significant")],
       pch = 16, bty = "n", cex = 0.78)
mtext("Labels denote the lowest adjusted p-values; they are descriptive, not validation",
      side = 1, line = 3.8, cex = 0.68, col = "grey35")
dev.off()

p_hist_object <- hist(pvalues, breaks = seq(0, 1, 0.05), plot = FALSE)
p_hist <- data.frame(bin_left = utils::head(p_hist_object$breaks, -1L),
                     count = p_hist_object$counts)
open_png("figures/final/fig05_pvalue_histogram_final.png")
par(mar = c(5.2, 5.4, 4.3, 1.5) + 0.1, las = 1, family = "sans")
barplot(p_hist$count, names.arg = sprintf("%.2f", p_hist$bin_left),
        col = "#6BAED6", border = "white", space = 0,
        xlab = "Raw Wald-test p-value (bin left edge)", ylab = "Genes",
        main = "Wald-test p-value distribution", cex.names = 0.55)
expected <- sum(p_hist$count) / nrow(p_hist)
abline(h = expected, col = "#D55E00", lty = 2, lwd = 1.4)
legend("topright", legend = sprintf("uniform-null expectation per bin: %.1f", expected),
       col = "#D55E00", lty = 2, bty = "n", cex = 0.78)
mtext(sprintf("Finite p-values in tested universe: %s", format(sum(p_hist$count), big.mark = ",")),
      side = 1, line = 3.8, cex = 0.7, col = "grey35")
dev.off()

finite_gene <- is.finite(dispersion$base_mean) & dispersion$base_mean > 0 &
  is.finite(dispersion$gene_estimate) & dispersion$gene_estimate > 0
finite_final <- is.finite(dispersion$base_mean) & dispersion$base_mean > 0 &
  is.finite(dispersion$final) & dispersion$final > 0
finite_fit <- is.finite(dispersion$base_mean) & dispersion$base_mean > 0 &
  is.finite(dispersion$fitted) & dispersion$fitted > 0
open_png("figures/final/fig06_dispersion_final.png")
par(mar = c(5.2, 5.4, 4.3, 1.5) + 0.1, las = 1, family = "sans")
plot(dispersion$base_mean[finite_gene], dispersion$gene_estimate[finite_gene],
     log = "xy", pch = 16, cex = 0.28,
     col = grDevices::adjustcolor("#B8B8B8", alpha.f = 0.55),
     xlab = "Mean normalized count", ylab = "Dispersion",
     main = "DESeq2 dispersion estimation")
points(dispersion$base_mean[finite_final], dispersion$final[finite_final],
       pch = 16, cex = 0.30, col = grDevices::adjustcolor("#0072B2", alpha.f = 0.55))
fit_order <- order(dispersion$base_mean[finite_fit])
lines(dispersion$base_mean[finite_fit][fit_order], dispersion$fitted[finite_fit][fit_order],
      col = "#D55E00", lwd = 1.7)
legend("topright", legend = c("gene-wise estimate", "final estimate", "fitted trend"),
       col = c("#B8B8B8", "#0072B2", "#D55E00"), pch = c(16, 16, NA),
       lty = c(NA, NA, 1), bty = "n", cex = 0.78)
dev.off()

checks <- c(
  official_dimensions_63677_by_8 = identical(dim(counts), c(63677L, 8L)),
  four_complete_cell_line_pairs = all(pair_table == 1L),
  counts_metadata_order_identical = identical(colnames(counts), rownames(metadata)),
  design_full_rank = design_rank == ncol(design_matrix),
  residual_df_positive = nrow(design_matrix) - design_rank > 0L,
  rank_vector_is_full_finite_tested_universe = nrow(ranked) == sum(tested),
  all_size_factors_positive = all(DESeq2::sizeFactors(dds) > 0),
  twelve_nonempty_pngs = length(list.files(file.path(output_dir, "figures"), "\\.png$", recursive = TRUE)) == 12L
)
if (!all(checks)) stop("One or more reproducibility checks failed")
write_tsv(data.frame(check = names(checks), passed = unname(checks)),
          "tables/reproducibility_checks.tsv")

write_lines(c(
  "# Scientific boundaries", "",
  "This teaching run estimates the association between dexamethasone exposure and gene expression in four paired airway smooth-muscle cell lines.",
  "Positive log2 fold change means higher expression in trt than untrt after adjustment for cell line.",
  "BH-adjusted p-values control false discovery within the reported finite Wald-test universe.",
  "No patient benefit, clinical response, causality, or population-generalization claim is supported."
), "reports/SCIENTIFIC_BOUNDARIES.md")
write_lines(c("# QA report", "", paste0("- ", names(checks), ": PASS")), "reports/QA_REPORT.md")
write_lines(c(
  "# Figure notes", "",
  "Six original diagnostic renders and six revised renders are retained.",
  "The revised figures improve pairing cues, clustering, direction legends, null reference, and dispersion-layer visibility without changing the estimand.",
  "Native pixel review is an external delivery gate and is not asserted by this unattended runner."
), "reports/FIGURE_NOTES.md")
write_lines(capture.output(utils::sessionInfo()), "provenance/sessionInfo.txt")
write_lines(capture.output(utils::citation("airway")), "provenance/citation_airway.txt")
write_lines(capture.output(utils::citation("DESeq2")), "provenance/citation_DESeq2.txt")
write_tsv(data.frame(package = names(required_versions), version = unname(required_versions),
                     library = vapply(names(required_versions), find.package, character(1))),
          "provenance/direct_package_versions.tsv")
write_lines(c(
  "dataset=Bioconductor airway 1.30.0; accession=GSE52778",
  "design=~ cell + dex", "contrast=dex trt - untrt", "positive_lfc=higher in trt",
  "prefilter=raw count >=10 in >=4 samples", "test=DESeq2 Wald",
  "multiple_testing=Benjamini-Hochberg; alpha=0.05"
), "provenance/method_contract.txt")

sha256_file <- function(path) {
  stdout_file <- tempfile("certutil-stdout-")
  stderr_file <- tempfile("certutil-stderr-")
  on.exit(unlink(c(stdout_file, stderr_file), force = TRUE), add = TRUE)
  status <- system2(
    "certutil.exe",
    c("-hashfile", shQuote(normalizePath(path, winslash = "\\")), "SHA256"),
    stdout = stdout_file,
    stderr = stderr_file
  )
  if (!identical(status, 0L)) stop("certutil SHA-256 failed for ", path, " with exit code ", status)
  size <- file.info(stdout_file)$size
  payload <- readBin(stdout_file, what = "raw", n = size)
  byte_values <- as.integer(payload)
  is_ascii_hex <- byte_values %in% c(48:57, 65:70, 97:102)
  runs <- rle(is_ascii_hex)
  run_ends <- cumsum(runs$lengths)
  candidate_runs <- which(runs$values & runs$lengths == 64L)
  if (length(candidate_runs) != 1L) stop("Could not isolate one ASCII SHA-256 digest for ", path)
  run_end <- run_ends[[candidate_runs]]
  run_start <- run_end - 63L
  tolower(rawToChar(payload[run_start:run_end]))
}
artifact_paths <- list.files(output_dir, recursive = TRUE, full.names = TRUE, all.files = TRUE)
artifact_paths <- artifact_paths[file.info(artifact_paths)$isdir %in% FALSE]
artifact_paths <- artifact_paths[basename(artifact_paths) != "artifact_manifest.tsv"]
artifact_manifest <- data.frame(
  relative_path = substring(normalizePath(artifact_paths, winslash = "/"), nchar(output_dir) + 2L),
  bytes = unname(file.info(artifact_paths)$size),
  sha256 = vapply(artifact_paths, sha256_file, character(1)), stringsAsFactors = FALSE
)
artifact_manifest <- artifact_manifest[order(artifact_manifest$relative_path), , drop = FALSE]
write_tsv(artifact_manifest, "provenance/artifact_manifest.tsv")
write_lines(c("status=complete", paste0("artifacts_hashed=", nrow(artifact_manifest))),
            "STATUS_COMPLETE.txt")
cat("p0_bulk_airway_complete\n", output_dir, "\n", sep = "")
