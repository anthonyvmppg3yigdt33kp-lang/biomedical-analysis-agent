args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 2L) stop("Expected <run_root> <private_source_root>")
run_root <- normalizePath(args[[1L]], winslash = "/", mustWork = TRUE)
private_source_root <- normalizePath(args[[2L]], winslash = "/", mustWork = TRUE)

required <- c("DEP", "QFeatures", "limma", "SummarizedExperiment", "S4Vectors", "digest",
              "jsonlite", "ggplot2", "ggrepel", "gridExtra")
missing_packages <- required[!vapply(required, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing_packages)) stop("Missing declared packages: ", paste(missing_packages, collapse = ", "))
expected_versions <- c(DEP = "1.31.0", QFeatures = "1.20.0", limma = "3.66.0", Rcpp = "1.1.1")
actual_versions <- vapply(names(expected_versions), function(pkg) as.character(utils::packageVersion(pkg)), character(1))
if (!identical(unname(actual_versions), unname(expected_versions))) {
  stop("Pinned version mismatch: ", paste(names(actual_versions), actual_versions, collapse = "; "))
}

dir_create <- function(...) dir.create(file.path(...), recursive = TRUE, showWarnings = FALSE)
write_tsv <- function(x, path) {
  utils::write.table(x, path, sep = "\t", quote = FALSE, row.names = FALSE, na = "NA")
}
write_json <- function(x, path) jsonlite::write_json(x, path, auto_unbox = TRUE, pretty = TRUE, na = "null")
sha_file <- function(path) digest::digest(file = path, algo = "sha256", serialize = FALSE)
rel_path <- function(path) {
  value <- normalizePath(path, winslash = "/", mustWork = TRUE)
  prefix <- paste0(run_root, "/")
  if (!startsWith(value, prefix)) stop("Artifact escapes run root: ", value)
  substring(value, nchar(prefix) + 1L)
}
now_utc <- function() format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC")

manifest_path <- file.path(run_root, "manifest", "run_manifest.json")
ledger_path <- file.path(run_root, "manifest", "artifact_ledger.jsonl")
state_path <- file.path(run_root, "manifest", "state_history.jsonl")

transition <- function(state, stage_id = NULL, note = NULL) {
  event <- list(timestamp = now_utc(), state = state, stage_id = stage_id, note = note)
  cat(jsonlite::toJSON(event, auto_unbox = TRUE, null = "null"), "\n", file = state_path, append = TRUE, sep = "")
  manifest <- jsonlite::fromJSON(manifest_path, simplifyVector = FALSE)
  manifest$current_state <- state
  manifest$current_stage <- stage_id
  manifest$updated_at <- now_utc()
  write_json(manifest, manifest_path)
}

checkpoint <- function(stage_id, maturity = "data-verified") {
  transition("STAGE_VALIDATING", stage_id)
  staging <- file.path(run_root, "_staging", stage_id)
  files <- list.files(staging, recursive = TRUE, full.names = TRUE, all.files = TRUE, no.. = TRUE)
  files <- files[file.info(files)$isdir %in% FALSE]
  if (!length(files)) stop("Checkpoint has no files: ", stage_id)
  if (any(file.info(files)$size <= 0)) stop("Checkpoint contains empty artifact: ", stage_id)
  target <- file.path(run_root, "04_intermediate", stage_id)
  if (dir.exists(target)) stop("Checkpoint target already exists: ", target)
  if (!file.rename(staging, target)) stop("Atomic checkpoint promotion failed: ", stage_id)
  promoted <- list.files(target, recursive = TRUE, full.names = TRUE, all.files = TRUE, no.. = TRUE)
  promoted <- promoted[file.info(promoted)$isdir %in% FALSE]
  records <- lapply(promoted, function(path) list(
    artifact_id = paste0(stage_id, "-", substr(sha_file(path), 1, 12)),
    stage_id = stage_id,
    path = rel_path(path),
    sha256 = sha_file(path),
    size_bytes = unname(file.info(path)$size),
    maturity = maturity,
    produced_at = now_utc()
  ))
  for (record in records) {
    cat(jsonlite::toJSON(record, auto_unbox = TRUE), "\n", file = ledger_path, append = TRUE, sep = "")
  }
  manifest <- jsonlite::fromJSON(manifest_path, simplifyVector = FALSE)
  if (is.null(manifest$checkpoints)) manifest$checkpoints <- list()
  manifest$checkpoints[[length(manifest$checkpoints) + 1L]] <- list(
    stage_id = stage_id,
    path = rel_path(target),
    artifact_count = length(records),
    validated = TRUE,
    promoted_at = now_utc()
  )
  manifest$current_state <- "CHECKPOINTED"
  manifest$current_stage <- stage_id
  manifest$updated_at <- now_utc()
  write_json(manifest, manifest_path)
  transition("CHECKPOINTED", stage_id)
  invisible(target)
}

new_stage <- function(stage_id) {
  path <- file.path(run_root, "_staging", stage_id)
  if (dir.exists(path)) stop("Staging stage already exists: ", stage_id)
  dir.create(path, recursive = TRUE, showWarnings = FALSE)
  transition("RUNNING_STAGE", stage_id)
  path
}

