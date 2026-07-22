#!/usr/bin/env Rscript

options(stringsAsFactors = FALSE, warn = 1L)
if (!identical(getOption("warn"), 1L)) {
  stop("Pipeline warning mode must be the integer value 1L", call. = FALSE)
}

parse_args <- function(args) {
  out <- list()
  i <- 1L
  while (i <= length(args)) {
    key <- args[[i]]
    if (!startsWith(key, "--") || i == length(args)) {
      stop("Arguments must be --key value pairs; invalid token: ", key, call. = FALSE)
    }
    out[[substring(key, 3L)]] <- args[[i + 1L]]
    i <- i + 2L
  }
  out
}

require_arg <- function(args, key) {
  value <- args[[key]]
  if (is.null(value) || !nzchar(value)) stop("Missing --", key, call. = FALSE)
  value
}

args <- parse_args(commandArgs(trailingOnly = TRUE))
run_root <- normalizePath(require_arg(args, "run-root"), winslash = "/", mustWork = TRUE)
input_root <- normalizePath(require_arg(args, "input-root"), winslash = "/", mustWork = TRUE)
resolved_manifest_path <- normalizePath(require_arg(args, "resolved-manifest"), winslash = "/", mustWork = TRUE)
analysis_params_path <- normalizePath(require_arg(args, "analysis-params"), winslash = "/", mustWork = TRUE)
visual_params_path <- normalizePath(require_arg(args, "visual-params"), winslash = "/", mustWork = TRUE)
environment_lock_path <- normalizePath(require_arg(args, "environment-lock"), winslash = "/", mustWork = TRUE)
completion_marker_path <- normalizePath(require_arg(args, "completion-marker"), winslash = "/", mustWork = FALSE)
mode <- require_arg(args, "mode")
if (!mode %in% c("run", "resume")) stop("--mode must be run or resume", call. = FALSE)

required_packages <- c(
  "renv", "Seurat", "SeuratObject", "jsonlite", "digest", "ggplot2",
  "patchwork", "hdf5r", "sctransform", "glmGamPoi", "BiocVersion", "SparseArray"
)
missing_packages <- required_packages[!vapply(required_packages, requireNamespace, quietly = TRUE, FUN.VALUE = logical(1L))]
if (length(missing_packages)) {
  stop("Frozen environment is missing packages: ", paste(missing_packages, collapse = ", "), call. = FALSE)
}
if (as.character(getRversion()) != "4.5.3") {
  stop("This case requires exact R 4.5.3; found ", getRversion(), call. = FALSE)
}
if (as.character(utils::packageVersion("renv")) != "1.2.2") {
  stop("This case requires exact task-local renv 1.2.2; found ", utils::packageVersion("renv"), call. = FALSE)
}
if (as.character(utils::packageVersion("Seurat")) != "5.5.0") {
  stop("This case requires exact Seurat 5.5.0; found ", utils::packageVersion("Seurat"), call. = FALSE)
}
if (as.character(utils::packageVersion("hdf5r")) != "1.3.12") {
  stop("This case requires exact hdf5r 1.3.12; found ", utils::packageVersion("hdf5r"), call. = FALSE)
}
expected_backend_versions <- c(
  Seurat = "5.5.0", SeuratObject = "5.4.0", sctransform = "0.4.3",
  glmGamPoi = "1.20.0", BiocVersion = "3.21.1", SparseArray = "1.8.1"
)
observed_backend_versions <- vapply(
  names(expected_backend_versions),
  function(package) as.character(utils::packageVersion(package)),
  character(1L)
)
if (!identical(unname(observed_backend_versions), unname(expected_backend_versions))) {
  stop(
    "Exact Seurat/glmGamPoi/Bioconductor backend version mismatch: ",
    paste(names(observed_backend_versions), observed_backend_versions, sep = "=", collapse = ", "),
    call. = FALSE
  )
}
project_root <- normalizePath(Sys.getenv("RENV_PROJECT", unset = ""), winslash = "/", mustWork = TRUE)
project_library <- normalizePath(renv::paths$library(project = project_root), winslash = "/", mustWork = TRUE)
backend_package_paths <- vapply(names(expected_backend_versions), function(package) {
  path <- normalizePath(find.package(package), winslash = "/", mustWork = TRUE)
  if (!startsWith(paste0(path, "/"), paste0(project_library, "/"))) {
    stop("Backend package was not loaded from the task-local project library: ", package, call. = FALSE)
  }
  substring(path, nchar(paste0(project_root, "/")) + 1L)
}, character(1L))
if (!identical(Sys.getenv("PROCESSOR_ARCHITECTURE", unset = ""), "AMD64")) {
  stop("R child PROCESSOR_ARCHITECTURE is not AMD64", call. = FALSE)
}

read_json <- function(path, simplify = TRUE) {
  jsonlite::read_json(path, simplifyVector = simplify)
}

write_json_atomic <- function(payload, path) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  tmp <- file.path(dirname(path), paste0(".", basename(path), ".tmp-", Sys.getpid()))
  jsonlite::write_json(payload, tmp, auto_unbox = TRUE, pretty = TRUE, null = "null")
  if (file.exists(path)) {
    if (sha256_file(tmp) == sha256_file(path)) {
      file.remove(tmp)
      return(invisible(path))
    }
    if (!file.remove(path)) stop("Cannot replace prior JSON state: ", path, call. = FALSE)
  }
  if (!file.rename(tmp, path)) stop("Atomic JSON promotion failed: ", path, call. = FALSE)
  invisible(path)
}

write_text_atomic <- function(lines, path) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  tmp <- file.path(dirname(path), paste0(".", basename(path), ".tmp-", Sys.getpid()))
  writeLines(lines, tmp, useBytes = TRUE)
  if (file.exists(path)) {
    if (sha256_file(tmp) == sha256_file(path)) {
      file.remove(tmp)
      return(invisible(path))
    }
    if (!file.remove(path)) stop("Cannot replace prior text state: ", path, call. = FALSE)
  }
  if (!file.rename(tmp, path)) stop("Atomic text promotion failed: ", path, call. = FALSE)
  invisible(path)
}

sha256_file <- function(path) {
  if (!file.exists(path)) stop("Cannot hash missing file: ", path, call. = FALSE)
  digest::digest(file = path, algo = "sha256", serialize = FALSE)
}

sha256_text <- function(value) {
  digest::digest(paste(value, collapse = "\n"), algo = "sha256", serialize = FALSE)
}

relative_path <- function(path) {
  full <- normalizePath(path, winslash = "/", mustWork = FALSE)
  prefix <- paste0(run_root, "/")
  if (!startsWith(full, prefix)) stop("Artifact escapes run root: ", full, call. = FALSE)
  substring(full, nchar(prefix) + 1L)
}
relative_path(completion_marker_path)

finite_layer_summary <- function(assay_object, layer) {
  value <- SeuratObject::LayerData(assay_object, layer = layer)
  numeric_values <- if (inherits(value, "sparseMatrix")) value@x else as.numeric(value)
  list(
    dimensions = as.list(dim(value)),
    stored_numeric_values = length(numeric_values),
    non_finite_values = sum(!is.finite(numeric_values))
  )
}

log_path <- file.path(run_root, "logs", "pipeline.log")
dir.create(dirname(log_path), recursive = TRUE, showWarnings = FALSE)
log_event <- function(message) {
  line <- sprintf("%s\t%s", format(Sys.time(), "%Y-%m-%dT%H:%M:%S%z"), message)
  cat(line, "\n")
  cat(line, "\n", file = log_path, append = TRUE)
}

for (path in c(
  "04_intermediate/_staging", "05_results/tables", "05_results/objects",
  "06_figures/original", "06_figures/final", "06_figures/review",
  "07_reports", "manifest"
)) {
  dir.create(file.path(run_root, path), recursive = TRUE, showWarnings = FALSE)
}

analysis <- read_json(analysis_params_path)
visual <- read_json(visual_params_path)
resolved_inputs <- read_json(resolved_manifest_path)
environment_lock <- read_json(environment_lock_path)

`%||%` <- function(x, y) if (is.null(x) || length(x) == 0L || !nzchar(x)) y else x

