# R matrix and tximport workflows

## Contents

1. Shared input preflight
2. Salmon plus tximport and DESeq2
3. Integer matrix plus DESeq2
4. edgeR quasi-likelihood
5. limma-voom
6. Output and interpretation rules

These recipes declare packages but never install them. Execute them only in a locked task environment after the environment manager verifies package availability and versions.

## 1. Shared input preflight

Use one metadata row per independent specimen. Keep sample identifiers as strings and align explicitly.

```r
required <- c("readr", "tibble")
missing <- required[!vapply(required, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing)) stop("Missing declared packages: ", paste(missing, collapse = ", "))

metadata <- readr::read_tsv("sample_metadata.tsv", show_col_types = FALSE)
stopifnot(all(c("sample_id", "condition") %in% names(metadata)))
if (anyDuplicated(metadata$sample_id)) stop("sample_id must be unique")
metadata$condition <- factor(metadata$condition)

assert_matrix_contract <- function(counts, metadata) {
  if (is.null(rownames(counts)) || is.null(colnames(counts))) stop("Matrix needs gene and sample names")
  if (anyDuplicated(rownames(counts)) || anyDuplicated(colnames(counts))) stop("Duplicated gene/sample identifiers")
  if (any(!is.finite(counts)) || any(counts < 0)) stop("Counts must be finite and non-negative")
  if (!setequal(colnames(counts), metadata$sample_id)) stop("Matrix columns and metadata samples differ")
  counts[, metadata$sample_id, drop = FALSE]
}

assert_full_rank <- function(formula, metadata) {
  mm <- model.matrix(formula, metadata)
  if (qr(mm)$rank < ncol(mm)) stop("Design is not full rank; inspect confounding")
  invisible(mm)
}
```

Do not use the replication check as a mechanical universal threshold. Investigate paired, continuous, and repeated-measure designs explicitly; otherwise require at least three independent specimens per categorical group for inferential reporting.

## 2. Salmon plus tximport and DESeq2

Keep transcript-to-gene mapping tied to the same annotation used to build the Salmon index. Do not construct DESeq2 input from TPM.

```r
required <- c("tximport", "DESeq2", "readr")
missing <- required[!vapply(required, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing)) stop("Missing declared packages: ", paste(missing, collapse = ", "))

tx2gene <- readr::read_tsv("tx2gene.tsv", col_names = c("TXNAME", "GENEID"), show_col_types = FALSE)
files <- file.path("quant", metadata$sample_id, "quant.sf")
names(files) <- metadata$sample_id
if (any(!file.exists(files))) stop("Missing quant.sf: ", paste(files[!file.exists(files)], collapse = ", "))

txi <- tximport::tximport(
  files,
  type = "salmon",
  tx2gene = tx2gene,
  countsFromAbundance = "no"
)

design_formula <- ~ batch + condition  # remove batch only when it is absent and not needed
assert_full_rank(design_formula, metadata)
dds <- DESeq2::DESeqDataSetFromTximport(txi, colData = as.data.frame(metadata), design = design_formula)
keep <- rowSums(DESeq2::counts(dds) >= 10) >= max(2L, min(table(metadata$condition)))
dds <- dds[keep, ]
dds <- DESeq2::DESeq(dds)

# Replace levels with the prespecified contrast; do not infer direction from filenames.
res <- DESeq2::results(dds, contrast = c("condition", "treated", "control"), alpha = 0.05)
res_df <- tibble::rownames_to_column(as.data.frame(res), "feature_id")
readr::write_tsv(res_df, "de_results.tsv", na = "NA")
readr::write_tsv(
  tibble::rownames_to_column(as.data.frame(DESeq2::counts(dds, normalized = TRUE)), "feature_id"),
  "normalized_counts.tsv"
)
saveRDS(dds, "deseq2_dataset.rds")
```

Use `countsFromAbundance="no"` with `DESeqDataSetFromTximport` so DESeq2 uses tximport's abundance/length information. A length-scaled-count matrix is a distinct compatible variant and must be recorded as such.

## 3. Integer matrix plus DESeq2

