#!/usr/bin/env Rscript

# PBMC3K teaching pipeline. Dependency installation is intentionally excluded.

parse_args <- function(args) {
  out <- list()
  index <- 1L
  while (index <= length(args)) {
    key <- args[[index]]
    if (!startsWith(key, "--") || index == length(args)) {
      stop("Arguments must be supplied as --name value pairs", call. = FALSE)
    }
    out[[substring(key, 3L)]] <- args[[index + 1L]]
    index <- index + 2L
  }
  required <- c("data-root", "run-root", "params", "signature", "completion-marker")
  missing <- required[!required %in% names(out)]
  if (length(missing) > 0L) {
    stop(sprintf("Missing arguments: %s", paste(missing, collapse = ", ")), call. = FALSE)
  }
  out
}

require_exact_environment <- function() {
  required_packages <- c("Seurat", "SeuratObject", "ggplot2", "patchwork", "jsonlite")
  unavailable <- required_packages[!vapply(required_packages, requireNamespace, quietly = TRUE, FUN.VALUE = logical(1))]
  if (length(unavailable) > 0L) {
    stop(sprintf("R_PACKAGE_MISSING: %s", paste(unavailable, collapse = ", ")), call. = FALSE)
  }
  observed_r <- paste(R.version$major, R.version$minor, sep = ".")
  if (!identical(observed_r, "4.5.3")) {
    stop(sprintf("R_VERSION_MISMATCH: expected 4.5.3, observed %s", observed_r), call. = FALSE)
  }
  observed_seurat <- as.character(utils::packageVersion("Seurat"))
  if (!identical(observed_seurat, "5.5.0")) {
    stop(sprintf("SEURAT_VERSION_MISMATCH: expected 5.5.0, observed %s", observed_seurat), call. = FALSE)
  }
  list(
    r = observed_r,
    platform = R.version$platform,
    packages = stats::setNames(
      lapply(required_packages, function(package) as.character(utils::packageVersion(package))),
      required_packages
    )
  )
}

read_signature <- function(path) {
  value <- trimws(paste(readLines(path, warn = FALSE, encoding = "UTF-8"), collapse = ""))
  if (!grepl("^[0-9a-f]{64}$", value)) {
    stop("analysis signature must be a lowercase SHA-256 string", call. = FALSE)
  }
  value
}

assert_within <- function(path, root) {
  normalized_path <- normalizePath(path, winslash = "/", mustWork = FALSE)
  normalized_root <- normalizePath(root, winslash = "/", mustWork = TRUE)
  prefix <- paste0(sub("/+$", "", normalized_root), "/")
  if (!startsWith(paste0(normalized_path, "/"), prefix) && normalized_path != normalized_root) {
    stop(sprintf("Path escapes run root: %s", normalized_path), call. = FALSE)
  }
  invisible(normalized_path)
}

sha256_file <- function(path) {
  value <- unname(tools::sha256sum(path))
  if (length(value) != 1L || is.na(value) || !grepl("^[0-9a-fA-F]{64}$", value)) {
    stop(sprintf("SHA256_EVIDENCE_FAILED: %s", basename(path)), call. = FALSE)
  }
  tolower(value)
}

promote_if_changed <- function(temporary, path, artifact_label) {
  if (!file.exists(temporary)) {
    stop(sprintf("Temporary %s artifact is missing: %s", artifact_label, temporary), call. = FALSE)
  }
  if (
    file.exists(path) &&
    identical(unname(file.info(temporary)$size), unname(file.info(path)$size)) &&
    identical(sha256_file(temporary), sha256_file(path))
  ) {
    unlink(temporary, force = TRUE)
    return(invisible(FALSE))
  }
  if (file.exists(path)) {
    unlink(path, force = TRUE)
  }
  if (!file.rename(temporary, path)) {
    stop(sprintf("Unable to promote %s artifact: %s", artifact_label, path), call. = FALSE)
  }
  invisible(TRUE)
}

atomic_json <- function(value, path) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  temporary <- paste0(path, ".tmp")
  jsonlite::write_json(value, temporary, auto_unbox = TRUE, pretty = TRUE, null = "null")
  promote_if_changed(temporary, path, "JSON")
}

atomic_csv <- function(value, path) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  temporary <- paste0(path, ".tmp")
  utils::write.csv(value, temporary, row.names = FALSE, na = "")
  promote_if_changed(temporary, path, "table")
}