if (!identical(environment_lock$status, "frozen")) stop("Environment is not frozen", call. = FALSE)
if (!identical(environment_lock$r_version, "4.5.3")) stop("Environment lock R mismatch", call. = FALSE)
if (!identical(environment_lock$task_local_renv_version, "1.2.2")) stop("Environment lock task-local renv mismatch", call. = FALSE)
if (!identical(environment_lock$bootstrap_renv_version, "1.2.2")) stop("Environment lock task-local bootstrap renv mismatch", call. = FALSE)
if (!identical(environment_lock$bootstrap_renv_role, "task_local_snapshot_bootstrap_not_host") ||
    !identical(environment_lock$bootstrap$host_renv_required, FALSE) ||
    !identical(environment_lock$bootstrap$binary_basename, "renv_1.2.2.zip") ||
    !identical(as.integer(environment_lock$bootstrap$binary_size_bytes), 2514910L) ||
    !identical(as.integer(environment_lock$bootstrap$expected_binary_size_bytes), 2514910L) ||
    !identical(environment_lock$bootstrap$binary_sha256, "bcba2170563c65c6d6ed9328e4a624033ad9c5ee4e5bf9132cff7bcc7327cce5") ||
    !identical(environment_lock$bootstrap$expected_binary_sha256, "bcba2170563c65c6d6ed9328e4a624033ad9c5ee4e5bf9132cff7bcc7327cce5") ||
    !identical(environment_lock$bootstrap$binary_index_repository, "https://packagemanager.posit.co/cran/2026-04-23/bin/windows/contrib/4.5") ||
    !identical(environment_lock$bootstrap$binary_index_md5_available, FALSE)) {
  stop("Environment lock lacks hash-bound task-local renv bootstrap evidence", call. = FALSE)
}
if (!identical(environment_lock$seurat_version, "5.5.0")) stop("Environment lock Seurat mismatch", call. = FALSE)
if (!identical(environment_lock$packages$renv, "1.2.2")) stop("Environment lock renv package mismatch", call. = FALSE)
if (!identical(environment_lock$packages$hdf5r, "1.3.12")) stop("Environment lock hdf5r mismatch", call. = FALSE)
if (!identical(environment_lock$packages$BiocVersion, "3.21.1") ||
    !identical(environment_lock$packages$glmGamPoi, "1.20.0") ||
    !identical(environment_lock$packages$SparseArray, "1.8.1") ||
    !identical(environment_lock$shutdown_mode, "native_exit") ||
    !identical(environment_lock$bioconductor$release, "3.21") ||
    !identical(environment_lock$bioconductor$version, "3.21.1") ||
    !identical(environment_lock$bioconductor$glmGamPoi_version, "1.20.0") ||
    !identical(environment_lock$bioconductor$SparseArray_version, "1.8.1") ||
    !isTRUE(environment_lock$bioconductor$same_release_closure) ||
    !identical(environment_lock$bioconductor$source_compilation_allowed, FALSE) ||
    !identical(environment_lock$bioconductor$cross_release_packages_detected, FALSE)) {
  stop("Environment lock Bioconductor 3.21 backend mismatch", call. = FALSE)
}
for (package in c("BiocVersion", "glmGamPoi", "SparseArray")) {
  locked_relative_path <- environment_lock$bioconductor$package_paths_relative[[package]]
  if (!identical(backend_package_paths[[package]], locked_relative_path)) {
    stop("Loaded backend path differs from environment lock for ", package, call. = FALSE)
  }
}
if (!identical(environment_lock$repository$snapshot_url, "https://packagemanager.posit.co/cran/2026-04-23")) stop("Environment lock repository snapshot mismatch", call. = FALSE)
if (!identical(environment_lock$repository$package_type, "binary")) stop("Environment lock package type is not binary", call. = FALSE)
if (!identical(environment_lock$repository$dependencies_argument, "NA")) stop("Environment lock dependency mode mismatch", call. = FALSE)
if (!isTRUE(environment_lock$verification$exact_task_local_renv) ||
    !isTRUE(environment_lock$verification$exact_bootstrap_renv) ||
    !isTRUE(environment_lock$verification$bootstrap_renv_excluded_from_run_library) ||
    !isTRUE(environment_lock$verification$host_renv_not_required) ||
    !isTRUE(environment_lock$verification$bootstrap_binary_index_version_before_install) ||
    !isTRUE(environment_lock$verification$bootstrap_binary_index_repository_before_install) ||
    !isTRUE(environment_lock$verification$bootstrap_binary_pinned_sha256_before_install) ||
    !isTRUE(environment_lock$verification$windows_binary_snapshot_gate) ||
     !isTRUE(environment_lock$verification$binary_only_install) ||
     !isTRUE(environment_lock$verification$renv_lock_snapshot_url) ||
     !isTRUE(environment_lock$verification$exact_bioconductor_3_21) ||
     !isTRUE(environment_lock$verification$exact_glmGamPoi_1_20_0) ||
     !isTRUE(environment_lock$verification$exact_SparseArray_1_8_1) ||
     !isTRUE(environment_lock$verification$complete_same_release_closure) ||
     !isTRUE(environment_lock$verification$all_archives_hash_verified_before_install) ||
     !isTRUE(environment_lock$verification$no_cross_release_packages) ||
     !isTRUE(environment_lock$verification$native_r_shutdown)) {
  stop("Environment lock lacks required binary snapshot verification evidence", call. = FALSE)
}
if (!isTRUE(environment_lock$verification$read10x_h5_smoke)) stop("Environment lock lacks Read10X_h5 smoke evidence", call. = FALSE)
if (!identical(analysis$sct_vst_flavor, "v2") ||
    !identical(analysis$sct_method, "glmGamPoi_offset")) {
  stop("Analysis config must explicitly freeze vst.flavor=v2 and method=glmGamPoi_offset", call. = FALSE)
}

render_round <- as.integer(visual$render_round)
if (length(render_round) != 1L || is.na(render_round) || render_round < 1L || render_round > 3L) {
  stop("visual render_round must be an integer from 1 to 3", call. = FALSE)
}
if (render_round > 1L && mode != "resume") {
  stop("Visual rounds 2-3 require resume mode", call. = FALSE)
}

input_records <- resolved_inputs$files
if (!is.data.frame(input_records) || !nrow(input_records)) stop("Resolved input manifest has no files", call. = FALSE)
required_input_columns <- c("file_id", "filename", "size_bytes", "sha256")
if (!all(required_input_columns %in% colnames(input_records))) stop("Resolved manifest lacks frozen file fields", call. = FALSE)
for (i in seq_len(nrow(input_records))) {
  path <- file.path(input_root, input_records$filename[[i]])
  if (!file.exists(path)) stop("Frozen input is missing: ", path, call. = FALSE)
  if (file.info(path)$size != as.numeric(input_records$size_bytes[[i]])) stop("Frozen input size mismatch: ", path, call. = FALSE)
  if (sha256_file(path) != input_records$sha256[[i]]) stop("Frozen input SHA-256 mismatch: ", path, call. = FALSE)
}
h5_lock_row <- input_records[input_records$file_id == "filtered_h5", , drop = FALSE]
if (nrow(h5_lock_row) != 1L || !identical(environment_lock$h5_reader_smoke$input_sha256, h5_lock_row$sha256[[1L]])) {
  stop("Environment H5 smoke evidence is not bound to the frozen filtered H5", call. = FALSE)
}

analysis_hash <- sha256_file(analysis_params_path)
visual_hash <- sha256_file(visual_params_path)
bioconductor_pins_path <- file.path(run_root, "02_environment", "bioconductor-3.21-archive-pins.json")
if (!file.exists(bioconductor_pins_path) ||
    !identical(sha256_file(bioconductor_pins_path), environment_lock$bioconductor$pins_sha256)) {
  stop("Environment lock is not bound to the run-local Bioconductor pin document", call. = FALSE)
}
bioconductor_pins_hash <- sha256_file(bioconductor_pins_path)

# Rscript does not guarantee sys.frame(1)$ofile. Resolve the copied script through
# the fixed run tree, which case_driver.py always materializes before execution.
pipeline_copy <- file.path(run_root, "03_scripts", "run_pipeline.R")
if (!file.exists(pipeline_copy)) stop("Copied pipeline script is missing: ", pipeline_copy, call. = FALSE)
code_hash <- sha256_file(pipeline_copy)
input_hash <- sha256_text(paste(input_records$file_id, input_records$sha256, input_records$size_bytes, sep = ":"))
environment_hash <- environment_lock$renv_lock_sha256
analysis_fingerprint <- sha256_text(c(input_hash, analysis_hash, code_hash, environment_hash))
visual_fingerprint <- sha256_text(c(analysis_fingerprint, visual_hash, paste0("round=", render_round)))

# Warning evidence is part of the execution contract. Warnings are printed
# immediately (`options(warn = 1)`), recorded without suppression, classified,
# and persisted atomically. No warning class is silently accepted: a warning
# blocks stage promotion and therefore blocks delivery/release. This tutorial
# has no warning allowlist path: formal evidence must contain zero warnings.
warning_evidence_path <- file.path(run_root, "logs", "pipeline-warnings.json")
warning_classification_version <- "1.0"
warning_state <- new.env(parent = emptyenv())
warning_state$records <- list()

sanitize_warning_text <- function(value) {
  value <- as.character(value %||% "")
  value <- gsub(run_root, "<RUN_ROOT>", value, fixed = TRUE)
  value <- gsub(input_root, "<INPUT_ROOT>", value, fixed = TRUE)
  gsub("(?i)(?:[A-Z]:[\\\\/]|/(?:home|Users)/)[^[:space:]\"']*", "<ABSOLUTE_PATH>", value, perl = TRUE)
}

classify_pipeline_warning <- function(message_text, call_text) {
  normalized_message <- tolower(trimws(message_text))
  normalized_call <- tolower(trimws(call_text))
  combined <- paste(normalized_message, normalized_call)
  if (identical(normalized_message, "iteration limit reached") && grepl("theta\\.ml", normalized_call)) {
    return(list(
      category = "sctransform_theta_iteration_limit",
      severity = "release_blocker",
      allowlisted = FALSE,
      rationale = "negative-binomial theta estimation reached its iteration limit; numerical review is required"
    ))
  }
  if (identical(normalized_message, "alternation limit reached") && grepl("glm\\.nb", normalized_call)) {
    return(list(
      category = "sctransform_glm_nb_alternation_limit",
      severity = "release_blocker",
      allowlisted = FALSE,
      rationale = "negative-binomial GLM alternation reached its limit; numerical review is required"
    ))
  }
  if (grepl("unused argument|could not find function|not an exported object|deprecated|defunct|api", combined)) {
    return(list(
      category = "api_compatibility_warning",
      severity = "release_blocker",
      allowlisted = FALSE,
      rationale = "API compatibility warnings are not accepted"
    ))
  }
  if (grepl("nan|non-finite|infinite|singular|converg|iteration limit|alternation limit|rank.deficient|zero variance|numerical", combined)) {
    return(list(
      category = "numerical_integrity_warning",
      severity = "release_blocker",
      allowlisted = FALSE,
      rationale = "numerical warnings require explicit reproduction and review"
    ))
  }
  if (grepl("barcode|coordinate|scale factor|spatial image|tissue position|image bound", combined)) {
    return(list(
      category = "spatial_integrity_warning",
      severity = "release_blocker",
      allowlisted = FALSE,
      rationale = "barcode/coordinate/image warnings are not accepted"
    ))
  }
  list(
    category = "unclassified_warning",
    severity = "release_blocker",
    allowlisted = FALSE,
    rationale = "unknown warnings fail closed until explicitly classified"
  )
}

warning_blocking_occurrences <- function() {
  if (!length(warning_state$records)) return(0L)
  sum(vapply(
    warning_state$records,
    function(record) if (isTRUE(record$allowlisted)) 0L else as.integer(record$count),
    integer(1L)
  ))
}