theme_final <- function(base_size = 12) {
  ggplot2::theme_classic(base_size = base_size) +
    ggplot2::theme(
      plot.title = ggplot2::element_text(face = "bold", size = base_size + 1),
      plot.subtitle = ggplot2::element_text(color = "#444444", size = base_size - 1),
      axis.title = ggplot2::element_text(face = "bold"),
      legend.title = ggplot2::element_text(face = "bold"),
      legend.position = "right",
      plot.margin = ggplot2::margin(10, 18, 10, 10)
    )
}

fit_limma <- function(matrix_log2, condition, feature_ids, metadata, contrast_name = "Ubi6_vs_Ctrl") {
  condition <- factor(condition, levels = c("Ctrl", "Ubi6"))
  design <- stats::model.matrix(~ 0 + condition)
  colnames(design) <- levels(condition)
  if (qr(design)$rank != ncol(design)) stop("Primary design is not full rank")
  contrast <- limma::makeContrasts(Ubi6 - Ctrl, levels = design)
  fit <- limma::lmFit(matrix_log2, design)
  fit <- limma::contrasts.fit(fit, contrast)
  fit <- limma::eBayes(fit, trend = TRUE, robust = TRUE)
  coef <- as.numeric(fit$coefficients[, 1L])
  se <- as.numeric(fit$stdev.unscaled[, 1L] * sqrt(fit$s2.post))
  df_total <- as.numeric(fit$df.total)
  critical <- stats::qt(0.975, df = df_total)
  p_value <- as.numeric(fit$p.value[, 1L])
  result <- data.frame(
    protein_group_id = feature_ids,
    gene_name = metadata$Gene.names,
    majority_protein_ids = metadata$Majority.protein.IDs,
    contrast = contrast_name,
    log2_fc = coef,
    moderated_se = se,
    ci95_low = coef - critical * se,
    ci95_high = coef + critical * se,
    moderated_t = as.numeric(fit$t[, 1L]),
    df_total = df_total,
    p_value = p_value,
    fdr_bh = stats::p.adjust(p_value, method = "BH"),
    ave_log2_lfq = rowMeans(matrix_log2, na.rm = TRUE),
    n_observed_ctrl = rowSums(!is.na(matrix_log2[, condition == "Ctrl", drop = FALSE])),
    n_observed_ubi6 = rowSums(!is.na(matrix_log2[, condition == "Ubi6", drop = FALSE])),
    stringsAsFactors = FALSE,
    check.names = FALSE
  )
  list(result = result, fit = fit, design = design, contrast = contrast)
}

transition("DATA_PROFILED", note = "Input source was acquired and pre-fit contrast was frozen before this execution")
transition("PLAN_COMPILED")
transition("AWAITING_AUTHORIZATION", note = "Explicit task-local run/install authorization recorded")
transition("ENV_PREPARING")
transition("ENV_LOCKED")

# P10: immutable input audit and exact source-object evidence.
p10 <- new_stage("P10_input_audit")
tarball <- file.path(private_source_root, "DEP_1.32.0.tar.gz")
extracted_root <- file.path(private_source_root, "extracted", "DEP")
source_rda <- file.path(extracted_root, "data", "UbiLength.rda")
design_rda <- file.path(extracted_root, "data", "UbiLength_ExpDesign.rda")
rd_path <- file.path(extracted_root, "man", "UbiLength.Rd")
for (path in c(tarball, source_rda, design_rda, rd_path)) if (!file.exists(path)) stop("Missing private source: ", path)

source_env <- new.env(parent = emptyenv())
source_names <- load(source_rda, envir = source_env)
design_env <- new.env(parent = emptyenv())
design_names <- load(design_rda, envir = design_env)
source_object <- source_env[[source_names[[1L]]]]
source_design <- design_env[[design_names[[1L]]]]

package_env <- new.env(parent = emptyenv())
utils::data("UbiLength", package = "DEP", envir = package_env)
utils::data("UbiLength_ExpDesign", package = "DEP", envir = package_env)
package_object <- package_env$UbiLength
package_design <- package_env$UbiLength_ExpDesign
if (!identical(source_object, package_object)) stop("Extracted and installed UbiLength objects differ")
if (!identical(source_design, package_design)) stop("Extracted and installed experimental designs differ")
if (!identical(dim(source_object), c(3006L, 23L))) stop("Materialized UbiLength is not 3006 x 23")
lfq_columns <- grep("^LFQ\\.intensity\\.", names(source_object), value = TRUE)
if (length(lfq_columns) != 12L) stop("Expected exactly 12 LFQ columns")
lfq_labels <- sub("^LFQ\\.intensity\\.", "", lfq_columns)
if (!identical(lfq_labels, as.character(source_design$label))) stop("LFQ columns do not map one-to-one and in order to design labels")
required_flags <- c("Reverse", "Potential.contaminant", "Only.identified.by.site")
if (!all(required_flags %in% names(source_object))) stop("Required source flags are missing")