atomic_rds <- function(value, path) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  temporary <- paste0(path, ".tmp")
  saveRDS(value, temporary, compress = TRUE)
  promote_if_changed(temporary, path, "RDS")
}

copy_artifact_if_changed <- function(source, path) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  temporary <- paste0(path, ".tmp.copy")
  unlink(temporary, force = TRUE)
  if (!file.copy(source, temporary, overwrite = TRUE, copy.mode = TRUE, copy.date = FALSE)) {
    stop(sprintf("Unable to stage copied artifact: %s", source), call. = FALSE)
  }
  promote_if_changed(temporary, path, "copied")
}

checkpoint_object <- function(stage_id, object_name, run_root, signature, environment, builder) {
  final_dir <- file.path(run_root, "04_intermediate", stage_id)
  marker_path <- file.path(final_dir, "stage.complete.json")
  object_path <- file.path(final_dir, object_name)
  if (dir.exists(final_dir)) {
    if (!file.exists(marker_path) || !file.exists(object_path)) {
      stop(sprintf("CHECKPOINT_INCOMPLETE: %s", stage_id), call. = FALSE)
    }
    marker <- jsonlite::read_json(marker_path, simplifyVector = TRUE)
    if (!identical(marker$analysis_signature, signature)) {
      stop(sprintf("CHECKPOINT_SIGNATURE_MISMATCH: %s", stage_id), call. = FALSE)
    }
    message(sprintf("checkpoint reuse: %s", stage_id))
    return(readRDS(object_path))
  }

  staging_root <- file.path(run_root, "_staging")
  dir.create(staging_root, recursive = TRUE, showWarnings = FALSE)
  staging_dir <- tempfile(pattern = paste0(stage_id, "-"), tmpdir = staging_root)
  dir.create(staging_dir, recursive = FALSE, showWarnings = FALSE)
  value <- builder(staging_dir)
  saveRDS(value, file.path(staging_dir, object_name), compress = TRUE)
  marker <- list(
    schema_version = "1.0.0",
    stage_id = stage_id,
    status = "checkpointed",
    analysis_signature = signature,
    object = object_name,
    r_version = environment$r,
    seurat_version = environment$packages$Seurat
  )
  jsonlite::write_json(
    marker,
    file.path(staging_dir, "stage.complete.json"),
    auto_unbox = TRUE,
    pretty = TRUE,
    null = "null"
  )
  if (!file.rename(staging_dir, final_dir)) {
    stop(sprintf("CHECKPOINT_PROMOTION_FAILED: %s", stage_id), call. = FALSE)
  }
  message(sprintf("checkpoint promoted: %s", stage_id))
  value
}

assert_equal <- function(observed, expected, label) {
  if (!identical(as.integer(observed), as.integer(expected))) {
    stop(sprintf("CANONICAL_GATE_FAILED[%s]: expected %s, observed %s", label, expected, observed), call. = FALSE)
  }
}

run_umap_with_explicit_transition_option <- function(object, umap_params) {
  option_contract <- umap_params$transition_warning_option
  if (
    !identical(umap_params$method, "uwot") ||
    !identical(umap_params$metric, "cosine") ||
    !identical(option_contract$name, "Seurat.warn.umap.uwot") ||
    !identical(option_contract$value_during_call, FALSE) ||
    !identical(option_contract$restore_after_call, TRUE)
  ) {
    stop("UMAP_RUNTIME_CONTRACT_MISMATCH", call. = FALSE)
  }
  option_name <- option_contract$name
  previous_was_set <- option_name %in% names(options())
  previous_value <- getOption(option_name, default = NULL)
  restore_option <- function() {
    do.call(options, stats::setNames(list(if (previous_was_set) previous_value else NULL), option_name))
  }
  on.exit(restore_option(), add = TRUE)
  do.call(options, stats::setNames(list(FALSE), option_name))
  if (!identical(getOption(option_name), FALSE)) {
    stop("UMAP_TRANSITION_OPTION_NOT_APPLIED", call. = FALSE)
  }
  result <- Seurat::RunUMAP(
    object,
    dims = seq_len(as.integer(umap_params$dims_used)),
    umap.method = umap_params$method,
    metric = umap_params$metric,
    seed.use = as.integer(umap_params$seed_use),
    verbose = FALSE
  )
  restore_option()
  restored <- if (previous_was_set) {
    identical(getOption(option_name), previous_value)
  } else {
    !option_name %in% names(options())
  }
  if (!isTRUE(restored)) {
    stop("UMAP_TRANSITION_OPTION_NOT_RESTORED", call. = FALSE)
  }
  list(
    object = result,
    contract = list(
      schema_version = "1.0.0",
      option_name = option_name,
      option_previous_state = if (previous_was_set) as.character(previous_value) else "unset",
      option_value_during_call = FALSE,
      option_restored = TRUE,
      umap_method = umap_params$method,
      metric = umap_params$metric,
      seed_use = as.integer(umap_params$seed_use),
      dims_used = as.integer(umap_params$dims_used),
      algorithm_changed = FALSE,
      r_warn_option = as.integer(getOption("warn")),
      warning_delivery = "immediate-stderr-via-options(warn=1)",
      transition_notice_option_applied = TRUE,
      suppress_warnings_used = FALSE,
      handler_muffling_used = FALSE,
      warning_allowlist_used = FALSE,
      purpose = "use Seurat's official transition option in the smallest scope to disable only its one-time migration notice for an explicitly configured uwot/cosine call, then restore the prior option state"
    )
  )
}