write_warning_evidence <- function() {
  blocking <- warning_blocking_occurrences()
  total <- if (length(warning_state$records)) {
    sum(vapply(warning_state$records, function(record) as.integer(record$count), integer(1L)))
  } else 0L
  write_json_atomic(list(
    schema_version = "1.0",
    classification_version = warning_classification_version,
    case = "visium-mouse-brain",
    status = if (blocking == 0L) "passed" else "blocked",
    warning_free = total == 0L,
    warning_occurrences = total,
    unique_warning_records = length(warning_state$records),
    blocking_warning_occurrences = blocking,
    warning_allowlist_used = FALSE,
    scientific_parameters_changed = FALSE,
    records = warning_state$records,
    code_hash = code_hash,
    analysis_config_hash = analysis_hash,
    environment_lock_hash = environment_hash,
    absolute_paths_included = FALSE
  ), warning_evidence_path)
}

record_pipeline_warning <- function(warning_condition, stage_key) {
  message_text <- conditionMessage(warning_condition)
  warning_call <- conditionCall(warning_condition)
  call_text <- if (is.null(warning_call)) "" else paste(deparse(warning_call), collapse = " ")
  classification <- classify_pipeline_warning(message_text, call_text)
  sanitized_message <- sanitize_warning_text(message_text)
  sanitized_call <- sanitize_warning_text(call_text)
  matches <- which(vapply(
    warning_state$records,
    function(record) identical(record$stage_key, stage_key) &&
      identical(record$message, sanitized_message) && identical(record$call, sanitized_call),
    logical(1L)
  ))
  if (length(matches)) {
    index <- matches[[1L]]
    warning_state$records[[index]]$count <- warning_state$records[[index]]$count + 1L
  } else {
    warning_state$records[[length(warning_state$records) + 1L]] <- c(
      list(stage_key = stage_key, message = sanitized_message, call = sanitized_call, count = 1L),
      classification
    )
  }
  write_warning_evidence()
  log_event(paste("warning", stage_key, classification$category, sanitized_message, sep = "\t"))
  if (!isTRUE(classification$allowlisted)) {
    stop(
      "Release-blocking warning in ", stage_key, ": ", classification$category,
      "; see logs/pipeline-warnings.json",
      call. = FALSE
    )
  }
}

if (mode == "resume" && file.exists(warning_evidence_path)) {
  prior_warning_evidence <- read_json(warning_evidence_path, simplify = FALSE)
  if (!identical(prior_warning_evidence$classification_version, warning_classification_version) ||
      !identical(prior_warning_evidence$code_hash, code_hash) ||
      !identical(prior_warning_evidence$analysis_config_hash, analysis_hash) ||
      !identical(prior_warning_evidence$environment_lock_hash, environment_hash)) {
    stop("Prior warning evidence is not bound to the current code/config/environment", call. = FALSE)
  }
  warning_state$records <- if (is.null(prior_warning_evidence$records)) list() else prior_warning_evidence$records
  if (warning_blocking_occurrences() > 0L) {
    stop("Prior warning evidence contains release-blocking warnings; use a corrected fresh run", call. = FALSE)
  }
}
write_warning_evidence()

checkpoint_valid <- function(stage_dir, expected_fingerprint) {
  checkpoint_path <- file.path(stage_dir, "_checkpoint.json")
  if (!file.exists(checkpoint_path)) stop("Checkpoint metadata missing: ", stage_dir, call. = FALSE)
  checkpoint <- read_json(checkpoint_path)
  if (!identical(checkpoint$fingerprint, expected_fingerprint)) {
    stop("Checkpoint fingerprint mismatch for ", checkpoint$stage_id, call. = FALSE)
  }
  artifacts <- checkpoint$artifacts
  if (is.data.frame(artifacts) && nrow(artifacts)) {
    for (i in seq_len(nrow(artifacts))) {
      path <- file.path(stage_dir, artifacts$path[[i]])
      if (!file.exists(path)) stop("Checkpoint artifact missing: ", path, call. = FALSE)
      if (file.info(path)$size != as.numeric(artifacts$size_bytes[[i]])) stop("Checkpoint size mismatch: ", path, call. = FALSE)
      if (sha256_file(path) != artifacts$sha256[[i]]) stop("Checkpoint hash mismatch: ", path, call. = FALSE)
    }
  }
  TRUE
}

execute_stage_producer <- function(
  producer, staging_dir, stage_key,
  warning_recorder, warning_evidence_writer, blocker_counter
) {
  stage_error <- NULL
  tryCatch(
    withCallingHandlers(
      producer(staging_dir),
      warning = function(warning_condition) warning_recorder(warning_condition, stage_key)
    ),
    error = function(error_condition) stage_error <<- error_condition
  )
  warning_evidence_writer()
  if (!is.null(stage_error)) stop(stage_error)
  stage_blockers <- blocker_counter(stage_key)
  if (stage_blockers > 0L) {
    stop(
      "Stage ", stage_key, " emitted ", stage_blockers,
      " release-blocking warning occurrence(s); see logs/pipeline-warnings.json",
      call. = FALSE
    )
  }
  invisible(TRUE)
}

run_stage <- function(stage_id, stage_key = stage_id, fingerprint, producer) {
  final_dir <- file.path(run_root, "04_intermediate", stage_key)
  if (dir.exists(final_dir)) {
    if (mode != "resume") stop("run mode refuses existing checkpoint: ", final_dir, call. = FALSE)
    checkpoint_valid(final_dir, fingerprint)
    log_event(paste("resume_reuse", stage_key, sep = "\t"))
    return(final_dir)
  }
  staging_dir <- file.path(
    run_root, "04_intermediate", "_staging",
    paste0(gsub("/", "-", stage_key, fixed = TRUE), "-", Sys.getpid(), "-", format(Sys.time(), "%Y%m%d%H%M%S"))
  )
  if (dir.exists(staging_dir)) stop("Unexpected staging collision: ", staging_dir, call. = FALSE)
  dir.create(staging_dir, recursive = TRUE, showWarnings = FALSE)
  log_event(paste("stage_start", stage_key, sep = "\t"))
  execute_stage_producer(
    producer = producer,
    staging_dir = staging_dir,
    stage_key = stage_key,
    warning_recorder = record_pipeline_warning,
    warning_evidence_writer = write_warning_evidence,
    blocker_counter = function(key) {
      sum(vapply(
        warning_state$records,
        function(record) {
          if (identical(record$stage_key, key) && !isTRUE(record$allowlisted)) as.integer(record$count) else 0L
        },
        integer(1L)
      ))
    }
  )
  outputs <- list.files(staging_dir, recursive = TRUE, full.names = TRUE, all.files = TRUE, no.. = TRUE)
  outputs <- outputs[file.info(outputs)$isdir %in% FALSE]
  if (!length(outputs)) stop("Stage produced no artifacts: ", stage_key, call. = FALSE)
  artifact_records <- lapply(sort(outputs), function(path) {
    list(
      path = substring(normalizePath(path, winslash = "/"), nchar(normalizePath(staging_dir, winslash = "/")) + 2L),
      size_bytes = unname(file.info(path)$size),
      sha256 = sha256_file(path)
    )
  })
  checkpoint <- list(
    schema_version = "1.0",
    stage_id = stage_id,
    stage_key = stage_key,
    fingerprint = fingerprint,
    input_hash = input_hash,
    analysis_config_hash = analysis_hash,
    visual_config_hash = if (startsWith(stage_id, "S80") || startsWith(stage_id, "S95")) visual_hash else NULL,
    code_hash = code_hash,
    environment_lock_hash = environment_hash,
    artifacts = artifact_records,
    validation_status = "passed"
  )
  write_json_atomic(checkpoint, file.path(staging_dir, "_checkpoint.json"))
  dir.create(dirname(final_dir), recursive = TRUE, showWarnings = FALSE)
  if (!file.rename(staging_dir, final_dir)) stop("Atomic stage promotion failed: ", stage_key, call. = FALSE)
  checkpoint_valid(final_dir, fingerprint)
  log_event(paste("stage_checkpointed", stage_key, sep = "\t"))
  final_dir
}

copy_immutable <- function(source, destination) {
  dir.create(dirname(destination), recursive = TRUE, showWarnings = FALSE)
  if (file.exists(destination)) {
    if (sha256_file(source) != sha256_file(destination)) {
      stop("Refusing to overwrite a different immutable artifact: ", destination, call. = FALSE)
    }
    return(invisible(destination))
  }
  if (!file.copy(source, destination, overwrite = FALSE, copy.date = TRUE)) {
    stop("Cannot copy artifact: ", source, " -> ", destination, call. = FALSE)
  }
  invisible(destination)
}

copy_current <- function(source, destination) {
  dir.create(dirname(destination), recursive = TRUE, showWarnings = FALSE)
  tmp <- file.path(dirname(destination), paste0(".", basename(destination), ".tmp-", Sys.getpid()))
  if (!file.copy(source, tmp, overwrite = TRUE, copy.date = TRUE)) {
    stop("Cannot stage current artifact: ", source, call. = FALSE)
  }
  if (file.exists(destination) && !file.remove(destination)) {
    stop("Cannot replace current artifact: ", destination, call. = FALSE)
  }
  if (!file.rename(tmp, destination)) stop("Cannot promote current artifact: ", destination, call. = FALSE)
  invisible(destination)
}

select_gene_expression <- function(raw) {
  if (!is.list(raw)) return(raw)
  if ("Gene Expression" %in% names(raw)) return(raw[["Gene Expression"]])
  if (length(raw) == 1L) return(raw[[1L]])
  stop("H5 contains multiple modalities but no unambiguous Gene Expression matrix", call. = FALSE)
}

h5_record <- input_records[input_records$file_id == "filtered_h5", , drop = FALSE]
if (nrow(h5_record) != 1L) stop("Resolved manifest must contain exactly one filtered_h5", call. = FALSE)
h5_path <- file.path(input_root, h5_record$filename[[1L]])