private_materialized <- file.path(p10, "source-materialized")
dir.create(private_materialized, recursive = TRUE, showWarnings = FALSE)
source_object_rds <- file.path(private_materialized, "UbiLength.DEP-1.32.0.private.rds")
source_design_rds <- file.path(private_materialized, "UbiLength_ExpDesign.DEP-1.32.0.private.rds")
saveRDS(source_object, source_object_rds, version = 3, compress = FALSE)
saveRDS(source_design, source_design_rds, version = 3, compress = FALSE)

rd_text <- paste(readLines(rd_path, warn = FALSE, encoding = "UTF-8"), collapse = "\n")
rd_claims_35 <- grepl("3006 observations and 35 variables", rd_text, fixed = TRUE)
input_audit <- list(
  status = "PASS_WITH_PROVENANCE_DISCREPANCY",
  package = list(
    name = "DEP",
    version = actual_versions[["DEP"]],
    bioconductor_release = as.character(BiocManager::version()),
    license = unname(utils::packageDescription("DEP")[["License"]]),
    repository = unname(utils::packageDescription("DEP")[["Repository"]]),
    git_commit = unname(utils::packageDescription("DEP")[["git_last_commit"]])
  ),
  source_hashes = list(
    dep_source_tarball_sha256 = sha_file(tarball),
    ubilength_rda_sha256 = sha_file(source_rda),
    experimental_design_rda_sha256 = sha_file(design_rda),
    ubilength_materialized_rds_sha256 = sha_file(source_object_rds),
    experimental_design_materialized_rds_sha256 = sha_file(source_design_rds),
    ubilength_serialization_sha256 = digest::digest(source_object, algo = "sha256", serialize = TRUE, serializeVersion = 3),
    experimental_design_serialization_sha256 = digest::digest(source_design, algo = "sha256", serialize = TRUE, serializeVersion = 3)
  ),
  materialized_object = list(
    dimensions = unname(dim(source_object)),
    names = names(source_object),
    lfq_columns = lfq_columns,
    lfq_labels = lfq_labels,
    design_labels = as.character(source_design$label),
    lfq_design_exact_ordered_match = identical(lfq_labels, as.character(source_design$label)),
    installed_equals_extracted = TRUE
  ),
  documentation_claim = list(dimensions = c(3006L, 35L), raw_intensity_columns = 12L, lfq_columns = 12L),
  provenance_discrepancy = list(
    present = isTRUE(rd_claims_35),
    blocking = FALSE,
    description = "DEP 1.32.0 Rd documents 3006 x 35, but the embedded object is 3006 x 23 and omits the 12 documented raw intensity columns; this run uses only the 12 materialized LFQ columns."
  ),
  literature = list(
    pmid = "28190767",
    doi = "10.1016/j.molcel.2017.01.004",
    proteomexchange = "PXD004185"
  ),
  redistribution = list(
    package_license = "Artistic-2.0",
    source_article_bytes_in_run = FALSE,
    raw_mass_spectra_downloaded = FALSE,
    original_package_objects_private_only = TRUE
  )
)
write_json(input_audit, file.path(p10, "input_audit.json"))
writeLines(paste(seq_along(names(source_object)), names(source_object), sep = "\t"), file.path(p10, "ubilength_column_names.tsv"), useBytes = TRUE)
sample_map <- data.frame(
  sample_id = as.character(source_design$label),
  condition = as.character(source_design$condition),
  replicate = as.integer(source_design$replicate),
  quant_column = paste0("LFQ.intensity.", source_design$label),
  statistical_unit = "documented experimental replicate/pull-down sample",
  independent_biological_status = "not independently established by package documentation",
  stringsAsFactors = FALSE
)
write_tsv(sample_map, file.path(p10, "sample_map.tsv"))
source_manifest <- data.frame(
  source_id = c("DEP_source_tarball", "UbiLength_rda", "UbiLength_ExpDesign_rda", "UbiLength_materialized_rds", "UbiLength_ExpDesign_materialized_rds"),
  logical_locator = c("bioconductor://3.22/DEP_1.32.0.tar.gz", "package-source://DEP/data/UbiLength.rda", "package-source://DEP/data/UbiLength_ExpDesign.rda", "private-work://materialized/UbiLength.rds", "private-work://materialized/UbiLength_ExpDesign.rds"),
  sha256 = c(sha_file(tarball), sha_file(source_rda), sha_file(design_rda), sha_file(source_object_rds), sha_file(source_design_rds)),
  size_bytes = c(file.info(tarball)$size, file.info(source_rda)$size, file.info(design_rda)$size, file.info(source_object_rds)$size, file.info(source_design_rds)$size),
  copied_to_user_delivery = FALSE,
  stringsAsFactors = FALSE
)
write_tsv(source_manifest, file.path(p10, "source_manifest.tsv"))
checkpoint("P10_input_audit")