save_plot <- function(plot, path, width, height, dpi) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  temporary <- file.path(dirname(path), paste0(".", basename(path), ".tmp.png"))
  ggplot2::ggsave(
    filename = temporary,
    plot = plot,
    width = width,
    height = height,
    units = "in",
    dpi = dpi,
    bg = "white",
    limitsize = FALSE
  )
  promote_if_changed(temporary, path, "plot")
}

args <- parse_args(commandArgs(trailingOnly = TRUE))
options(warn = 1)
if (!identical(as.integer(getOption("warn")), 1L)) {
  stop("R_WARNING_MODE_NOT_IMMEDIATE", call. = FALSE)
}
run_root <- normalizePath(args[["run-root"]], winslash = "/", mustWork = TRUE)
data_root <- normalizePath(args[["data-root"]], winslash = "/", mustWork = TRUE)
params_path <- normalizePath(args$params, winslash = "/", mustWork = TRUE)
signature_path <- normalizePath(args$signature, winslash = "/", mustWork = TRUE)
completion_marker <- normalizePath(args[["completion-marker"]], winslash = "/", mustWork = FALSE)
assert_within(params_path, run_root)
assert_within(signature_path, run_root)
assert_within(completion_marker, run_root)
unlink(completion_marker, force = TRUE)

params <- jsonlite::read_json(params_path, simplifyVector = TRUE)
if (
  !identical(as.integer(params$r_warning_policy$warn), 1L) ||
  !identical(params$r_warning_policy$delivery, "immediate-stderr") ||
  !identical(params$r_warning_policy$forbidden_scan, "fail-closed")
) {
  stop("R_WARNING_POLICY_PARAMS_MISMATCH", call. = FALSE)
}
signature <- read_signature(signature_path)
environment <- require_exact_environment()
set.seed(as.integer(params$random_seed))

expected_input <- 2700L
expected_qc <- 2638L
expected_clusters <- 9L