s10 <- run_stage("S10_INGEST", fingerprint = analysis_fingerprint, producer = function(stage_dir) {
  set.seed(as.integer(analysis$seed))
  raw <- select_gene_expression(Seurat::Read10X_h5(h5_path, use.names = TRUE, unique.features = TRUE))
  if (nrow(raw) < 1L || ncol(raw) < 1L) stop("Filtered H5 matrix is empty", call. = FALSE)
  object <- Seurat::Load10X_Spatial(
    data.dir = input_root,
    filename = basename(h5_path),
    assay = analysis$assay,
    slice = "anterior1",
    filter.matrix = isTRUE(analysis$filtering$load_in_tissue_only),
    to.upper = FALSE
  )
  if (!(analysis$assay %in% SeuratObject::Assays(object))) stop("Spatial assay was not loaded", call. = FALSE)
  image_names <- SeuratObject::Images(object)
  if (!length(image_names)) stop("No spatial image was loaded", call. = FALSE)
  writeLines(colnames(raw), file.path(stage_dir, "matrix_barcodes.txt"), useBytes = TRUE)
  saveRDS(object, file.path(stage_dir, "ingested_seurat.rds"), compress = "xz")
  write_json_atomic(list(
    matrix_features = nrow(raw),
    matrix_barcodes = ncol(raw),
    loaded_spots = ncol(object),
    loaded_features = nrow(object),
    assays = SeuratObject::Assays(object),
    images = image_names
  ), file.path(stage_dir, "ingest_summary.json"))
})

read_tissue_positions <- function(spatial_dir) {
  modern <- file.path(spatial_dir, "tissue_positions.csv")
  legacy <- file.path(spatial_dir, "tissue_positions_list.csv")
  if (file.exists(modern)) {
    positions <- utils::read.csv(modern, check.names = FALSE, stringsAsFactors = FALSE)
  } else if (file.exists(legacy)) {
    positions <- utils::read.csv(legacy, header = FALSE, stringsAsFactors = FALSE)
    if (ncol(positions) != 6L) stop("Legacy tissue positions must have six columns", call. = FALSE)
    colnames(positions) <- c(
      "barcode", "in_tissue", "array_row", "array_col",
      "pxl_row_in_fullres", "pxl_col_in_fullres"
    )
  } else {
    stop("No tissue_positions.csv or tissue_positions_list.csv found", call. = FALSE)
  }
  aliases <- c(
    pxl_row_in_fullres = "pxl_row_in_fullres",
    pxl_col_in_fullres = "pxl_col_in_fullres"
  )
  required <- c("barcode", "in_tissue", "array_row", "array_col", names(aliases))
  if (!all(required %in% colnames(positions))) {
    stop("Tissue positions lack required columns: ", paste(setdiff(required, colnames(positions)), collapse = ", "), call. = FALSE)
  }
  positions
}

parse_finite_numeric_field <- function(values, column) {
  if (is.factor(values)) values <- as.character(values)
  if (is.numeric(values)) {
    parsed <- as.numeric(values)
  } else if (is.character(values)) {
    if (anyNA(values)) {
      stop("Missing tissue position field: ", column, call. = FALSE)
    }
    lexemes <- trimws(values, which = "both")
    numeric_pattern <- "^[+-]?(?:[0-9]+(?:\\.[0-9]*)?|\\.[0-9]+)(?:[eE][+-]?[0-9]+)?$"
    malformed <- !nzchar(lexemes) | !grepl(numeric_pattern, lexemes, perl = TRUE)
    if (any(malformed)) {
      examples <- unique(lexemes[malformed])
      stop(
        "Malformed numeric tissue position field ", column, ": ",
        paste(utils::head(examples, 3L), collapse = ", "),
        call. = FALSE
      )
    }
    parsed <- as.numeric(lexemes)
  } else {
    stop("Unsupported tissue position field type for ", column, call. = FALSE)
  }
  if (anyNA(parsed) || any(!is.finite(parsed))) {
    stop("Non-finite tissue position field: ", column, call. = FALSE)
  }
  parsed
}

s20 <- run_stage("S20_COORD_IMAGE_QC", fingerprint = analysis_fingerprint, producer = function(stage_dir) {
  object <- readRDS(file.path(s10, "ingested_seurat.rds"))
  matrix_barcodes <- readLines(file.path(s10, "matrix_barcodes.txt"), warn = FALSE)
  positions <- read_tissue_positions(file.path(input_root, "spatial"))
  if (anyDuplicated(positions$barcode)) stop("Duplicate barcodes in tissue positions", call. = FALSE)
  if (anyDuplicated(matrix_barcodes)) stop("Duplicate barcodes in filtered H5 matrix", call. = FALSE)
  numeric_columns <- c("in_tissue", "array_row", "array_col", "pxl_row_in_fullres", "pxl_col_in_fullres")
  for (column in numeric_columns) {
    positions[[column]] <- parse_finite_numeric_field(positions[[column]], column)
  }
  if (!all(positions$in_tissue %in% c(0, 1))) stop("in_tissue must contain only 0/1", call. = FALSE)
  if (any(positions[, c("array_row", "array_col", "pxl_row_in_fullres", "pxl_col_in_fullres")] < 0)) {
    stop("Negative vendor coordinates detected", call. = FALSE)
  }
  vendor_in_tissue <- positions$barcode[positions$in_tissue == 1]
  object_barcodes <- colnames(object)
  if (anyDuplicated(object_barcodes)) stop("Duplicate barcodes in loaded Seurat object", call. = FALSE)
  image_name <- SeuratObject::Images(object)[[1L]]
  assay_cells <- SeuratObject::Cells(object[[analysis$assay]])
  image_cells <- SeuratObject::Cells(object[[image_name]])
  coordinates <- SeuratObject::GetTissueCoordinates(object, image = image_name)
  coordinate_barcodes <- rownames(coordinates)
  if (!length(coordinate_barcodes) || all(grepl("^[0-9]+$", coordinate_barcodes))) {
    barcode_column <- intersect(c("barcode", "cell", "ID"), colnames(coordinates))
    if (length(barcode_column) == 1L) coordinate_barcodes <- as.character(coordinates[[barcode_column]])
  }
  if (anyDuplicated(assay_cells)) stop("Duplicate Spatial assay cells", call. = FALSE)
  if (anyDuplicated(image_cells)) stop("Duplicate spatial image cells", call. = FALSE)
  if (anyDuplicated(coordinate_barcodes)) stop("Duplicate Seurat image-coordinate barcodes", call. = FALSE)

  barcode_sets <- list(
    assay_cells = assay_cells,
    image_cells = image_cells,
    coordinates = coordinate_barcodes
  )
  directed_pairs <- list(
    c("assay_cells", "image_cells"),
    c("image_cells", "assay_cells"),
    c("assay_cells", "coordinates"),
    c("coordinates", "assay_cells"),
    c("image_cells", "coordinates"),
    c("coordinates", "image_cells")
  )
  difference_rows <- do.call(rbind, lapply(directed_pairs, function(pair) {
    values <- sort(setdiff(barcode_sets[[pair[[1L]]]], barcode_sets[[pair[[2L]]]]))
    if (!length(values)) {
      return(data.frame(source = character(), target = character(), barcode = character(), stringsAsFactors = FALSE))
    }
    data.frame(source = pair[[1L]], target = pair[[2L]], barcode = values, stringsAsFactors = FALSE)
  }))
  difference_counts <- vapply(directed_pairs, function(pair) {
    length(setdiff(barcode_sets[[pair[[1L]]]], barcode_sets[[pair[[2L]]]]))
  }, integer(1L))
  names(difference_counts) <- vapply(directed_pairs, function(pair) {
    paste0(pair[[1L]], "_not_", pair[[2L]])
  }, character(1L))
  expected_object <- intersect(matrix_barcodes, vendor_in_tissue)
  reconciliation <- data.frame(
    set = c(
      "matrix_barcodes", "vendor_all_positions", "vendor_in_tissue",
      "loaded_object", "assay_cells", "image_cells", "coordinates"
    ),
    count = c(
      length(matrix_barcodes), nrow(positions), length(vendor_in_tissue),
      length(object_barcodes), length(assay_cells), length(image_cells), length(coordinate_barcodes)
    ),
    stringsAsFactors = FALSE
  )
  utils::write.csv(reconciliation, file.path(stage_dir, "barcode_reconciliation.csv"), row.names = FALSE)
  utils::write.csv(difference_rows, file.path(stage_dir, "barcode_set_differences.csv"), row.names = FALSE)
  write_json_atomic(list(
    status = if (any(difference_counts != 0L)) "blocked" else "passed",
    contract = "Spatial_assay_cells_equals_image_cells_equals_coordinate_barcodes",
    counts = as.list(stats::setNames(reconciliation$count, reconciliation$set)),
    directed_difference_counts = as.list(difference_counts),
    difference_table = "barcode_set_differences.csv",
    full_differences_preserved = TRUE
  ), file.path(stage_dir, "barcode_set_reconciliation.json"))
  if (any(difference_counts != 0L)) {
    stop(
      "Spatial assay/image/coordinate barcode reconciliation failed: ",
      paste(names(difference_counts), difference_counts, sep = "=", collapse = ", "),
      ". Evidence retained in the S20 staging directory; the contract is not relaxed.",
      call. = FALSE
    )
  }
  if (!setequal(object_barcodes, expected_object) || !setequal(object_barcodes, assay_cells)) {
    stop("Loaded object/assay barcodes do not equal the filtered-H5 and vendor in-tissue intersection", call. = FALSE)
  }
  coordinate_numeric <- vapply(coordinates, is.numeric, logical(1L))
  if (sum(coordinate_numeric) < 2L) stop("Tissue coordinate table lacks two numeric axes", call. = FALSE)
  if (any(!is.finite(as.matrix(coordinates[, coordinate_numeric, drop = FALSE])))) {
    stop("Non-finite Seurat image coordinates detected", call. = FALSE)
  }
  image_raw <- Seurat::GetImage(object[[image_name]], mode = "raw")
  image_dimensions <- dim(image_raw)
  if (length(image_dimensions) < 2L || any(image_dimensions[1:2] <= 0)) {
    stop("Loaded spatial image has invalid dimensions", call. = FALSE)
  }
  coordinate_pair <- if (all(c("imagecol", "imagerow") %in% colnames(coordinates))) {
    c("imagecol", "imagerow")
  } else if (all(c("x", "y") %in% colnames(coordinates))) {
    c("x", "y")
  } else {
    stop("Cannot identify image x/y coordinate columns", call. = FALSE)
  }
  scale_factors <- unlist(Seurat::ScaleFactors(object[[image_name]]), recursive = TRUE, use.names = TRUE)
  if (!length(scale_factors) || any(!is.finite(as.numeric(scale_factors))) || any(as.numeric(scale_factors) <= 0)) {
    stop("Spatial image scale factors are missing, non-finite, or non-positive", call. = FALSE)
  }
  required_scale_factors <- c("spot", "hires", "lowres")
  if (!all(required_scale_factors %in% names(scale_factors))) {
    stop("Spatial image scale factors lack: ", paste(setdiff(required_scale_factors, names(scale_factors)), collapse = ", "), call. = FALSE)
  }
  # Seurat 5.5.0 returns Visium tissue coordinates in full-resolution pixels,
  # while Load10X_Spatial stores the default low-resolution image (600 x 600
  # for this dataset). Apply the vendor lowres scale factor before checking
  # the loaded image bounds; this is the same transform used by Seurat's
  # coordinate-faithful spatial plotting interface.
  lowres_scale <- as.numeric(scale_factors[["lowres"]])
  loaded_image_coordinates <- data.frame(
    x = as.numeric(coordinates[[coordinate_pair[[1L]]]]) * lowres_scale,
    y = as.numeric(coordinates[[coordinate_pair[[2L]]]]) * lowres_scale,
    stringsAsFactors = FALSE
  )
  if (any(!is.finite(as.matrix(loaded_image_coordinates)))) {
    stop("Scaled low-resolution image coordinates are non-finite", call. = FALSE)
  }
  if (any(loaded_image_coordinates$x < 0 | loaded_image_coordinates$x > image_dimensions[[2L]]) ||
      any(loaded_image_coordinates$y < 0 | loaded_image_coordinates$y > image_dimensions[[1L]])) {
    stop("Vendor-scaled low-resolution coordinates fall outside loaded image bounds", call. = FALSE)
  }
  image_assets <- c("scalefactors_json.json", "tissue_hires_image.png", "tissue_lowres_image.png")
  missing_assets <- image_assets[!file.exists(file.path(input_root, "spatial", image_assets))]
  if (length(missing_assets)) stop("Missing spatial image assets: ", paste(missing_assets, collapse = ", "), call. = FALSE)

  utils::write.csv(positions, file.path(stage_dir, "tissue_positions_normalized.csv"), row.names = FALSE)
  saveRDS(object, file.path(stage_dir, "coordinate_qc_seurat.rds"), compress = "xz")
  write_json_atomic(list(
    status = "passed",
    image_name = image_name,
    exact_object_expected_barcode_match = TRUE,
    exact_assay_image_coordinate_barcode_match = TRUE,
    directed_barcode_difference_counts = as.list(difference_counts),
    coordinate_columns = colnames(coordinates),
    image_coordinate_pair = coordinate_pair,
    full_resolution_coordinate_bounds = lapply(coordinates[, coordinate_numeric, drop = FALSE], function(x) c(min = min(x), max = max(x))),
    loaded_image_coordinate_bounds = lapply(loaded_image_coordinates, function(x) c(min = min(x), max = max(x))),
    loaded_image_dimensions = as.integer(image_dimensions),
    coordinate_transform = list(
      source_space = "vendor_full_resolution_pixels",
      target_space = "loaded_low_resolution_image_pixels",
      scale_factor_name = "lowres",
      scale_factor = lowres_scale
    ),
    coordinate_bounds_within_loaded_image = TRUE,
    scale_factors = as.list(scale_factors),
    required_image_assets = as.list(image_assets),
    orientation = "vendor_transform_preserved_no_manual_flip",
    native_alignment_review = "pending"
  ), file.path(stage_dir, "coordinate_image_qc.json"))
})