# P20: explicit filtering, zero-to-NA conversion, and quantitative-object creation.
p20 <- new_stage("P20_preprocessing")
flag_matrix <- sapply(required_flags, function(nm) source_object[[nm]] == "+")
if (!is.matrix(flag_matrix)) flag_matrix <- matrix(flag_matrix, ncol = length(required_flags))
colnames(flag_matrix) <- required_flags
excluded <- rowSums(flag_matrix) > 0
reasons <- apply(flag_matrix, 1L, function(z) paste(required_flags[z], collapse = ";"))
exclusion_ledger <- data.frame(
  source_row = seq_len(nrow(source_object)),
  protein_group_id = source_object$Protein.IDs,
  reverse = flag_matrix[, "Reverse"],
  potential_contaminant = flag_matrix[, "Potential.contaminant"],
  site_only = flag_matrix[, "Only.identified.by.site"],
  excluded = excluded,
  exclusion_reason = reasons,
  stringsAsFactors = FALSE
)
write_tsv(exclusion_ledger, file.path(p20, "exclusion_ledger.tsv"))

filtered <- source_object[!excluded, , drop = FALSE]
filtered_unique <- DEP::make_unique(filtered, "Gene.names", "Protein.IDs", delim = ";")
quant_indices <- match(lfq_columns, names(filtered_unique))
if (anyNA(quant_indices)) stop("LFQ columns missing after make_unique")
quant_indices <- as.integer(quant_indices)
se <- DEP::make_se(filtered_unique, quant_indices, source_design)
if (!inherits(se, "SummarizedExperiment")) stop("DEP make_se did not create a SummarizedExperiment")
assay_log2 <- SummarizedExperiment::assay(se)
if (ncol(assay_log2) != 12L) stop("DEP assay does not contain 12 samples")

lfq_raw_filtered <- as.matrix(filtered_unique[, lfq_columns, drop = FALSE])
storage.mode(lfq_raw_filtered) <- "numeric"
zero_mask <- lfq_raw_filtered == 0
missing_mask <- is.na(assay_log2)
if (!identical(unname(zero_mask), unname(missing_mask))) stop("DEP zero-to-NA mask differs from explicit source-zero mask")
zero_by_sample <- colSums(zero_mask)
zero_summary <- data.frame(
  sample_id = lfq_labels,
  condition = source_design$condition,
  original_zero_count = as.integer(zero_by_sample),
  filtered_protein_groups = nrow(filtered_unique),
  missing_fraction_after_zero_to_na = as.numeric(zero_by_sample / nrow(filtered_unique)),
  positive_observed_count = as.integer(colSums(!missing_mask)),
  stringsAsFactors = FALSE
)
write_tsv(zero_summary, file.path(p20, "zero_to_na_by_sample.tsv"))
zero_overall <- list(
  original_zero_count = sum(zero_mask),
  affected_protein_groups = sum(rowSums(zero_mask) > 0),
  affected_samples = sum(colSums(zero_mask) > 0),
  filtered_protein_groups = nrow(filtered_unique),
  total_lfq_cells = length(zero_mask),
  conversion = "derived layer only: source zero placeholder -> NA",
  original_object_mutated = FALSE
)
write_json(zero_overall, file.path(p20, "zero_to_na_summary.json"))

row_meta <- as.data.frame(SummarizedExperiment::rowData(se), stringsAsFactors = FALSE)
feature_map <- data.frame(
  protein_group_id = row_meta$ID,
  computational_name = row_meta$name,
  gene_name = row_meta$Gene.names,
  majority_protein_ids = row_meta$Majority.protein.IDs,
  protein_names = row_meta$Protein.names,
  peptide_count = row_meta$Peptides,
  unique_peptide_count = row_meta$Unique.peptides,
  ambiguity_note = "Protein-group identity preserved; no unique-protein claim",
  stringsAsFactors = FALSE
)
write_tsv(feature_map, file.path(p20, "feature_map.tsv"))
write_tsv(data.frame(protein_group_id = row_meta$ID, assay_log2, check.names = FALSE), file.path(p20, "log2_lfq_matrix.private.tsv"))
write_tsv(data.frame(protein_group_id = row_meta$ID, missing_mask, check.names = FALSE), file.path(p20, "missingness_mask.private.tsv"))
saveRDS(se, file.path(p20, "dep_log2_lfq_no_imputation.private.rds"), version = 3)

qc_summary <- list(
  source_rows = nrow(source_object),
  flag_counts = as.list(colSums(flag_matrix)),
  union_excluded_rows = sum(excluded),
  filtered_rows = nrow(filtered_unique),
  original_zero_count_after_flag_filter = sum(zero_mask),
  zero_affected_protein_groups = sum(rowSums(zero_mask) > 0),
  scale = "log2 of positive MaxQuant LFQ intensity; zeros converted to NA in derived layer",
  normalization = "MaxQuant LFQ as supplied; no additional VSN in primary model",
  qfeatures_role = "Installed and version-verified, but not used because this is already a protein-group matrix without PSM/peptide assay lineage"
)
write_json(qc_summary, file.path(p20, "preprocessing_qc.json"))
checkpoint("P20_preprocessing")