pbmc <- checkpoint_object(
  "SC01_IMPORT_AND_IDENTITY",
  "pbmc3k_imported.rds",
  run_root,
  signature,
  environment,
  function(stage_dir) {
    counts <- Seurat::Read10X(data.dir = data_root, gene.column = 2, unique.features = TRUE)
    if (is.list(counts)) {
      if (!"Gene Expression" %in% names(counts)) {
        stop("INPUT_MODALITY_MISSING: Gene Expression", call. = FALSE)
      }
      counts <- counts[["Gene Expression"]]
    }
    assert_equal(nrow(counts), 32738L, "matrix_features")
    assert_equal(ncol(counts), expected_input, "input_cells")
    if (anyDuplicated(colnames(counts)) != 0L) {
      stop("INPUT_IDENTITY_FAILED: duplicate barcodes", call. = FALSE)
    }
    original_feature_names <- rownames(counts)
    seurat_feature_names <- gsub("_", "-", original_feature_names, fixed = TRUE)
    if (anyDuplicated(seurat_feature_names) != 0L) {
      stop("FEATURE_NAME_NORMALIZATION_COLLISION: underscore-to-dash created duplicates", call. = FALSE)
    }
    dimensions_before_feature_rename <- dim(counts)
    values_before_feature_rename <- counts@x
    rownames(counts) <- seurat_feature_names
    if (
      !identical(dim(counts), dimensions_before_feature_rename) ||
      !identical(counts@x, values_before_feature_rename)
    ) {
      stop("FEATURE_NAME_NORMALIZATION_CHANGED_COUNTS", call. = FALSE)
    }
    feature_name_mapping <- data.frame(
      original_feature = original_feature_names,
      seurat_feature = seurat_feature_names,
      changed = original_feature_names != seurat_feature_names,
      stringsAsFactors = FALSE
    )
    utils::write.csv(
      feature_name_mapping,
      file.path(stage_dir, "feature_name_mapping.csv"),
      row.names = FALSE
    )
    utils::write.csv(
      data.frame(
        metric = c(
          "input_features",
          "renamed_features",
          "duplicates_after_rename",
          "matrix_rows_unchanged",
          "matrix_columns_unchanged",
          "count_values_unchanged"
        ),
        value = c(
          length(original_feature_names),
          sum(feature_name_mapping$changed),
          anyDuplicated(seurat_feature_names),
          as.integer(nrow(counts) == dimensions_before_feature_rename[[1L]]),
          as.integer(ncol(counts) == dimensions_before_feature_rename[[2L]]),
          as.integer(identical(counts@x, values_before_feature_rename))
        ),
        status = "pass",
        stringsAsFactors = FALSE
      ),
      file.path(stage_dir, "feature_name_mapping_summary.csv"),
      row.names = FALSE
    )
    object <- Seurat::CreateSeuratObject(
      counts = counts,
      project = params$project_name,
      min.cells = as.integer(params$create_object$min_cells),
      min.features = as.integer(params$create_object$min_features)
    )
    assert_equal(nrow(object), 13714L, "seurat_features_after_min_cells")
    assert_equal(ncol(object), expected_input, "seurat_input_cells")
    counts_layer <- SeuratObject::LayerData(object = object, assay = "RNA", layer = "counts")
    sampled <- counts_layer@x[seq_len(min(length(counts_layer@x), 100000L))]
    if (any(!is.finite(sampled)) || any(sampled < 0) || any(abs(sampled - round(sampled)) > 1e-8)) {
      stop("RAW_COUNT_GATE_FAILED: non-negative integer-like counts required", call. = FALSE)
    }
    import_summary <- data.frame(
      metric = c("matrix_features", "matrix_barcodes", "seurat_features", "seurat_cells"),
      value = c(nrow(counts), ncol(counts), nrow(object), ncol(object))
    )
    utils::write.csv(import_summary, file.path(stage_dir, "import_summary.csv"), row.names = FALSE)
    object
  }
)

pbmc <- checkpoint_object(
  "SC04_QC_PER_CAPTURE",
  "pbmc3k_post_qc.rds",
  run_root,
  signature,
  environment,
  function(stage_dir) {
    object <- pbmc
    object[["percent.mt"]] <- Seurat::PercentageFeatureSet(object, pattern = "^MT-")
    metadata <- object[[]]
    metadata$barcode <- rownames(metadata)
    metadata$keep_nFeature_min <- metadata$nFeature_RNA > as.numeric(params$qc$min_features_exclusive)
    metadata$keep_nFeature_max <- metadata$nFeature_RNA < as.numeric(params$qc$max_features_exclusive)
    metadata$keep_percent_mt <- metadata$percent.mt < as.numeric(params$qc$max_percent_mt_exclusive)
    metadata$qc_keep <- metadata$keep_nFeature_min & metadata$keep_nFeature_max & metadata$keep_percent_mt
    metadata$exclusion_reason <- ifelse(
      metadata$qc_keep,
      "retained",
      paste0(
        ifelse(!metadata$keep_nFeature_min, "nFeature_below_or_equal_min;", ""),
        ifelse(!metadata$keep_nFeature_max, "nFeature_above_or_equal_max;", ""),
        ifelse(!metadata$keep_percent_mt, "percent_mt_above_or_equal_max;", "")
      )
    )
    utils::write.csv(metadata, file.path(stage_dir, "qc_cell_audit.csv"), row.names = FALSE)
    summary <- data.frame(
      metric = c("input_cells", "retained_cells", "excluded_cells"),
      value = c(nrow(metadata), sum(metadata$qc_keep), sum(!metadata$qc_keep))
    )
    utils::write.csv(summary, file.path(stage_dir, "qc_summary.csv"), row.names = FALSE)
    object <- subset(object, cells = rownames(metadata)[metadata$qc_keep])
    assert_equal(ncol(object), expected_qc, "qc_retained_cells")
    object
  }
)

