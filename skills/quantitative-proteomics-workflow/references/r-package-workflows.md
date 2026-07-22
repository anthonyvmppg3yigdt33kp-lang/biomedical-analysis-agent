# R package workflows for quantitative proteomics

## Contents

1. Shared package preflight
2. QFeatures multilevel aggregation
3. DEP protein-matrix workflow
4. Direct limma model
5. MSstats feature/run workflow
6. tidyproteomics package-native variant
7. Sensitivity and figures

All examples assume a locked task environment. They declare missing dependencies and stop; they never install packages.

## 1. Shared package preflight

```r
assert_packages <- function(pkgs) {
  missing <- pkgs[!vapply(pkgs, requireNamespace, logical(1), quietly = TRUE)]
  if (length(missing)) stop("Missing declared packages: ", paste(missing, collapse = ", "))
}

metadata <- read.delim("sample_metadata.tsv", check.names = FALSE, stringsAsFactors = FALSE)
stopifnot(all(c("sample_id", "participant_id", "condition") %in% names(metadata)))
if (anyDuplicated(metadata$sample_id)) stop("sample_id must be unique")
metadata$condition <- factor(metadata$condition)
```

## 2. QFeatures multilevel aggregation

Use this route when the input contains PSM/peptide features and a protein mapping. Adapt column names explicitly; never guess which columns are quantitative.

```r
assert_packages(c("QFeatures", "SummarizedExperiment", "S4Vectors", "MsCoreUtils"))
psm <- read.delim("psm_or_peptide_table.tsv", check.names = FALSE, stringsAsFactors = FALSE)
quant_cols <- metadata$sample_id
required_cols <- c("feature_id", "peptide_sequence", "protein_group", quant_cols)
if (!all(required_cols %in% names(psm))) stop("Missing QFeatures input columns")
if (anyDuplicated(psm$feature_id)) stop("feature_id must be unique before aggregation")

intensity <- as.matrix(psm[, quant_cols, drop = FALSE])
storage.mode(intensity) <- "numeric"
rownames(intensity) <- psm$feature_id
if (any(intensity < 0, na.rm = TRUE)) stop("Intensities cannot be negative")

feature_data <- S4Vectors::DataFrame(
  feature_id = psm$feature_id,
  peptide_sequence = psm$peptide_sequence,
  protein_group = psm$protein_group,
  row.names = psm$feature_id
)
sample_data <- S4Vectors::DataFrame(metadata, row.names = metadata$sample_id)
se <- SummarizedExperiment::SummarizedExperiment(
  assays = list(intensity = intensity), rowData = feature_data, colData = sample_data
)
qf <- QFeatures::QFeatures(list(peptide = se))
qf <- QFeatures::aggregateFeatures(
  qf, i = "peptide", fcol = "protein_group", name = "protein",
  fun = MsCoreUtils::robustSummary
)
saveRDS(qf, "qfeatures_aggregated.rds")
write.table(
  SummarizedExperiment::assay(qf[["protein"]], "intensity"),
  "protein_matrix.tsv", sep = "\t", quote = FALSE, col.names = NA
)
```

Inspect assay links and aggregation diagnostics. Shared peptides and ambiguous protein groups require a predeclared rule; `robustSummary` does not resolve biological ambiguity by itself.

## 3. DEP protein-matrix workflow

DEP expects a protein table, quantitative-column indices, and experimental design. Keep filtering, normalization, imputation sensitivity, differential testing, and rejection calls together.

```r
assert_packages(c("DEP", "SummarizedExperiment"))
protein <- read.delim("protein_table.tsv", check.names = FALSE, stringsAsFactors = FALSE)
stopifnot(all(c("protein_id", "gene_name", metadata$sample_id) %in% names(protein)))
protein_unique <- DEP::make_unique(protein, "gene_name", "protein_id", delim = ";")
quant_cols <- match(metadata$sample_id, names(protein_unique))
if (anyNA(quant_cols)) stop("Quantitative columns do not match sample metadata")

experimental_design <- data.frame(
  label = metadata$sample_id,
  condition = metadata$condition,
  replicate = ave(seq_len(nrow(metadata)), metadata$condition, FUN = seq_along),
  stringsAsFactors = FALSE
)
se <- DEP::make_se(protein_unique, quant_cols, experimental_design)
se_filtered <- DEP::filter_missval(se, thr = 0)
se_normalized <- DEP::normalize_vsn(se_filtered)
saveRDS(se_normalized, "dep_normalized_no_imputation.rds")

# MinProb is a documented left-censored/MNAR-like sensitivity variant.
se_mnar <- DEP::impute(se_normalized, fun = "MinProb", q = 0.01)
saveRDS(se_mnar, "dep_normalized_minprob.rds")
diff <- DEP::test_diff(se_mnar, type = "all")
dep <- DEP::add_rejections(diff, alpha = 0.05, lfc = 1)
saveRDS(dep, "dep_results.rds")
```