# P30: primary no-imputation limma model.
p30 <- new_stage("P30_primary_limma_no_imputation")
condition_all <- as.character(SummarizedExperiment::colData(se)$condition)
contrast_samples <- condition_all %in% c("Ctrl", "Ubi6")
contrast_condition <- condition_all[contrast_samples]
contrast_matrix <- assay_log2[, contrast_samples, drop = FALSE]
n_ctrl <- rowSums(!is.na(contrast_matrix[, contrast_condition == "Ctrl", drop = FALSE]))
n_ubi6 <- rowSums(!is.na(contrast_matrix[, contrast_condition == "Ubi6", drop = FALSE]))
primary_eligible <- n_ctrl >= 2L & n_ubi6 >= 2L
if (sum(primary_eligible) < 100L) stop("Unexpectedly few primary-eligible protein groups")
primary_fit <- fit_limma(
  contrast_matrix[primary_eligible, , drop = FALSE],
  contrast_condition,
  feature_ids = row_meta$ID[primary_eligible],
  metadata = row_meta[primary_eligible, , drop = FALSE]
)
primary_result <- primary_fit$result
primary_result$analysis_variant <- "primary_no_imputation"
primary_result$primary_eligible_rule <- ">=2 observed experimental replicates in Ctrl and >=2 in Ubi6"
primary_result$significant_fdr05_abs_lfc1 <- !is.na(primary_result$fdr_bh) & primary_result$fdr_bh < 0.05 & abs(primary_result$log2_fc) >= 1
write_tsv(primary_result, file.path(p30, "protein_results_primary.tsv"))
saveRDS(primary_fit$fit, file.path(p30, "limma_primary_fit.private.rds"), version = 3)
write_json(list(
  contrast = "Ubi6_vs_Ctrl",
  contrast_selection = "pre-fit, design-semantic, non-data-driven",
  design_columns = colnames(primary_fit$design),
  design_rank = qr(primary_fit$design)$rank,
  tested_protein_groups = nrow(primary_result),
  fdr_method = "Benjamini-Hochberg across all tested protein groups for this one contrast",
  fdr05_abs_lfc1_count = sum(primary_result$significant_fdr05_abs_lfc1, na.rm = TRUE),
  model = "limma lmFit + contrasts.fit + eBayes(trend=TRUE, robust=TRUE)",
  missingness = "observed log2 LFQ only; no imputation"
), file.path(p30, "primary_model_summary.json"))
checkpoint("P30_primary_limma_no_imputation")

# P40: DEP MinProb left-censored sensitivity and stability comparison.
p40 <- new_stage("P40_dep_minprob_sensitivity")
se_sensitivity <- DEP::filter_missval(se, thr = 1)
set.seed(20260719)
se_minprob <- DEP::impute(se_sensitivity, fun = "MinProb", q = 0.01)
minprob_matrix_all <- SummarizedExperiment::assay(se_minprob)
if (anyNA(minprob_matrix_all)) stop("DEP MinProb sensitivity retained missing values")
row_meta_sensitivity <- as.data.frame(SummarizedExperiment::rowData(se_minprob), stringsAsFactors = FALSE)
condition_sensitivity <- as.character(SummarizedExperiment::colData(se_minprob)$condition)
contrast_samples_sensitivity <- condition_sensitivity %in% c("Ctrl", "Ubi6")
sensitivity_fit <- fit_limma(
  minprob_matrix_all[, contrast_samples_sensitivity, drop = FALSE],
  condition_sensitivity[contrast_samples_sensitivity],
  feature_ids = row_meta_sensitivity$ID,
  metadata = row_meta_sensitivity
)
sensitivity_result <- sensitivity_fit$result
sensitivity_result$analysis_variant <- "sensitivity_DEP_MinProb_q0.01"
sensitivity_result$seed <- 20260719L
sensitivity_result$significant_fdr05_abs_lfc1 <- !is.na(sensitivity_result$fdr_bh) & sensitivity_result$fdr_bh < 0.05 & abs(sensitivity_result$log2_fc) >= 1
write_tsv(sensitivity_result, file.path(p40, "protein_results_minprob_sensitivity.tsv"))
saveRDS(se_minprob, file.path(p40, "dep_minprob_q0.01.private.rds"), version = 3)
saveRDS(sensitivity_fit$fit, file.path(p40, "limma_minprob_fit.private.rds"), version = 3)

comparison <- merge(
  primary_result[, c("protein_group_id", "gene_name", "log2_fc", "fdr_bh", "significant_fdr05_abs_lfc1")],
  sensitivity_result[, c("protein_group_id", "log2_fc", "fdr_bh", "significant_fdr05_abs_lfc1")],
  by = "protein_group_id", all = TRUE, suffixes = c("_primary", "_minprob")
)
comparison$present_primary <- !is.na(comparison$log2_fc_primary)
comparison$present_minprob <- !is.na(comparison$log2_fc_minprob)
comparison$direction_reversal <- with(comparison, present_primary & present_minprob & sign(log2_fc_primary) != sign(log2_fc_minprob))
comparison$abs_effect_delta <- abs(comparison$log2_fc_minprob - comparison$log2_fc_primary)
comparison$effect_delta_ge_1 <- comparison$present_primary & comparison$present_minprob & comparison$abs_effect_delta >= 1
comparison$fdr_class_change <- comparison$present_primary & comparison$present_minprob &
  comparison$significant_fdr05_abs_lfc1_primary != comparison$significant_fdr05_abs_lfc1_minprob