pbmc <- checkpoint_object(
  "SC06_NORMALIZE_AND_HVG_PER_SAMPLE",
  "pbmc3k_normalized.rds",
  run_root,
  signature,
  environment,
  function(stage_dir) {
    object <- Seurat::NormalizeData(
      pbmc,
      normalization.method = params$normalization$method,
      scale.factor = as.numeric(params$normalization$scale_factor),
      verbose = FALSE
    )
    object <- Seurat::FindVariableFeatures(
      object,
      selection.method = params$variable_features$method,
      nfeatures = as.integer(params$variable_features$nfeatures),
      verbose = FALSE
    )
    assert_equal(length(Seurat::VariableFeatures(object)), params$variable_features$nfeatures, "variable_features")
    object <- Seurat::ScaleData(object, features = rownames(object), verbose = FALSE)
    utils::write.csv(
      data.frame(rank = seq_along(Seurat::VariableFeatures(object)), feature = Seurat::VariableFeatures(object)),
      file.path(stage_dir, "variable_features.csv"),
      row.names = FALSE
    )
    object
  }
)

pbmc <- checkpoint_object(
  "SC08_GRAPH_CLUSTER_AND_EMBED",
  "pbmc3k_clustered.rds",
  run_root,
  signature,
  environment,
  function(stage_dir) {
    object <- Seurat::RunPCA(
      pbmc,
      features = Seurat::VariableFeatures(pbmc),
      npcs = as.integer(params$pca$npcs),
      verbose = FALSE
    )
    dims <- seq_len(as.integer(params$pca$dims_used))
    object <- Seurat::FindNeighbors(object, dims = dims, verbose = FALSE)
    object <- Seurat::FindClusters(
      object,
      resolution = as.numeric(params$clustering$resolution),
      algorithm = as.integer(params$clustering$algorithm),
      random.seed = as.integer(params$clustering$random_seed),
      verbose = FALSE
    )
    umap_run <- run_umap_with_explicit_transition_option(object, params$umap)
    object <- umap_run$object
    atomic_json(umap_run$contract, file.path(stage_dir, "umap_runtime_contract.json"))
    assert_equal(nrow(Seurat::Embeddings(object, reduction = "umap")), expected_qc, "umap_cells")
    cluster_count <- length(levels(Seurat::Idents(object)))
    assert_equal(cluster_count, expected_clusters, "clusters")
    cluster_sizes <- as.data.frame(table(cluster = as.character(Seurat::Idents(object))), stringsAsFactors = FALSE)
    names(cluster_sizes)[[2L]] <- "cells"
    utils::write.csv(cluster_sizes, file.path(stage_dir, "cluster_sizes.csv"), row.names = FALSE)
    object
  }
)