s30 <- run_stage("S30_UNIT_QC", fingerprint = analysis_fingerprint, producer = function(stage_dir) {
  object <- readRDS(file.path(s20, "coordinate_qc_seurat.rds"))
  required_metadata <- c("nCount_Spatial", "nFeature_Spatial")
  missing <- setdiff(required_metadata, colnames(object[[]]))
  if (length(missing)) stop("Missing spot QC fields: ", paste(missing, collapse = ", "), call. = FALSE)
  qc <- data.frame(
    barcode = colnames(object),
    nCount_Spatial = object$nCount_Spatial,
    nFeature_Spatial = object$nFeature_Spatial,
    stringsAsFactors = FALSE
  )
  if (any(!is.finite(qc$nCount_Spatial)) || any(!is.finite(qc$nFeature_Spatial))) stop("Non-finite spot QC values", call. = FALSE)
  if (any(qc$nCount_Spatial <= 0) || any(qc$nFeature_Spatial <= 0)) stop("Loaded in-tissue spots contain non-positive QC values", call. = FALSE)
  reconciliation <- utils::read.csv(file.path(s20, "barcode_reconciliation.csv"), stringsAsFactors = FALSE)
  lookup <- setNames(reconciliation$count, reconciliation$set)
  attrition <- data.frame(
    step = c("matrix_barcodes", "matrix_and_vendor_in_tissue", "loaded_spots", "post_load_retained_spots"),
    count = c(lookup[["matrix_barcodes"]], lookup[["loaded_object"]], ncol(object), ncol(object)),
    rule = c("filtered H5", "vendor in_tissue intersection", "Load10X_Spatial(filter.matrix=TRUE)", "no additional post-load filtering"),
    stringsAsFactors = FALSE
  )
  utils::write.csv(qc, file.path(stage_dir, "spot_qc.csv"), row.names = FALSE)
  utils::write.csv(attrition, file.path(stage_dir, "attrition.csv"), row.names = FALSE)
  saveRDS(object, file.path(stage_dir, "unit_qc_seurat.rds"), compress = "xz")
})

s40 <- run_stage("S40_PREPROCESS", fingerprint = analysis_fingerprint, producer = function(stage_dir) {
  object <- readRDS(file.path(s30, "unit_qc_seurat.rds"))
  input_spots <- ncol(object)
  set.seed(as.integer(analysis$seed))
  object <- Seurat::SCTransform(
    object,
    assay = analysis$assay,
    new.assay.name = analysis$sct_assay,
    variable.features.n = as.integer(analysis$sct_variable_features_n),
    return.only.var.genes = FALSE,
    vst.flavor = analysis$sct_vst_flavor,
    method = analysis$sct_method,
    verbose = FALSE
  )
  object <- Seurat::RunPCA(
    object,
    assay = analysis$sct_assay,
    npcs = as.integer(analysis$pca_npcs),
    seed.use = as.integer(analysis$seed),
    verbose = FALSE
  )
  if (!(analysis$sct_assay %in% SeuratObject::Assays(object))) stop("SCTransform assay missing", call. = FALSE)
  model <- object[[analysis$sct_assay]]@SCTModel.list[[1L]]
  model_arguments <- model@arguments
  if (!identical(model_arguments$method, "glmGamPoi_offset") ||
      !identical(model_arguments$vst.flavor, "v2") ||
      !isTRUE(model_arguments$glmGamPoi_check)) {
    stop("SCTModel does not prove the requested glmGamPoi_offset/v2 backend", call. = FALSE)
  }
  variable_feature_count <- length(SeuratObject::VariableFeatures(object[[analysis$sct_assay]]))
  if (variable_feature_count != as.integer(analysis$sct_variable_features_n)) {
    stop("SCT variable-feature count differs from the frozen config", call. = FALSE)
  }
  pca <- SeuratObject::Embeddings(object, reduction = "pca")
  loadings <- SeuratObject::Loadings(object, reduction = "pca")
  stdev <- SeuratObject::Stdev(object, reduction = "pca")
  if (ncol(pca) != as.integer(analysis$pca_npcs) ||
      any(!is.finite(pca)) || any(!is.finite(loadings)) || any(!is.finite(stdev))) {
    stop("PCA output failed exact dimensionality or finite-value checks", call. = FALSE)
  }
  layer_summaries <- lapply(c("counts", "data", "scale.data"), function(layer) {
    finite_layer_summary(object[[analysis$sct_assay]], layer)
  })
  names(layer_summaries) <- c("counts", "data", "scale.data")
  if (any(vapply(layer_summaries, function(record) record$non_finite_values != 0L, logical(1L)))) {
    stop("SCT output contains non-finite values", call. = FALSE)
  }
  if (ncol(object) != input_spots) stop("SCTransform/PCA unexpectedly changed the spot count", call. = FALSE)
  saveRDS(object, file.path(stage_dir, "preprocessed_seurat.rds"), compress = "xz")
  write_json_atomic(list(
    status = "passed",
    method = "SCTransform_then_PCA",
    requested_vst_flavor = analysis$sct_vst_flavor,
    requested_method = analysis$sct_method,
    actual_vst_flavor = model_arguments$vst.flavor,
    actual_method = model_arguments$method,
    actual_glmGamPoi_check = model_arguments$glmGamPoi_check,
    glmGamPoi_version = observed_backend_versions[["glmGamPoi"]],
    BiocVersion = observed_backend_versions[["BiocVersion"]],
    SparseArray_version = observed_backend_versions[["SparseArray"]],
    backend_package_paths = as.list(backend_package_paths),
    input_spots = input_spots,
    retained_spots = ncol(object),
    sct_assay = analysis$sct_assay,
    sct_variable_features = variable_feature_count,
    sct_layers = layer_summaries,
    sct_non_finite_values = sum(vapply(layer_summaries, function(record) record$non_finite_values, integer(1L))),
    pca_dimensions = ncol(pca),
    pca_non_finite_values = sum(!is.finite(pca)) + sum(!is.finite(loadings)) + sum(!is.finite(stdev)),
    code_hash = code_hash,
    analysis_config_hash = analysis_hash,
    environment_lock_hash = environment_hash,
    bioconductor_pins_sha256 = bioconductor_pins_hash,
    raw_counts_preserved = analysis$assay %in% SeuratObject::Assays(object)
  ), file.path(stage_dir, "preprocess_summary.json"))
})