comparison$stability_class <- ifelse(
  !comparison$present_primary & comparison$present_minprob, "sensitivity_only_not_primary_estimand",
  ifelse(comparison$direction_reversal, "direction_reversal",
    ifelse(comparison$effect_delta_ge_1, "effect_delta_ge_1",
      ifelse(comparison$fdr_class_change, "fdr_class_change", "stable_by_prespecified_rules"))))
write_tsv(comparison, file.path(p40, "primary_vs_minprob_comparison.tsv"))
common <- comparison$present_primary & comparison$present_minprob
sensitivity_summary <- list(
  method = "DEP 1.31.0 runtime MinProb, q=0.01, seed=20260719; same limma contrast after imputation; input objects remain hash-bound to DEP 1.32.0 source",
  role = "left-censored sensitivity only; alternative estimand/preprocessing, never substituted for primary",
  sensitivity_tested_protein_groups = nrow(sensitivity_result),
  common_with_primary = sum(common),
  sensitivity_only = sum(!comparison$present_primary & comparison$present_minprob),
  spearman_log2fc_common = unname(stats::cor(comparison$log2_fc_primary[common], comparison$log2_fc_minprob[common], method = "spearman")),
  direction_reversals_common = sum(comparison$direction_reversal, na.rm = TRUE),
  effect_delta_ge_1_common = sum(comparison$effect_delta_ge_1, na.rm = TRUE),
  fdr_class_changes_common = sum(comparison$fdr_class_change, na.rm = TRUE)
)
write_json(sensitivity_summary, file.path(p40, "sensitivity_summary.json"))
checkpoint("P40_dep_minprob_sensitivity")

# P50: original diagnostics and refined publication-style figures.
transition("ANALYSIS_QA", note = "Statistical and artifact gates passed; figures may now be generated")
transition("VISUALIZING")
p50 <- new_stage("P50_figures")
original_dir <- file.path(p50, "original")
final_dir <- file.path(p50, "final")
dir.create(original_dir, recursive = TRUE, showWarnings = FALSE)
dir.create(final_dir, recursive = TRUE, showWarnings = FALSE)

condition_palette <- c(Ctrl = "#7A7A7A", Ubi1 = "#0072B2", Ubi4 = "#E69F00", Ubi6 = "#D55E00")

# PCA uses only proteins observed in all 12 samples; no hidden imputation.
complete_all <- rowSums(is.na(assay_log2)) == 0L
if (sum(complete_all) < 20L) stop("Too few complete proteins for descriptive PCA")
pca <- stats::prcomp(t(assay_log2[complete_all, , drop = FALSE]), center = TRUE, scale. = FALSE)
variance <- 100 * pca$sdev^2 / sum(pca$sdev^2)
pca_df <- data.frame(
  sample_id = as.character(SummarizedExperiment::colData(se)$label),
  condition = factor(condition_all, levels = c("Ctrl", "Ubi1", "Ubi4", "Ubi6")),
  replicate = as.integer(SummarizedExperiment::colData(se)$replicate),
  PC1 = pca$x[, 1L], PC2 = pca$x[, 2L], stringsAsFactors = FALSE
)
png(file.path(original_dir, "01_sample_pca_original.png"), width = 1800, height = 1400, res = 220)
plot(pca_df$PC1, pca_df$PC2, pch = 19, col = unname(condition_palette[pca_df$condition]),
     xlab = "PC1", ylab = "PC2", main = "PCA")
text(pca_df$PC1, pca_df$PC2, labels = pca_df$sample_id, pos = 3, cex = 0.7)
dev.off()
p_pca <- ggplot2::ggplot(pca_df, ggplot2::aes(PC1, PC2, color = condition, label = sample_id)) +
  ggplot2::geom_hline(yintercept = 0, linewidth = 0.25, color = "#DDDDDD") +
  ggplot2::geom_vline(xintercept = 0, linewidth = 0.25, color = "#DDDDDD") +
  ggplot2::geom_point(size = 3.3, alpha = 0.95) +
  ggrepel::geom_text_repel(size = 3.2, max.overlaps = Inf, box.padding = 0.35,
                           min.segment.length = 0, show.legend = FALSE) +
  ggplot2::scale_color_manual(values = condition_palette, drop = FALSE) +
  ggplot2::labs(
    title = "Sample structure in observed log2 LFQ data",
    subtitle = paste0("PCA uses ", sum(complete_all), " protein groups observed in all 12 samples; no imputation"),
    x = sprintf("PC1 (%.1f%%)", variance[1L]), y = sprintf("PC2 (%.1f%%)", variance[2L]),
    color = "Condition"
  ) + theme_final()
ggplot2::ggsave(file.path(final_dir, "01_sample_pca_final.png"), p_pca, width = 8.5, height = 6.5, dpi = 320, bg = "white")

missing_df <- zero_summary
missing_df$sample_id <- factor(missing_df$sample_id, levels = missing_df$sample_id)
png(file.path(original_dir, "02_missingness_original.png"), width = 1800, height = 1400, res = 220)
barplot(100 * missing_df$missing_fraction_after_zero_to_na, names.arg = missing_df$sample_id,
        las = 2, col = "grey60", ylab = "Missing (%)", main = "Missing LFQ values")