pbmc <- checkpoint_object(
  "SC09_ANNOTATE_AND_REVIEW",
  "pbmc3k_annotated.rds",
  run_root,
  signature,
  environment,
  function(stage_dir) {
    object <- pbmc
    markers <- Seurat::FindAllMarkers(
      object,
      only.pos = isTRUE(params$markers$only_pos),
      min.pct = as.numeric(params$markers$min_pct),
      logfc.threshold = as.numeric(params$markers$logfc_threshold),
      verbose = FALSE
    )
    if (nrow(markers) == 0L) {
      stop("MARKER_GATE_FAILED: no cluster markers returned", call. = FALSE)
    }
    mapping <- c(
      "0" = "Naive CD4 T",
      "1" = "CD14+ Mono",
      "2" = "Memory CD4 T",
      "3" = "B",
      "4" = "CD8 T",
      "5" = "FCGR3A+ Mono",
      "6" = "NK",
      "7" = "DC",
      "8" = "Platelet"
    )
    cluster_ids <- as.character(Seurat::Idents(object))
    unknown <- sort(unique(cluster_ids[!cluster_ids %in% names(mapping)]))
    if (length(unknown) > 0L || length(unique(cluster_ids)) != expected_clusters) {
      stop(sprintf("ANNOTATION_GATE_FAILED: unexpected clusters %s", paste(unknown, collapse = ",")), call. = FALSE)
    }
    object$teaching_label <- factor(
      unname(mapping[cluster_ids]),
      levels = unname(mapping),
      ordered = FALSE
    )
    positive <- list(
      "Naive CD4 T" = c("CCR7", "LTB", "IL7R"),
      "CD14+ Mono" = c("LYZ", "S100A8", "S100A9", "LST1"),
      "Memory CD4 T" = c("IL7R", "LTB", "LDHB"),
      "B" = c("MS4A1", "CD79A", "CD37"),
      "CD8 T" = c("CD8A", "CCL5", "LTB"),
      "FCGR3A+ Mono" = c("FCGR3A", "LST1", "MS4A7"),
      "NK" = c("GNLY", "NKG7", "PRF1"),
      "DC" = c("FCER1A", "CST3", "CD1C"),
      "Platelet" = c("PPBP", "PF4", "NRGN")
    )
    contradictory <- list(
      "Naive CD4 T" = c("NKG7", "LST1", "MS4A1"),
      "CD14+ Mono" = c("CD3D", "MS4A1", "GNLY"),
      "Memory CD4 T" = c("NKG7", "LST1", "MS4A1"),
      "B" = c("CD3D", "LST1", "NKG7"),
      "CD8 T" = c("MS4A1", "LST1", "PPBP"),
      "FCGR3A+ Mono" = c("CD3D", "MS4A1", "PPBP"),
      "NK" = c("MS4A1", "LST1", "PPBP"),
      "DC" = c("CD3D", "MS4A1", "PPBP"),
      "Platelet" = c("CD3D", "MS4A1", "LST1")
    )
    all_marker_genes <- unique(c(unlist(positive, use.names = FALSE), unlist(contradictory, use.names = FALSE)))
    available <- intersect(all_marker_genes, rownames(object))
    averages <- Seurat::AverageExpression(
      object,
      assays = "RNA",
      features = available,
      group.by = "seurat_clusters",
      layer = "data",
      verbose = FALSE
    )$RNA
    evidence <- do.call(
      rbind,
      lapply(names(mapping), function(cluster) {
        label <- unname(mapping[[cluster]])
        cluster_column <- if (cluster %in% colnames(averages)) cluster else paste0("g", cluster)
        if (!cluster_column %in% colnames(averages)) {
          stop(sprintf("ANNOTATION_EVIDENCE_CLUSTER_MISSING: %s", cluster), call. = FALSE)
        }
        pos <- intersect(positive[[label]], rownames(averages))
        contra <- intersect(contradictory[[label]], rownames(averages))
        data.frame(
          cluster = cluster,
          teaching_label = label,
          annotation_status = "teaching_reference_requires_marker_review",
          positive_markers = paste(pos, collapse = ";"),
          positive_mean_log_normalized = if (length(pos)) mean(averages[pos, cluster_column]) else NA_real_,
          contradictory_markers = paste(contra, collapse = ";"),
          contradictory_mean_log_normalized = if (length(contra)) mean(averages[contra, cluster_column]) else NA_real_,
          claim_boundary = "single-library descriptive label; not donor-level inference",
          stringsAsFactors = FALSE
        )
      })
    )
    utils::write.csv(markers, file.path(stage_dir, "cluster_markers.csv"), row.names = FALSE)
    utils::write.csv(evidence, file.path(stage_dir, "annotation_evidence.csv"), row.names = FALSE)
    object
  }
)

# Materialize deterministic results from the last checkpoint.
tables_dir <- file.path(run_root, "05_results", "tables")
objects_dir <- file.path(run_root, "05_results", "objects")
original_dir <- file.path(run_root, "06_figures", "original")
final_dir <- file.path(run_root, "06_figures", "final")
for (path in c(tables_dir, objects_dir, original_dir, final_dir)) {
  dir.create(path, recursive = TRUE, showWarnings = FALSE)
}

qc_stage <- file.path(run_root, "04_intermediate", "SC04_QC_PER_CAPTURE")
import_stage <- file.path(run_root, "04_intermediate", "SC01_IMPORT_AND_IDENTITY")
annotation_stage <- file.path(run_root, "04_intermediate", "SC09_ANNOTATE_AND_REVIEW")
copy_artifact_if_changed(file.path(import_stage, "feature_name_mapping.csv"), file.path(tables_dir, "feature_name_mapping.csv"))
copy_artifact_if_changed(file.path(import_stage, "feature_name_mapping_summary.csv"), file.path(tables_dir, "feature_name_mapping_summary.csv"))
copy_artifact_if_changed(file.path(qc_stage, "qc_summary.csv"), file.path(tables_dir, "qc_summary.csv"))
copy_artifact_if_changed(file.path(qc_stage, "qc_cell_audit.csv"), file.path(tables_dir, "qc_cell_audit.csv"))
copy_artifact_if_changed(file.path(run_root, "04_intermediate", "SC08_GRAPH_CLUSTER_AND_EMBED", "cluster_sizes.csv"), file.path(tables_dir, "cluster_sizes.csv"))
copy_artifact_if_changed(file.path(run_root, "04_intermediate", "SC08_GRAPH_CLUSTER_AND_EMBED", "umap_runtime_contract.json"), file.path(tables_dir, "umap_runtime_contract.json"))
copy_artifact_if_changed(file.path(annotation_stage, "cluster_markers.csv"), file.path(tables_dir, "cluster_markers.csv"))
copy_artifact_if_changed(file.path(annotation_stage, "annotation_evidence.csv"), file.path(tables_dir, "annotation_evidence.csv"))