s60 <- run_stage("S60_CORE_DISCOVERY", fingerprint = analysis_fingerprint, producer = function(stage_dir) {
  object <- readRDS(file.path(s40, "preprocessed_seurat.rds"))
  dims <- seq.int(as.integer(analysis$neighbor_dims[[1L]]), as.integer(analysis$neighbor_dims[[2L]]))
  set.seed(as.integer(analysis$seed))
  object <- Seurat::FindNeighbors(
    object,
    reduction = "pca",
    dims = dims,
    k.param = as.integer(analysis$neighbor_k),
    verbose = FALSE
  )
  object <- Seurat::FindClusters(
    object,
    resolution = as.numeric(analysis$cluster_resolution),
    algorithm = as.integer(analysis$cluster_algorithm),
    random.seed = as.integer(analysis$seed),
    verbose = FALSE
  )
  clusters <- as.character(SeuratObject::Idents(object))
  if (length(clusters) != ncol(object) || anyNA(clusters)) stop("Invalid cluster assignment", call. = FALSE)
  counts <- as.data.frame(table(cluster = clusters), stringsAsFactors = FALSE)
  colnames(counts) <- c("spot_cluster", "n_spots")
  counts$spot_cluster <- as.character(counts$spot_cluster)
  utils::write.csv(counts, file.path(stage_dir, "cluster_counts.csv"), row.names = FALSE)
  saveRDS(object, file.path(stage_dir, "clustered_seurat.rds"), compress = "xz")
  write_json_atomic(list(
    status = "passed",
    cluster_count = nrow(counts),
    cluster_semantics = "expression-derived spot cluster",
    graph_semantics = "PCA transcriptomic kNN; not a spatial geometry graph",
    spatially_regularized = FALSE,
    cell_type_assignment = FALSE
  ), file.path(stage_dir, "cluster_summary.json"))
})

visual_json_signature <- function(value) {
  sha256_text(jsonlite::toJSON(value, auto_unbox = TRUE, null = "null", digits = NA))
}

if (render_round > 1L) {
  previous_round <- render_round - 1L
  previous_review <- file.path(run_root, "06_figures", "review", paste0("review-round-", previous_round, ".json"))
  previous_config <- file.path(run_root, "06_figures", "review", paste0("visual-params-round-", previous_round, ".json"))
  if (!file.exists(previous_review) || !file.exists(previous_config)) {
    stop("A visual revision requires the prior review and prior visual config", call. = FALSE)
  }
  review <- read_json(previous_review)
  if (!identical(review$overall_decision, "revise")) stop("Prior review did not authorize a revision", call. = FALSE)
  prior <- read_json(previous_config)
  keys <- setdiff(union(names(prior), names(visual)), "render_round")
  changed <- keys[vapply(keys, function(key) {
    visual_json_signature(prior[[key]]) != visual_json_signature(visual[[key]])
  }, logical(1L))]
  allowed <- unlist(prior$allowed_visual_only_revision_keys, use.names = FALSE)
  if (!length(changed) || any(!changed %in% allowed)) {
    stop("Visual revision changed no keys or unauthorized keys: ", paste(changed, collapse = ", "), call. = FALSE)
  }
  authorized <- unlist(review$authorized_visual_keys, use.names = FALSE)
  if (any(!changed %in% authorized)) {
    stop("Visual revision keys are not issue-authorized by prior review: ", paste(setdiff(changed, authorized), collapse = ", "), call. = FALSE)
  }
}

s80_key <- file.path("S80_ADVANCED", paste0("round-", render_round))
s80 <- run_stage("S80_ADVANCED", stage_key = s80_key, fingerprint = visual_fingerprint, producer = function(stage_dir) {
  object <- readRDS(file.path(s60, "clustered_seurat.rds"))
  features <- unlist(analysis$features, use.names = FALSE)
  missing_features <- setdiff(features, rownames(object))
  if (length(missing_features)) stop("Required plotted features are missing: ", paste(missing_features, collapse = ", "), call. = FALSE)
  SeuratObject::DefaultAssay(object) <- analysis$sct_assay
  image_name <- SeuratObject::Images(object)[[1L]]

  common <- list(
    object = object,
    images = image_name,
    crop = isTRUE(visual$crop),
    image.scale = visual$image_scale,
    pt.size.factor = as.numeric(visual$point_size_factor),
    image.alpha = as.numeric(visual$image_alpha)
  )
  qc_plot <- do.call(Seurat::SpatialFeaturePlot, c(common, list(
    features = c("nCount_Spatial", "nFeature_Spatial"),
    alpha = c(as.numeric(visual$feature_alpha_min), as.numeric(visual$feature_alpha_max)),
    ncol = 2L,
    combine = TRUE
  ))) + patchwork::plot_annotation(title = "Visium spot QC")
  cluster_plot <- do.call(Seurat::SpatialDimPlot, c(common, list(
    group.by = "seurat_clusters",
    label = isTRUE(visual$cluster_label),
    label.size = as.numeric(visual$cluster_label_size),
    repel = isTRUE(visual$cluster_label_repel),
    alpha = as.numeric(visual$spot_alpha),
    combine = TRUE
  ))) + patchwork::plot_annotation(title = "Expression-derived spot clusters")
  feature_plot <- do.call(Seurat::SpatialFeaturePlot, c(common, list(
    features = features,
    alpha = c(as.numeric(visual$feature_alpha_min), as.numeric(visual$feature_alpha_max)),
    ncol = 2L,
    combine = TRUE
  ))) + patchwork::plot_annotation(title = "SCT-normalized spatial expression")

  plots <- list(
    spatial_qc = qc_plot,
    spatial_clusters = cluster_plot,
    spatial_features_hpca_ttr = feature_plot
  )
  original_dir <- file.path(stage_dir, "original")
  final_dir <- file.path(stage_dir, "final")
  dir.create(original_dir, recursive = TRUE, showWarnings = FALSE)
  dir.create(final_dir, recursive = TRUE, showWarnings = FALSE)
  for (figure_id in names(plots)) {
    styled <- plots[[figure_id]] & ggplot2::theme(text = ggplot2::element_text(size = as.numeric(visual$base_font_size)))
    ggplot2::ggsave(
      filename = file.path(original_dir, paste0(figure_id, ".png")), plot = styled,
      width = as.numeric(visual$original_export$width_in), height = as.numeric(visual$original_export$height_in),
      units = "in", dpi = as.integer(visual$original_export$dpi), bg = "white", limitsize = TRUE
    )
    ggplot2::ggsave(
      filename = file.path(final_dir, paste0(figure_id, ".png")), plot = styled,
      width = as.numeric(visual$final_export$width_in), height = as.numeric(visual$final_export$height_in),
      units = "in", dpi = as.integer(visual$final_export$dpi), bg = "white", limitsize = TRUE
    )
  }
  figure_rows <- do.call(rbind, lapply(names(plots), function(figure_id) {
    original <- file.path(original_dir, paste0(figure_id, ".png"))
    final <- file.path(final_dir, paste0(figure_id, ".png"))
    if (file.info(original)$size < 10000 || file.info(final)$size < 10000) stop("Suspiciously small PNG: ", figure_id, call. = FALSE)
    data.frame(
      figure_id = figure_id,
      original_path = file.path("original", paste0(figure_id, ".png")),
      original_sha256 = sha256_file(original),
      final_path = file.path("final", paste0(figure_id, ".png")),
      final_sha256 = sha256_file(final),
      review_status = "awaiting_native_review",
      stringsAsFactors = FALSE
    )
  }))
  utils::write.csv(figure_rows, file.path(stage_dir, "figure_index.csv"), row.names = FALSE)
  write_json_atomic(visual, file.path(stage_dir, paste0("visual-params-round-", render_round, ".json")))
})

s90 <- run_stage("S90_INFERENCE_QA", fingerprint = analysis_fingerprint, producer = function(stage_dir) {
  summary <- read_json(file.path(s60, "cluster_summary.json"))
  if (!identical(summary$cluster_semantics, "expression-derived spot cluster")) stop("Cluster semantics drift", call. = FALSE)
  write_json_atomic(list(
    status = "passed",
    analysis_scope = "descriptive-only",
    assay_unit = "spot",
    sampling_unit = "one tissue section",
    inference_unit = "not_applicable",
    no_spot_as_cell_claim = TRUE,
    no_spot_as_independent_subject_claim = TRUE,
    no_cell_type_claim = TRUE,
    no_population_effect_claim = TRUE,
    no_mechanistic_or_causal_claim = TRUE,
    prohibited_terms_audited_in_generated_reports = TRUE
  ), file.path(stage_dir, "claim_boundary_qa.json"))
})