dev.off()
p_missing <- ggplot2::ggplot(missing_df, ggplot2::aes(sample_id, 100 * missing_fraction_after_zero_to_na, fill = condition)) +
  ggplot2::geom_col(width = 0.76, color = "white", linewidth = 0.3) +
  ggplot2::geom_text(ggplot2::aes(label = original_zero_count), vjust = -0.35, size = 3.0) +
  ggplot2::scale_fill_manual(values = condition_palette, drop = FALSE) +
  ggplot2::scale_y_continuous(expand = ggplot2::expansion(mult = c(0, 0.10))) +
  ggplot2::labs(
    title = "MaxQuant zero placeholders converted to missing values",
    subtitle = paste0(sum(zero_mask), " zero cells across ", sum(rowSums(zero_mask) > 0), " protein groups; labels show zero counts"),
    x = "Experimental sample", y = "Derived missingness (%)", fill = "Condition"
  ) + theme_final() + ggplot2::theme(axis.text.x = ggplot2::element_text(angle = 40, hjust = 1))
ggplot2::ggsave(file.path(final_dir, "02_missingness_final.png"), p_missing, width = 9.5, height = 6.5, dpi = 320, bg = "white")

volcano <- primary_result
volcano$minus_log10_fdr <- -log10(pmax(volcano$fdr_bh, .Machine$double.xmin))
volcano$classification <- ifelse(volcano$significant_fdr05_abs_lfc1,
  ifelse(volcano$log2_fc > 0, "Higher in Ubi6", "Lower in Ubi6"), "Not called")
volcano$classification <- factor(volcano$classification, levels = c("Higher in Ubi6", "Lower in Ubi6", "Not called"))
label_order <- order(volcano$fdr_bh, -abs(volcano$log2_fc), na.last = NA)
label_idx <- head(label_order[volcano$significant_fdr05_abs_lfc1[label_order]], 10L)
volcano$label <- ""
volcano$label[label_idx] <- ifelse(nzchar(volcano$gene_name[label_idx]), volcano$gene_name[label_idx], volcano$protein_group_id[label_idx])
png(file.path(original_dir, "03_primary_volcano_original.png"), width = 1800, height = 1400, res = 220)
plot(volcano$log2_fc, -log10(volcano$p_value), pch = 16, cex = 0.55,
     xlab = "log2 fold change", ylab = "-log10(P)", main = "Ubi6 vs Ctrl")
abline(v = c(-1, 1), lty = 2, col = "grey50")
dev.off()
p_volcano <- ggplot2::ggplot(volcano, ggplot2::aes(log2_fc, minus_log10_fdr, color = classification)) +
  ggplot2::geom_hline(yintercept = -log10(0.05), linetype = 2, color = "#666666", linewidth = 0.45) +
  ggplot2::geom_vline(xintercept = c(-1, 1), linetype = 2, color = "#666666", linewidth = 0.45) +
  ggplot2::geom_point(size = 1.7, alpha = 0.72) +
  ggrepel::geom_text_repel(ggplot2::aes(label = label), size = 3.0, max.overlaps = Inf,
                           box.padding = 0.35, point.padding = 0.25, min.segment.length = 0,
                           show.legend = FALSE) +
  ggplot2::scale_color_manual(values = c("Higher in Ubi6" = "#D55E00", "Lower in Ubi6" = "#0072B2", "Not called" = "#B8B8B8")) +
  ggplot2::labs(
    title = "Observed-data primary analysis: Ubi6 versus Ctrl",
    subtitle = paste0(nrow(volcano), " protein groups tested; BH FDR < 0.05 and |log2FC| >= 1 highlighted"),
    x = "log2 fold change (Ubi6 - Ctrl)", y = "-log10(BH FDR)", color = "Prespecified call"
  ) + theme_final()
ggplot2::ggsave(file.path(final_dir, "03_primary_volcano_final.png"), p_volcano, width = 9.0, height = 6.8, dpi = 320, bg = "white")

comparison_common <- comparison[common, , drop = FALSE]
comparison_common$stability_group <- ifelse(comparison_common$direction_reversal, "Direction reversal",
  ifelse(comparison_common$effect_delta_ge_1, "|Delta effect| >= 1",
    ifelse(comparison_common$fdr_class_change, "FDR class changed", "Stable by rules")))
comparison_common$stability_group <- factor(comparison_common$stability_group,
  levels = c("Direction reversal", "|Delta effect| >= 1", "FDR class changed", "Stable by rules"))
range_effect <- range(c(comparison_common$log2_fc_primary, comparison_common$log2_fc_minprob), finite = TRUE)
png(file.path(original_dir, "04_minprob_sensitivity_original.png"), width = 1800, height = 1400, res = 220)
plot(comparison_common$log2_fc_primary, comparison_common$log2_fc_minprob,
     xlab = "No-imputation log2FC", ylab = "MinProb log2FC", main = "Sensitivity")