metadata <- pbmc[[]]
metadata$barcode <- rownames(metadata)
umap <- as.data.frame(Seurat::Embeddings(pbmc, reduction = "umap"))
umap$barcode <- rownames(umap)
cell_metadata <- merge(metadata, umap, by = "barcode", sort = FALSE)
atomic_csv(cell_metadata, file.path(tables_dir, "cell_metadata_with_umap.csv"))
atomic_rds(pbmc, file.path(objects_dir, "pbmc3k_annotated.rds"))

cluster_count <- length(unique(as.character(pbmc$seurat_clusters)))
metrics <- data.frame(
  metric = c("input_cells", "qc_retained_cells", "clusters"),
  value = c(expected_input, ncol(pbmc), cluster_count),
  expected = c(expected_input, expected_qc, expected_clusters),
  status = c("pass", ifelse(ncol(pbmc) == expected_qc, "pass", "fail"), ifelse(cluster_count == expected_clusters, "pass", "fail")),
  stringsAsFactors = FALSE
)
atomic_csv(metrics, file.path(tables_dir, "canonical_metrics.csv"))

cluster_palette <- c(
  "#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00",
  "#56B4E9", "#F0E442", "#000000", "#999999"
)
names(cluster_palette) <- as.character(0:8)
label_palette <- stats::setNames(cluster_palette, levels(pbmc$teaching_label))
visual <- params$visual

qc_original <- Seurat::VlnPlot(
  pbmc,
  features = c("nFeature_RNA", "nCount_RNA", "percent.mt"),
  ncol = 3,
  pt.size = 0.1
)
qc_final <- qc_original &
  ggplot2::theme_classic(base_size = as.numeric(visual$base_size_pt)) &
  ggplot2::theme(
    plot.title = ggplot2::element_text(face = "bold"),
    legend.position = visual$qc_legend_position
  )

pca_original <- Seurat::DimPlot(pbmc, reduction = "pca", group.by = "seurat_clusters")
pca_final <- Seurat::DimPlot(
  pbmc,
  reduction = "pca",
  group.by = "seurat_clusters",
  cols = unname(cluster_palette),
  pt.size = as.numeric(visual$umap_point_size)
) + ggplot2::theme_classic(base_size = as.numeric(visual$base_size_pt)) +
  ggplot2::labs(title = "PBMC3K PCA", color = "Cluster")

umap_original <- Seurat::DimPlot(pbmc, reduction = "umap", group.by = "seurat_clusters", label = TRUE)
umap_final <- Seurat::DimPlot(
  pbmc,
  reduction = "umap",
  group.by = "seurat_clusters",
  cols = unname(cluster_palette),
  pt.size = as.numeric(visual$umap_point_size),
  label = TRUE,
  label.size = as.numeric(visual$label_size),
  repel = TRUE
) + ggplot2::theme_classic(base_size = as.numeric(visual$base_size_pt)) +
  ggplot2::theme(legend.position = visual$legend_position) +
  ggplot2::labs(title = "PBMC3K clusters", color = "Cluster")

annotation_original <- Seurat::DimPlot(pbmc, reduction = "umap", group.by = "teaching_label", label = TRUE)
annotation_final <- Seurat::DimPlot(
  pbmc,
  reduction = "umap",
  group.by = "teaching_label",
  cols = unname(label_palette),
  pt.size = as.numeric(visual$umap_point_size),
  label = isTRUE(visual$annotation_direct_labels),
  label.size = as.numeric(visual$label_size),
  repel = TRUE
) + ggplot2::theme_classic(base_size = as.numeric(visual$base_size_pt)) +
  ggplot2::theme(legend.position = visual$legend_position) +
  ggplot2::labs(title = "PBMC3K teaching annotation", color = "Teaching label")