For paired/complex designs, inspect the DEP version's design support or use direct limma with the correct design instead of forcing this simple template.

## 4. Direct limma model

Use verified log2 protein intensities. Missing values remain `NA`; proteins with condition-specific absence still require sensitivity interpretation.

```r
assert_packages(c("limma"))
protein_log2 <- as.matrix(read.delim("protein_log2_matrix.tsv", row.names = 1, check.names = FALSE))
protein_log2 <- protein_log2[, metadata$sample_id, drop = FALSE]
design <- model.matrix(~ batch + condition, metadata)
if (qr(design)$rank < ncol(design)) stop("Design is not full rank")
fit <- limma::eBayes(limma::lmFit(protein_log2, design), trend = TRUE, robust = TRUE)
coef_name <- "conditiontreated"
if (!coef_name %in% colnames(design)) stop("Requested coefficient missing: ", coef_name)
results <- limma::topTable(fit, coef = coef_name, number = Inf, sort.by = "none")
results$protein_id <- rownames(results)
write.table(results, "protein_results.tsv", sep = "\t", quote = FALSE, row.names = FALSE)
saveRDS(fit, "limma_protein_fit.rds")
```

Replace `batch` and coefficient levels with the prespecified design. For pairing/repeated measures, add participant effects or a justified correlation/mixed approach.

## 5. MSstats feature/run workflow

Use a converter that matches the upstream search tool, or provide an MSstats-compatible long table. Verify required columns against the locked MSstats version.

```r
assert_packages(c("MSstats"))
ms <- read.csv("msstats_input.csv", stringsAsFactors = FALSE, check.names = FALSE)
required <- c("ProteinName", "PeptideSequence", "Condition", "BioReplicate", "Run", "Intensity")
if (!all(required %in% names(ms))) stop("Missing MSstats columns: ", paste(setdiff(required, names(ms)), collapse = ", "))
processed <- MSstats::dataProcess(
  raw = ms, normalization = "equalizeMedians", summaryMethod = "TMP",
  censoredInt = "NA", MBimpute = FALSE
)
conditions <- levels(factor(ms$Condition))
if (!all(c("control", "treated") %in% conditions)) stop("Define the prespecified MSstats contrast levels")
contrast <- matrix(c(-1, 1), nrow = 1, dimnames = list("treated-control", c("control", "treated")))
comparison <- MSstats::groupComparison(contrast.matrix = contrast, data = processed)
write.table(comparison$ComparisonResult, "protein_results.tsv", sep = "\t", quote = FALSE, row.names = FALSE)
saveRDS(list(processed = processed, comparison = comparison), "msstats_results.rds")
```

Set normalization, summary, censoring, and imputation from the acquisition design and current API. Compare defensible alternatives and record them.

## 6. tidyproteomics package-native variant

Use only with a verified tidyproteomics object and API. Preserve the full operation-history chain:

```r
assert_packages(c("tidyproteomics"))
obj <- readRDS("tidyproteomics_object.rds")
summary_before <- summary(obj, by = "sample")
qc_counts <- tidyproteomics::plot_counts(obj)
qc_rank <- tidyproteomics::plot_quantrank(obj)
filtered <- subset(obj, num_unique_peptides > 1)
normalized <- tidyproteomics::normalize(filtered, .method = "median")
saveRDS(normalized, "tidyproteomics_normalized.rds")
history <- tidyproteomics::operations(normalized)
saveRDS(list(summary = summary_before, operations = history), "tidyproteomics_audit.rds")
```

Function exports and object structure may differ by version. Verify `packageVersion`, `getNamespaceExports("tidyproteomics")`, signatures, and object class before execution. Imputation is an optional separate variant. Without the exact source object, retain this branch as `parse-verified`, not data-verified.

## 7. Sensitivity and figures

Compare primary no-imputation/model-native analysis with justified imputation, alternative evidence filters, normalization diagnostics, effects with/without influential samples, and protein-group/minimum-peptide rules. Never silently delete an influential sample.

Generate identification/missingness summaries, intensity distributions, CV, correlation/PCA, normalization diagnostics, volcano/MA, heatmap, and selected protein profiles. State the unit, scale, missingness/imputation state, statistic, and conclusion boundary for every figure.
