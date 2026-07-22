options(stringsAsFactors = FALSE, warn = 2L)

parse_args <- function(values) {
  result <- list()
  index <- 1L
  while (index <= length(values)) {
    key <- values[[index]]
    if (!startsWith(key, "--") || index == length(values)) {
      stop("Arguments must be --key value pairs", call. = FALSE)
    }
    result[[substring(key, 3L)]] <- values[[index + 1L]]
    index <- index + 2L
  }
  result
}

required_arg <- function(values, key) {
  value <- values[[key]]
  if (is.null(value) || !nzchar(value)) stop("Missing --", key, call. = FALSE)
  normalizePath(value, winslash = "/", mustWork = key != "output")
}

args <- parse_args(commandArgs(trailingOnly = TRUE))
env_root <- required_arg(args, "env-root")
marker_path <- required_arg(args, "marker")
lock_path <- required_arg(args, "lock")
status_path <- required_arg(args, "status")
smoke_h5 <- required_arg(args, "smoke-h5")
output_path <- required_arg(args, "output")
fault_injection <- args[["fault-injection"]]
if (is.null(fault_injection)) fault_injection <- "none"
if (!fault_injection %in% c("none", "before_completion_marker")) stop("Unsupported cache validation fault injection", call. = FALSE)

if (!identical(as.character(getRversion()), "4.5.3")) stop("Cache validation requires exact R 4.5.3", call. = FALSE)
if (!identical(Sys.getenv("PROCESSOR_ARCHITECTURE", unset = ""), "AMD64")) stop("Cache validation requires AMD64", call. = FALSE)
if (!identical(normalizePath(Sys.getenv("RENV_PROJECT", unset = ""), winslash = "/", mustWork = TRUE), env_root)) {
  stop("RENV_PROJECT does not identify the validated cache", call. = FALSE)
}

required_packages <- c(
  renv = "1.2.2", Seurat = "5.5.0", SeuratObject = "5.4.0",
  hdf5r = "1.3.12", sctransform = "0.4.3", glmGamPoi = "1.20.0",
  BiocVersion = "3.21.1", SparseArray = "1.8.1", jsonlite = "2.0.0",
  digest = "0.6.39"
)
missing <- names(required_packages)[!vapply(names(required_packages), requireNamespace, quietly = TRUE, FUN.VALUE = logical(1L))]
if (length(missing)) stop("Validated cache is missing packages: ", paste(missing, collapse = ", "), call. = FALSE)
observed <- vapply(names(required_packages), function(package) as.character(utils::packageVersion(package)), character(1L))
if (!identical(unname(observed), unname(required_packages))) {
  stop("Validated cache package versions changed: ", paste(names(observed), observed, sep = "=", collapse = ", "), call. = FALSE)
}

project_library <- normalizePath(renv::paths$library(project = env_root), winslash = "/", mustWork = TRUE)
for (package in names(required_packages)) {
  package_path <- normalizePath(find.package(package), winslash = "/", mustWork = TRUE)
  if (!startsWith(paste0(package_path, "/"), paste0(project_library, "/"))) {
    stop("Cache validation loaded a package outside the task-local project library: ", package, call. = FALSE)
  }
}

marker <- jsonlite::read_json(marker_path, simplifyVector = TRUE)
status <- jsonlite::read_json(status_path, simplifyVector = TRUE)
lock_sha256 <- digest::digest(file = lock_path, algo = "sha256", serialize = FALSE)
if (!identical(marker$status, "frozen") || !identical(marker$renv_lock_sha256, lock_sha256)) {
  stop("Cached environment marker is not frozen or lock-bound", call. = FALSE)
}
if (!isTRUE(status$synchronized) || as.integer(status$status_difference_count) != 0L || as.integer(status$restore_action_count) != 0L) {
  stop("Cached renv status is not synchronized with an empty restore plan", call. = FALSE)
}

matrix <- Seurat::Read10X_h5(smoke_h5, use.names = TRUE, unique.features = TRUE)
observed_dimensions <- unname(dim(matrix))
expected_dimensions <- c(
  as.integer(marker$h5_reader_smoke$matrix_dimensions$features),
  as.integer(marker$h5_reader_smoke$matrix_dimensions$barcodes)
)
if (!identical(observed_dimensions, expected_dimensions)) stop("Cached environment H5 smoke dimensions changed", call. = FALSE)
if (identical(fault_injection, "before_completion_marker")) {
  message("FAULT_INJECTION_BEFORE_COMPLETION_MARKER")
  stop("FAULT_INJECTION_BEFORE_COMPLETION_MARKER", call. = FALSE)
}

payload <- list(
  schema_version = "1.0",
  status = "passed",
  validation_mode = "fresh_run_cache_reuse",
  r_version = as.character(getRversion()),
  package_versions = as.list(observed),
  renv_lock_sha256 = lock_sha256,
  renv_status_synchronized = TRUE,
  restore_action_count = 0L,
  h5_input_sha256 = digest::digest(file = smoke_h5, algo = "sha256", serialize = FALSE),
  matrix_dimensions = list(features = observed_dimensions[[1L]], barcodes = observed_dimensions[[2L]]),
  processor_architecture = Sys.getenv("PROCESSOR_ARCHITECTURE"),
  shutdown_mode = "native_exit",
  absolute_paths_included = FALSE
)
dir.create(dirname(output_path), recursive = TRUE, showWarnings = FALSE)
temporary <- file.path(dirname(output_path), paste0(".", basename(output_path), ".tmp-", Sys.getpid()))
jsonlite::write_json(payload, temporary, auto_unbox = TRUE, pretty = TRUE)
if (!file.rename(temporary, output_path)) stop("Cannot promote cache validation probe", call. = FALSE)
cat(jsonlite::toJSON(payload, auto_unbox = TRUE, pretty = TRUE), "\n")
flush.console()