dot_features <- c("CCR7", "IL7R", "CD14", "LYZ", "MS4A1", "CD8A", "FCGR3A", "GNLY", "NKG7", "FCER1A", "CST3", "PPBP")
dot_features <- intersect(dot_features, rownames(pbmc))
dot_original <- Seurat::DotPlot(pbmc, features = dot_features, group.by = "teaching_label") + Seurat::RotatedAxis()
dot_final <- Seurat::DotPlot(
  pbmc,
  features = dot_features,
  group.by = "teaching_label",
  cols = c("#F7FBFF", "#CB181D"),
  dot.scale = 5
) + Seurat::RotatedAxis() +
  ggplot2::theme_classic(base_size = as.numeric(visual$base_size_pt)) +
  ggplot2::theme(axis.title = ggplot2::element_blank()) +
  ggplot2::labs(title = "Canonical marker overview", color = "Average expression", size = "Percent expressed")

plots <- list(
  qc_violin = list(original = qc_original, final = qc_final),
  pca_clusters = list(original = pca_original, final = pca_final),
  umap_clusters = list(original = umap_original, final = umap_final),
  umap_annotation = list(original = annotation_original, final = annotation_final),
  marker_dotplot = list(original = dot_original, final = dot_final)
)
for (name in names(plots)) {
  save_plot(
    plots[[name]]$original,
    file.path(original_dir, paste0(name, ".png")),
    as.numeric(visual$original_width_in),
    as.numeric(visual$original_height_in),
    as.integer(visual$dpi)
  )
  save_plot(
    plots[[name]]$final,
    file.path(final_dir, paste0(name, ".png")),
    as.numeric(visual$final_width_in),
    as.numeric(visual$final_height_in),
    as.integer(visual$dpi)
  )
}

execution_metrics <- list(
  schema_version = "1.0.0",
  case_id = "pbmc3k",
  analysis_signature = signature,
  environment = environment,
  canonical = list(input_cells = expected_input, qc_retained_cells = expected_qc, clusters = expected_clusters),
  observed = list(input_cells = expected_input, qc_retained_cells = ncol(pbmc), clusters = cluster_count),
  status = "data-verified-pending-native-review",
  claim_boundary = "single-library descriptive teaching result; no donor-level inference"
)
atomic_json(execution_metrics, file.path(tables_dir, "execution_metrics.json"))
delivery_checkpoint <- checkpoint_object(
  "SC13_FIGURES_AND_INTERPRETATION",
  "delivery_checkpoint.rds",
  run_root,
  signature,
  environment,
  function(stage_dir) {
    inventory <- data.frame(
      figure_id = names(plots),
      original = file.path("06_figures", "original", paste0(names(plots), ".png")),
      final = file.path("06_figures", "final", paste0(names(plots), ".png")),
      status = "rendered_pending_native_review",
      stringsAsFactors = FALSE
    )
    utils::write.csv(inventory, file.path(stage_dir, "figure_inventory.csv"), row.names = FALSE)
    list(
      case_id = "pbmc3k",
      analysis_signature = signature,
      canonical_metrics = execution_metrics$observed,
      figure_ids = names(plots),
      status = "rendered_pending_native_review"
    )
  }
)
message("PBMC3K pipeline completed; figures remain pending native visual review.")
execution_metrics_path <- file.path(tables_dir, "execution_metrics.json")
umap_runtime_contract_path <- file.path(tables_dir, "umap_runtime_contract.json")
delivery_checkpoint_path <- file.path(
  run_root,
  "04_intermediate",
  "SC13_FIGURES_AND_INTERPRETATION",
  "delivery_checkpoint.rds"
)
execution_metrics_sha256 <- sha256_file(execution_metrics_path)
umap_runtime_contract_sha256 <- sha256_file(umap_runtime_contract_path)
delivery_checkpoint_sha256 <- sha256_file(delivery_checkpoint_path)
writeLines(
  c(
    "stage=pbmc3k-r-pipeline",
    "status=complete",
    "shutdown_mode=native_exit",
    paste0("analysis_signature=", signature),
    paste0("execution_metrics_sha256=", execution_metrics_sha256),
    paste0("umap_runtime_contract_sha256=", umap_runtime_contract_sha256),
    paste0("delivery_checkpoint_sha256=", delivery_checkpoint_sha256)
  ),
  completion_marker,
  useBytes = TRUE
)
flush.console()