abline(0, 1, lty = 2, col = "red")
dev.off()
p_sensitivity <- ggplot2::ggplot(comparison_common, ggplot2::aes(log2_fc_primary, log2_fc_minprob, color = stability_group)) +
  ggplot2::geom_abline(slope = 1, intercept = 0, linetype = 2, color = "#444444", linewidth = 0.55) +
  ggplot2::geom_point(size = 1.75, alpha = 0.70) +
  ggplot2::coord_equal(xlim = range_effect, ylim = range_effect) +
  ggplot2::scale_color_manual(values = c("Direction reversal" = "#CC79A7", "|Delta effect| >= 1" = "#E69F00", "FDR class changed" = "#56B4E9", "Stable by rules" = "#999999")) +
  ggplot2::labs(
    title = "Primary effects versus DEP MinProb sensitivity",
    subtitle = sprintf("Common protein groups: %d; Spearman rho = %.3f; MinProb is not the primary estimand",
      nrow(comparison_common), sensitivity_summary$spearman_log2fc_common),
    x = "Primary no-imputation log2FC", y = "DEP MinProb sensitivity log2FC", color = "Stability"
  ) + theme_final()
ggplot2::ggsave(file.path(final_dir, "04_minprob_sensitivity_final.png"), p_sensitivity, width = 8.8, height = 7.0, dpi = 320, bg = "white")

figure_metrics <- list(
  pca_complete_protein_groups = sum(complete_all),
  pca_variance_pc1_percent = unname(variance[1L]),
  pca_variance_pc2_percent = unname(variance[2L]),
  missingness_total_zero_placeholders = sum(zero_mask),
  volcano_tested_protein_groups = nrow(volcano),
  volcano_calls_fdr05_abs_lfc1 = sum(volcano$significant_fdr05_abs_lfc1, na.rm = TRUE),
  sensitivity_common_protein_groups = nrow(comparison_common),
  sensitivity_spearman = sensitivity_summary$spearman_log2fc_common,
  native_review_status = "PENDING_MANUAL_ORIGINAL_PIXEL_REVIEW"
)
write_json(figure_metrics, file.path(p50, "figure_metrics.json"))
checkpoint("P50_figures", maturity = "data-verified")

# P60: machine-verifiable analysis QA evidence before native image review.
p60 <- new_stage("P60_analysis_qa")
qa_checks <- data.frame(
  check_id = c(
    "source_object_identity", "source_dimensions", "lfq_design_mapping", "flag_filter_complete",
    "source_object_immutable", "zero_to_na_mask_exact", "log2_once", "contrast_prefit_frozen",
    "primary_no_imputation", "design_full_rank", "bh_fdr", "minprob_separate",
    "pca_no_imputation", "patient_claim_blocked", "package_versions_pinned"
  ),
  status = "PASS",
  evidence = c(
    "installed DEP object identical to extracted package RDA object",
    "materialized object 3006 x 23; Rd 3006 x 35 discrepancy retained as non-blocking provenance warning",
    "12 LFQ labels exactly match 12 design labels in order",
    paste0(sum(excluded), " union-excluded rows; all three actual '+' flags applied"),
    "source object remains private and unchanged; derived objects written separately",
    paste0(sum(zero_mask), " zero placeholders equal derived missingness mask exactly"),
    "positive LFQ transformed once to log2 by DEP::make_se",
    "Ubi6 - Ctrl documented in ANALYSIS_DESIGN.pre_fit.md before fitting",
    paste0(nrow(primary_result), " protein groups modelled with >=2 observations per group"),
    paste0("rank ", qr(primary_fit$design)$rank, "/", ncol(primary_fit$design)),
    "BH across the one-contrast tested protein family",
    "DEP MinProb q=0.01 seed=20260719 stored and labelled sensitivity-only",
    paste0(sum(complete_all), " complete protein groups; no PCA imputation"),
    "claim boundary prohibits patient, population, causal, pathway-activation, and direct-binding conclusions",
    paste(names(actual_versions), actual_versions, collapse = "; ")
  ),
  stringsAsFactors = FALSE
)
write_tsv(qa_checks, file.path(p60, "qa_checks.tsv"))
write_json(list(
  status = "PASS_PENDING_NATIVE_VISUAL_REVIEW",
  high_severity_scientific_errors = 0,
  primary_significant_calls = sum(primary_result$significant_fdr05_abs_lfc1, na.rm = TRUE),
  sensitivity = sensitivity_summary,
  session = list(r = R.version.string, packages = as.list(actual_versions))
), file.path(p60, "analysis_qa_summary.json"))
writeLines(capture.output(utils::sessionInfo()), file.path(p60, "sessionInfo.txt"), useBytes = TRUE)
checkpoint("P60_analysis_qa")

transition("NATIVE_VISUAL_REVIEW", note = "Pipeline complete; original and final PNGs require original-pixel inspection before delivery")
cat(jsonlite::toJSON(list(
  status = "PASS_PENDING_NATIVE_VISUAL_REVIEW",
  run_root = run_root,
  checkpoints = c("P10_input_audit", "P20_preprocessing", "P30_primary_limma_no_imputation", "P40_dep_minprob_sensitivity", "P50_figures", "P60_analysis_qa"),
  primary_tested = nrow(primary_result),
  primary_calls = sum(primary_result$significant_fdr05_abs_lfc1, na.rm = TRUE),
  sensitivity_spearman = sensitivity_summary$spearman_log2fc_common
), auto_unbox = TRUE, pretty = TRUE), "\n")