```r
required <- c("DESeq2", "readr", "tibble")
missing <- required[!vapply(required, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing)) stop("Missing declared packages: ", paste(missing, collapse = ", "))

count_df <- readr::read_tsv("gene_counts.tsv", show_col_types = FALSE)
feature_id <- count_df[[1]]
counts <- as.matrix(count_df[-1])
rownames(counts) <- feature_id
storage.mode(counts) <- "numeric"
counts <- assert_matrix_contract(counts, metadata)
if (any(abs(counts - round(counts)) > 1e-8)) stop("This DESeq2 matrix route requires integer counts")
storage.mode(counts) <- "integer"

design_formula <- ~ batch + condition
assert_full_rank(design_formula, metadata)
dds <- DESeq2::DESeqDataSetFromMatrix(countData = counts, colData = as.data.frame(metadata), design = design_formula)
keep <- rowSums(DESeq2::counts(dds) >= 10) >= max(2L, min(table(metadata$condition)))
dds <- DESeq2::DESeq(dds[keep, ])
res <- DESeq2::results(dds, contrast = c("condition", "treated", "control"), alpha = 0.05)
readr::write_tsv(tibble::rownames_to_column(as.data.frame(res), "feature_id"), "de_results.tsv", na = "NA")
saveRDS(dds, "deseq2_dataset.rds")
```

## 4. edgeR quasi-likelihood

```r
required <- c("edgeR", "readr", "tibble")
missing <- required[!vapply(required, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing)) stop("Missing declared packages: ", paste(missing, collapse = ", "))

design <- model.matrix(~ batch + condition, metadata)
if (qr(design)$rank < ncol(design)) stop("Design is not full rank")
y <- edgeR::DGEList(counts = counts, samples = as.data.frame(metadata))
keep <- edgeR::filterByExpr(y, design = design)
y <- edgeR::calcNormFactors(y[keep, , keep.lib.sizes = FALSE], method = "TMM")
y <- edgeR::estimateDisp(y, design, robust = TRUE)
fit <- edgeR::glmQLFit(y, design, robust = TRUE)

# Verify the coefficient name or replace with an explicit contrast.
coef_name <- "conditiontreated"
if (!coef_name %in% colnames(design)) stop("Requested coefficient not found: ", coef_name)
qlf <- edgeR::glmQLFTest(fit, coef = which(colnames(design) == coef_name))
tab <- edgeR::topTags(qlf, n = Inf, sort.by = "none")$table
tab$feature_id <- rownames(tab)
readr::write_tsv(tab[, c("feature_id", setdiff(names(tab), "feature_id"))], "de_results.tsv")
saveRDS(list(dge = y, fit = fit, test = qlf), "edger_ql.rds")
```

## 5. limma-voom

```r
required <- c("edgeR", "limma", "readr")
missing <- required[!vapply(required, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing)) stop("Missing declared packages: ", paste(missing, collapse = ", "))

design <- model.matrix(~ batch + condition, metadata)
if (qr(design)$rank < ncol(design)) stop("Design is not full rank")
y <- edgeR::DGEList(counts = counts)
keep <- edgeR::filterByExpr(y, design = design)
y <- edgeR::calcNormFactors(y[keep, , keep.lib.sizes = FALSE])

pdf("voom_mean_variance.pdf")
v <- limma::voom(y, design, plot = TRUE)
dev.off()
fit <- limma::lmFit(v, design)
fit <- limma::eBayes(fit, robust = TRUE)
coef_name <- "conditiontreated"
if (!coef_name %in% colnames(design)) stop("Requested coefficient not found: ", coef_name)
tab <- limma::topTable(fit, coef = coef_name, number = Inf, sort.by = "none")
tab$feature_id <- rownames(tab)
readr::write_tsv(tab[, c("feature_id", setdiff(names(tab), "feature_id"))], "de_results.tsv")
saveRDS(list(voom = v, fit = fit), "limma_voom.rds")
```

For repeated specimens, build a subject-aware model and use an appropriate correlation/mixed strategy; do not paste this independent-sample design unchanged.

## 6. Output and interpretation rules

- Preserve the sign convention: state exactly which numerator/reference defines positive log fold change.
- Report effect estimates with uncertainty and FDR; a volcano plot is not the result table.
- Use independent filtering/`filterByExpr` before testing and report the tested feature universe.
- Inspect normalized sample structure before interpreting genes. Investigate outliers; never delete them only because they weaken significance.
- For GSEA rank the complete tested list by a signed statistic. For ORA use the tested features as background.