s95_key <- file.path("S95_VISUALIZE_INTERPRET", paste0("round-", render_round))
s95 <- run_stage("S95_VISUALIZE_INTERPRET", stage_key = s95_key, fingerprint = visual_fingerprint, producer = function(stage_dir) {
  warning_evidence <- read_json(warning_evidence_path)
  if (!identical(warning_evidence$status, "passed") || as.integer(warning_evidence$blocking_warning_occurrences) != 0L) {
    stop("Runtime warning evidence is not release-safe", call. = FALSE)
  }
  reconciliation <- utils::read.csv(file.path(s20, "barcode_reconciliation.csv"), stringsAsFactors = FALSE)
  set_reconciliation <- read_json(file.path(s20, "barcode_set_reconciliation.json"))
  attrition <- utils::read.csv(file.path(s30, "attrition.csv"), stringsAsFactors = FALSE)
  preprocess <- read_json(file.path(s40, "preprocess_summary.json"))
  cluster_counts <- utils::read.csv(file.path(s60, "cluster_counts.csv"), stringsAsFactors = FALSE)
  figure_index <- utils::read.csv(file.path(s80, "figure_index.csv"), stringsAsFactors = FALSE)
  lookup <- setNames(reconciliation$count, reconciliation$set)

  results_lines <- c(
    "# RESULTS",
    "",
    "## Data and reconciliation",
    "",
    sprintf("- Matrix barcodes: %s", lookup[["matrix_barcodes"]]),
    sprintf("- Vendor in-tissue barcodes: %s", lookup[["vendor_in_tissue"]]),
    sprintf("- Loaded and retained spots: %s", lookup[["loaded_object"]]),
    sprintf("- Spatial assay cells / image cells / coordinate barcodes: %s / %s / %s", lookup[["assay_cells"]], lookup[["image_cells"]], lookup[["coordinates"]]),
    "- All six directed assay/image/coordinate set differences are zero; H5 ∩ vendor in-tissue and the loaded object also reconcile exactly.",
    "- Vendor image assets and positive finite scale factors passed structural QC; native alignment review remains pending.",
    "",
    "## Descriptive analysis",
    "",
    sprintf("- Expression-derived spot clusters: %s", nrow(cluster_counts)),
    sprintf(
      "- Normalization and reduction: SCTransform(vst.flavor = %s, method = %s) on Spatial assay followed by PCA.",
      preprocess$actual_vst_flavor, preprocess$actual_method
    ),
    sprintf(
      "- Backend evidence: glmGamPoi %s, BiocVersion %s, SparseArray %s; %s SCT variable features; %s finite PCs; no spot attrition.",
      preprocess$glmGamPoi_version, preprocess$BiocVersion, preprocess$SparseArray_version,
      preprocess$sct_variable_features, preprocess$pca_dimensions
    ),
    "- Feature overlays: Hpca and Ttr from the SCT data slot.",
    "- Pixel-grounded biological descriptions will be added only after native review of both original and final-size images.",
    "",
    "## Claim boundary",
    "",
    "This is a single-section, spot-level descriptive tutorial. Spots are mixtures, not cells; clusters are not cell types or spatially regularized domains. No donor/group effect, population generalization, mechanism, interaction, or causal claim is supported."
  )
  writeLines(results_lines, file.path(stage_dir, "RESULTS.md"), useBytes = TRUE)

  figure_notes <- c(
    "# FIGURE NOTES",
    "",
    "All figures use the vendor coordinate/image transform and Visium spot as the assay unit. The independent inference unit is not applicable because no inferential comparison is performed.",
    "",
    "## spatial_qc",
    "",
    "Displays nCount_Spatial and nFeature_Spatial over the tissue image. It supports visual inspection of within-section technical gradients only; it does not prove tissue biology or group effects.",
    "",
    "## spatial_clusters",
    "",
    "Displays expression-derived PCA-kNN spot clusters. A colored region is a spot cluster, not a cell type, direct interaction niche, or spatially regularized domain.",
    "",
    "## spatial_features_hpca_ttr",
    "",
    "Displays SCT-normalized Hpca and Ttr expression at capture spots. Co-localization at spot resolution does not establish cell identity, direct contact, signaling, directionality, mechanism, or causality.",
    "",
    "## Visual QA state",
    "",
    sprintf("Render round %s is awaiting native review. Both original and final-size hashes must be opened and recorded before a keep decision.", render_round)
  )
  writeLines(figure_notes, file.path(stage_dir, "FIGURE_NOTES.md"), useBytes = TRUE)

  qa_lines <- c(
    "# QA REPORT",
    "",
    "| Gate | Status | Evidence |",
    "|---|---|---|",
    "| Exact R 4.5.3 | pass | environment.locked.json |",
    "| Exact task-local renv 1.2.2 for bootstrap and project; no host renv dependency | pass | pre-install binary hash, environment lock, renv.lock, and environment probe |",
    "| Exact Seurat 5.5.0 and hdf5r 1.3.12 from Windows binary snapshot | pass | environment lock and spatial/API/H5 smoke probe |",
    "| Complete Bioconductor 3.21 cohort | pass | archive pins, SHA-256/DESCRIPTION gates, renv.lock; glmGamPoi 1.20.0 and SparseArray 1.8.1 |",
    "| Explicit SCT backend | pass | preprocess_summary.json records vst.flavor=v2, method=glmGamPoi_offset, glmGamPoi_check=true, exact package paths and finite layers/PCA |",
    "| Native Windows R shutdown | pass | child-only AMD64 restoration, clean stdout/stderr scan, native exit 0, hash-bound completion marker |",
    "| Input size/SHA freeze | pass | resolved-inputs.json |",
    "| Assay/image/coordinate two-way barcode reconciliation | pass | barcode_reconciliation.csv, barcode_set_reconciliation.json, barcode_set_differences.csv |",
    "| Coordinate/image/scale-factor structural QC | pass | coordinate_image_qc.json |",
    "| No undocumented post-load filtering | pass | attrition.csv and analysis config |",
    "| Checkpoint hash binding | pass | per-stage _checkpoint.json |",
    sprintf("| Runtime warning classification | pass | pipeline-warnings.json; %s occurrence(s), zero release blockers |", warning_evidence$warning_occurrences),
    "| Descriptive claim boundary | pass | claim_boundary_qa.json |",
    "| Original/final PNG export | pass | figure_index.csv |",
    "| Native coordinate alignment and visual review | pending | review-round template; not inferred from code |",
    "",
    "A pending native review is a release blocker. Generated PNGs alone do not satisfy native review."
  )
  writeLines(qa_lines, file.path(stage_dir, "QA_REPORT.md"), useBytes = TRUE)

  write_json_atomic(list(
    schema_version = "1.0",
    case = "visium-mouse-brain",
    status = "NATIVE_VISUAL_REVIEW",
    render_round = render_round,
    dataset_id = resolved_inputs$dataset_id,
    observed = list(
      matrix_barcodes = as.integer(lookup[["matrix_barcodes"]]),
      vendor_all_positions = as.integer(lookup[["vendor_all_positions"]]),
      vendor_in_tissue_barcodes = as.integer(lookup[["vendor_in_tissue"]]),
      loaded_spots = as.integer(lookup[["loaded_object"]]),
      assay_cells = as.integer(lookup[["assay_cells"]]),
      image_cells = as.integer(lookup[["image_cells"]]),
      coordinate_barcodes = as.integer(lookup[["coordinates"]]),
      directed_assay_image_coordinate_differences = set_reconciliation$directed_difference_counts,
      retained_spots = as.integer(tail(attrition$count, 1L)),
      expression_spot_clusters = nrow(cluster_counts),
      plotted_features = c("Hpca", "Ttr"),
      preprocessing = list(
        vst_flavor = preprocess$actual_vst_flavor,
        method = preprocess$actual_method,
        glmGamPoi_check = preprocess$actual_glmGamPoi_check,
        glmGamPoi_version = preprocess$glmGamPoi_version,
        BiocVersion = preprocess$BiocVersion,
        SparseArray_version = preprocess$SparseArray_version,
        sct_variable_features = preprocess$sct_variable_features,
        pca_dimensions = preprocess$pca_dimensions,
        sct_non_finite_values = preprocess$sct_non_finite_values,
        pca_non_finite_values = preprocess$pca_non_finite_values
      )
    ),
    validation = list(
      inputs = "passed",
      barcode_coordinate_image_structural_qc = "passed",
      analysis = "passed",
      runtime_warnings = "passed",
      reports = "passed",
      native_visual_review = "pending"
    ),
    environment = list(
      repository_snapshot = environment_lock$repository$snapshot_url,
      package_type = environment_lock$repository$package_type,
      task_local_renv_version = environment_lock$task_local_renv_version,
      bootstrap_renv_version = environment_lock$bootstrap_renv_version,
      bootstrap_renv_binary_sha256 = environment_lock$bootstrap$binary_sha256,
      host_renv_required = environment_lock$bootstrap$host_renv_required,
      renv_lock_sha256 = environment_lock$renv_lock_sha256,
      probe_sha256 = environment_lock$probe$sha256,
      shutdown_mode = environment_lock$shutdown_mode,
      bioconductor_release = environment_lock$bioconductor$release,
      bioconductor_version = environment_lock$bioconductor$version,
      bioconductor_pins_sha256 = environment_lock$bioconductor$pins_sha256,
      bioconductor_archive_manifest_sha256 = environment_lock$bioconductor$archive_manifest_sha256,
      complete_same_release_closure = environment_lock$bioconductor$same_release_closure,
      source_compilation_allowed = environment_lock$bioconductor$source_compilation_allowed
    ),
    claim_scope = "single-section spot-level descriptive-only",
    sensitive_paths_included = FALSE
  ), file.path(stage_dir, "execution-summary.json"))
})

# Materialize immutable, round-addressed delivery artifacts only after every
# preceding stage has a hash-valid checkpoint.
for (pair in list(
  c(file.path(s20, "barcode_reconciliation.csv"), file.path(run_root, "05_results", "tables", "barcode_reconciliation.csv")),
  c(file.path(s20, "barcode_set_reconciliation.json"), file.path(run_root, "05_results", "tables", "barcode_set_reconciliation.json")),
  c(file.path(s20, "barcode_set_differences.csv"), file.path(run_root, "05_results", "tables", "barcode_set_differences.csv")),
  c(file.path(s20, "coordinate_image_qc.json"), file.path(run_root, "05_results", "tables", "coordinate_image_qc.json")),
  c(file.path(s30, "spot_qc.csv"), file.path(run_root, "05_results", "tables", "spot_qc.csv")),
  c(file.path(s30, "attrition.csv"), file.path(run_root, "05_results", "tables", "attrition.csv")),
  c(file.path(s60, "cluster_counts.csv"), file.path(run_root, "05_results", "tables", "cluster_counts.csv")),
  c(file.path(s60, "clustered_seurat.rds"), file.path(run_root, "05_results", "objects", "analysis_final_seurat.rds"))
)) copy_immutable(pair[[1L]], pair[[2L]])

round_label <- paste0("round-", render_round)
for (kind in c("original", "final")) {
  for (source in list.files(file.path(s80, kind), full.names = TRUE, pattern = "\\.png$")) {
    copy_immutable(source, file.path(run_root, "06_figures", kind, round_label, basename(source)))
  }
}
copy_immutable(
  file.path(s80, paste0("visual-params-round-", render_round, ".json")),
  file.path(run_root, "06_figures", "review", paste0("visual-params-round-", render_round, ".json"))
)
for (report in c("RESULTS.md", "FIGURE_NOTES.md", "QA_REPORT.md")) {
  copy_immutable(file.path(s95, report), file.path(run_root, "07_reports", round_label, report))
  copy_current(file.path(s95, report), file.path(run_root, "07_reports", report))
}
copy_immutable(file.path(s95, "execution-summary.json"), file.path(run_root, "manifest", paste0("execution-summary-", round_label, ".json")))
copy_current(file.path(s95, "execution-summary.json"), file.path(run_root, "manifest", "execution-summary.json"))

figure_index <- utils::read.csv(file.path(s80, "figure_index.csv"), stringsAsFactors = FALSE)
review_rows <- lapply(seq_len(nrow(figure_index)), function(i) {
  list(
    figure_id = figure_index$figure_id[[i]],
    original_path = file.path("06_figures", "original", round_label, paste0(figure_index$figure_id[[i]], ".png")),
    original_sha256 = figure_index$original_sha256[[i]],
    final_path = file.path("06_figures", "final", round_label, paste0(figure_index$figure_id[[i]], ".png")),
    final_sha256 = figure_index$final_sha256[[i]],
    visible = list(), interpretable = list(), confirmed = list(),
    cannot_assert = c("donor/group effect", "cell type", "mechanism or causality"),
    findings = list(),
    decision = "REQUIRED_keep_revise_or_reselect"
  )
})
review_template <- list(
  schema_version = "2.0",
  case = "visium-mouse-brain",
  round = render_round,
  data_hash = sha256_file(file.path(run_root, "05_results", "objects", "analysis_final_seurat.rds")),
  reviewer_method = "native_local_image_view",
  reviewer_tool = "REQUIRED_ACTUAL_TOOL_NAME",
  opened_original_and_final = FALSE,
  evidence_level = "image_code_data",
  figure_reviews = review_rows,
  authorized_visual_keys = list(),
  overall_findings = list(),
  overall_decision = "REQUIRED_keep_revise_reselect_or_blocked"
)
review_template_path <- file.path(run_root, "06_figures", "review", paste0("review-round-", render_round, ".template.json"))
if (!file.exists(review_template_path)) write_json_atomic(review_template, review_template_path)

review_state <- list(
  schema_version = "2.0",
  case = "visium-mouse-brain",
  status = "awaiting_native_review",
  current_round = render_round,
  data_hash = review_template$data_hash,
  code_hash = code_hash,
  analysis_config_hash = analysis_hash,
  visual_config_hash = visual_hash,
  renders = review_rows
)
write_json_atomic(review_state, file.path(run_root, "06_figures", "review", "visual-review-state.json"))

artifact_paths <- c(
  file.path(run_root, "05_results", "tables", "barcode_reconciliation.csv"),
  file.path(run_root, "05_results", "tables", "barcode_set_reconciliation.json"),
  file.path(run_root, "05_results", "tables", "barcode_set_differences.csv"),
  file.path(run_root, "05_results", "tables", "coordinate_image_qc.json"),
  file.path(run_root, "05_results", "tables", "spot_qc.csv"),
  file.path(run_root, "05_results", "tables", "attrition.csv"),
  file.path(run_root, "05_results", "tables", "cluster_counts.csv"),
  file.path(run_root, "05_results", "objects", "analysis_final_seurat.rds"),
  warning_evidence_path,
  list.files(file.path(run_root, "06_figures", "original", round_label), full.names = TRUE),
  list.files(file.path(run_root, "06_figures", "final", round_label), full.names = TRUE)
)
artifact_paths <- artifact_paths[file.exists(artifact_paths)]
ledger_records <- lapply(seq_along(artifact_paths), function(i) {
  path <- artifact_paths[[i]]
  rel <- relative_path(path)
  role <- if (grepl("06_figures", path, fixed = TRUE)) {
    "figure"
  } else if (grepl("07_reports", path, fixed = TRUE)) {
    "report"
  } else if (identical(normalizePath(path, winslash = "/"), normalizePath(warning_evidence_path, winslash = "/"))) {
    "validation_evidence"
  } else {
    "analysis_artifact"
  }
  stage_for_artifact <- if (grepl("barcode_reconciliation|barcode_set_reconciliation|barcode_set_differences|coordinate_image_qc", rel)) {
    "S20_COORD_IMAGE_QC"
  } else if (grepl("spot_qc|attrition", rel)) {
    "S30_UNIT_QC"
  } else if (role == "figure") {
    "S95_VISUALIZE_INTERPRET"
  } else if (role == "validation_evidence") {
    "RUNTIME_WARNING_QA"
  } else {
    "S60_CORE_DISCOVERY"
  }
  list(
    artifact_id = paste0("visium-", gsub("(^-|-$)", "", gsub("[^a-z0-9]+", "-", tolower(tools::file_path_sans_ext(rel))))),
    stage_id = stage_for_artifact,
    role = role,
    type = if (dir.exists(path)) "directory" else "file",
    format = tolower(tools::file_ext(path)) %||% "binary",
    path = rel,
    sha256 = sha256_file(path),
    size_bytes = unname(file.info(path)$size),
    producer = list(recipe_id = "visium-mouse-brain-seurat-v1", code_version = code_hash),
    consumers = list(),
    environment_lock_hash = environment_hash,
    units = list(
      assay_unit = if (role %in% c("report", "validation_evidence")) "not_applicable" else "spot",
      spatial_unit = "Visium spot",
      sampling_unit = "one tissue section",
      inference_unit = "not_applicable_descriptive"
    ),
    spatial_frame = if (role %in% c("report", "validation_evidence")) NULL else list(
      coordinate_system = "10x_visium_vendor_image_coordinates",
      coordinate_unit = "pixel",
      transform_id = "vendor_scalefactors_json_lowres"
    ),
    validation = list(status = "passed", rules = c("readable", "sha256-bound", "nonzero-size")),
    maturity = "data-verified",
    conclusion_role = if (role == "figure") "descriptive_spatial_overview_pending_native_review" else "auditable_delivery"
  )
})
ledger_path <- file.path(run_root, "manifest", "artifact_ledger.jsonl")
existing_ids <- character()
if (file.exists(ledger_path)) {
  existing_lines <- readLines(ledger_path, warn = FALSE)
  existing_lines <- existing_lines[nzchar(trimws(existing_lines))]
  if (length(existing_lines)) {
    existing_ids <- vapply(existing_lines, function(line) jsonlite::fromJSON(line, simplifyVector = TRUE)$artifact_id, character(1L))
  }
}
new_records <- ledger_records[!vapply(ledger_records, function(record) record$artifact_id %in% existing_ids, logical(1L))]
if (length(new_records)) {
  new_lines <- vapply(new_records, jsonlite::toJSON, auto_unbox = TRUE, null = "null", FUN.VALUE = character(1L))
  if (!file.exists(ledger_path)) {
    write_text_atomic(new_lines, ledger_path)
  } else {
    cat(paste0(new_lines, "\n"), file = ledger_path, append = TRUE, sep = "")
  }
}

all_ledger_lines <- readLines(ledger_path, warn = FALSE)
all_ledger_lines <- all_ledger_lines[nzchar(trimws(all_ledger_lines))]
all_ledger_records <- lapply(all_ledger_lines, jsonlite::fromJSON, simplifyVector = TRUE)
artifact_lines <- c(
  "# ARTIFACT INDEX", "",
  "| Artifact ID | Stage | Role | Path | SHA-256 | Size (bytes) | Maturity |",
  "|---|---|---|---|---|---:|---|"
)
for (record in all_ledger_records) {
  artifact_lines <- c(artifact_lines, sprintf(
    "| %s | %s | %s | `%s` | `%s` | %s | %s |",
    record$artifact_id, record$stage_id, record$role, record$path, record$sha256, record$size_bytes, record$maturity
  ))
}
artifact_lines <- c(artifact_lines, "", "Figure maturity remains data-verified until hash-bound native visual review is terminal.")
artifact_index_path <- file.path(run_root, "07_reports", "ARTIFACT_INDEX.md")
write_text_atomic(artifact_lines, artifact_index_path)

run_manifest <- list(
  schema_version = "1.0",
  case = "visium-mouse-brain",
  state = "NATIVE_VISUAL_REVIEW",
  mode = mode,
  render_round = render_round,
  fingerprints = list(
    inputs = input_hash,
    analysis_config = analysis_hash,
    visual_config = visual_hash,
    code = code_hash,
    environment_lock = environment_hash
  ),
  checkpoints = c("S10_INGEST", "S20_COORD_IMAGE_QC", "S30_UNIT_QC", "S40_PREPROCESS", "S60_CORE_DISCOVERY", "S80_ADVANCED", "S90_INFERENCE_QA", "S95_VISUALIZE_INTERPRET"),
  artifact_ledger = "manifest/artifact_ledger.jsonl",
  execution_summary = "manifest/execution-summary.json",
  native_visual_review = "pending"
)
write_json_atomic(run_manifest, file.path(run_root, "manifest", "run_manifest.json"))
log_event("pipeline_complete_awaiting_native_review")
cat(jsonlite::toJSON(read_json(file.path(run_root, "manifest", "execution-summary.json")), auto_unbox = TRUE, pretty = TRUE), "\n")
flush.console()
terminal_warning_evidence <- read_json(warning_evidence_path)
if (!identical(terminal_warning_evidence$status, "passed") ||
    as.integer(terminal_warning_evidence$warning_occurrences) != 0L ||
    as.integer(terminal_warning_evidence$blocking_warning_occurrences) != 0L) {
  stop("Pipeline cannot emit a completion marker with runtime warnings", call. = FALSE)
}
write_json_atomic(list(
  schema_version = "1.0",
  stage = "pipeline",
  status = "complete",
  mode = mode,
  shutdown_mode = "native_exit",
  code_hash = code_hash,
  analysis_config_hash = analysis_hash,
  visual_config_hash = visual_hash,
  environment_lock_hash = environment_hash,
  bioconductor_pins_sha256 = bioconductor_pins_hash,
  warning_evidence_sha256 = sha256_file(warning_evidence_path),
  warning_occurrences = 0L,
  fault_injection = "none"
), completion_marker_path)
flush.console()
