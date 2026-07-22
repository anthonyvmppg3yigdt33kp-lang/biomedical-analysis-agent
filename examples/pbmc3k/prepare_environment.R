#!/usr/bin/env Rscript

# Task-local renv provision/freeze helper. This is environment-management code,
# not an AnalysisRecipe; run_pipeline.R intentionally contains no installers.

parse_args <- function(args) {
  values <- list()
  index <- 1L
  while (index <= length(args)) {
    if (!startsWith(args[[index]], "--") || index == length(args)) {
      stop("Arguments must be supplied as --name value pairs", call. = FALSE)
    }
    values[[substring(args[[index]], 3L)]] <- args[[index + 1L]]
    index <- index + 2L
  }
  required <- c(
    "project", "library", "cache", "lock-out", "evidence-out",
    "packages", "expected", "snapshot-repo", "binary-evidence-dir",
    "bootstrap-library", "seurat-binary-sha256", "seurat-binary-size",
    "renv-binary-sha256", "renv-binary-size",
    "completion-marker", "inject-error-before-exit"
  )
  missing <- required[!required %in% names(values)]
  if (length(missing) > 0L) {
    stop(sprintf("Missing arguments: %s", paste(missing, collapse = ", ")), call. = FALSE)
  }
  values
}

parse_expected <- function(value) {
  records <- strsplit(value, ";", fixed = TRUE)[[1L]]
  pieces <- strsplit(records, "=", fixed = TRUE)
  if (any(lengths(pieces) != 2L)) {
    stop("Invalid expected package version contract", call. = FALSE)
  }
  stats::setNames(vapply(pieces, `[[`, character(1), 2L), vapply(pieces, `[[`, character(1), 1L))
}

args <- parse_args(commandArgs(trailingOnly = TRUE))
# Preserve the caller-provided Windows 8.3 aliases. normalizePath() expands them
# back to the long workspace path and can make RcppEigen include paths exceed the
# Windows compiler boundary during Seurat source installation.
portable_path <- function(path) chartr("\\", "/", path)
project <- portable_path(args$project)
library <- portable_path(args$library)
cache <- portable_path(args$cache)
lock_out <- portable_path(args[["lock-out"]])
evidence_out <- portable_path(args[["evidence-out"]])
snapshot_repo <- args[["snapshot-repo"]]
binary_evidence_dir <- portable_path(args[["binary-evidence-dir"]])
bootstrap_library <- portable_path(args[["bootstrap-library"]])
completion_marker <- portable_path(args[["completion-marker"]])
inject_error_before_exit <- tolower(args[["inject-error-before-exit"]])
package_specs <- strsplit(args$packages, ";", fixed = TRUE)[[1L]]
expected <- parse_expected(args$expected)
expected_seurat_binary_sha256 <- tolower(args[["seurat-binary-sha256"]])
expected_seurat_binary_size <- as.numeric(args[["seurat-binary-size"]])
expected_renv_binary_sha256 <- tolower(args[["renv-binary-sha256"]])
expected_renv_binary_size <- as.numeric(args[["renv-binary-size"]])

if (!inject_error_before_exit %in% c("true", "false")) {
  stop("INVALID_INJECT_ERROR_BEFORE_EXIT_FLAG", call. = FALSE)
}

for (path in c(
  project, library, cache, binary_evidence_dir, bootstrap_library, dirname(lock_out),
  dirname(evidence_out), dirname(completion_marker)
)) {
  dir.create(path, recursive = TRUE, showWarnings = FALSE)
}
unlink(completion_marker, force = TRUE)

observed_r <- paste(R.version$major, R.version$minor, sep = ".")
if (!identical(observed_r, "4.5.3")) {
  stop(sprintf("R_VERSION_MISMATCH: expected 4.5.3, observed %s", observed_r), call. = FALSE)
}

sha256_file <- function(path) {
  value <- unname(tools::sha256sum(path))
  if (length(value) != 1L || is.na(value) || !grepl("^[0-9a-fA-F]{64}$", value)) {
    stop(sprintf("SHA256_EVIDENCE_FAILED: %s", basename(path)), call. = FALSE)
  }
  tolower(value)
}

Sys.setenv(
  RENV_PATHS_LIBRARY = library,
  RENV_PATHS_CACHE = cache,
  RENV_CONFIG_AUTO_SNAPSHOT = "FALSE",
  RENV_CONFIG_SYNCHRONIZED_CHECK = "FALSE"
)
options(repos = c(CRAN = snapshot_repo))

if (!identical(snapshot_repo, "https://packagemanager.posit.co/cran/2026-04-23")) {
  stop("REPOSITORY_SNAPSHOT_MISMATCH", call. = FALSE)
}
available <- utils::available.packages(repos = snapshot_repo, type = "binary")
missing_available <- setdiff(names(expected), rownames(available))
if (length(missing_available) > 0L) {
  stop(sprintf("SNAPSHOT_PACKAGE_MISSING: %s", paste(missing_available, collapse = ", ")), call. = FALSE)
}
available_versions <- available[names(expected), "Version"]
version_mismatch <- names(expected)[available_versions != expected]
if (length(version_mismatch) > 0L) {
  details <- paste(
    sprintf("%s expected=%s available=%s", version_mismatch, expected[version_mismatch], available_versions[version_mismatch]),
    collapse = "; "
  )
  stop(sprintf("SNAPSHOT_VERSION_ASSERTION_FAILED: %s", details), call. = FALSE)
}
seurat_spec <- package_specs[startsWith(package_specs, "Seurat@")]
if (length(seurat_spec) != 1L || !identical(seurat_spec, "Seurat@5.5.0")) {
  stop("SEURAT_PIN_INVALID: expected exactly Seurat@5.5.0", call. = FALSE)
}
downloaded <- utils::download.packages(
  pkgs = c("Seurat", "renv"),
  destdir = binary_evidence_dir,
  available = available,
  repos = snapshot_repo,
  type = "binary"
)
if (!is.matrix(downloaded) || nrow(downloaded) != 2L) {
  stop("PINNED_BINARY_EVIDENCE_DOWNLOAD_FAILED", call. = FALSE)
}

download_path <- function(package) {
  row <- which(downloaded[, 1L] == package)
  if (length(row) != 1L || !file.exists(downloaded[row, 2L])) {
    stop(sprintf("PINNED_BINARY_EVIDENCE_MISSING: %s", package), call. = FALSE)
  }
  downloaded[row, 2L]
}
seurat_binary_path <- download_path("Seurat")
renv_binary_path <- download_path("renv")
seurat_binary_sha256 <- sha256_file(seurat_binary_path)
renv_binary_sha256 <- sha256_file(renv_binary_path)
seurat_binary_size <- unname(file.info(seurat_binary_path)[["size"]])
renv_binary_size <- unname(file.info(renv_binary_path)[["size"]])
if (
  !identical(basename(seurat_binary_path), "Seurat_5.5.0.zip") ||
  !identical(seurat_binary_sha256, expected_seurat_binary_sha256) ||
  !isTRUE(all.equal(seurat_binary_size, expected_seurat_binary_size))
) {
  stop("SEURAT_BINARY_PIN_MISMATCH", call. = FALSE)
}
if (
  !identical(basename(renv_binary_path), "renv_1.2.2.zip") ||
  !identical(renv_binary_sha256, expected_renv_binary_sha256) ||
  !isTRUE(all.equal(renv_binary_size, expected_renv_binary_size))
) {
  stop("RENV_BINARY_PIN_MISMATCH", call. = FALSE)
}

# Bootstrap the environment manager from verified bytes inside this run.  No
# host/user/site renv installation is consulted or modified.
.libPaths(.Library)
if ("renv" %in% loadedNamespaces()) {
  stop("HOST_RENV_NAMESPACE_PRELOADED", call. = FALSE)
}
utils::install.packages(
  pkgs = renv_binary_path,
  lib = bootstrap_library,
  repos = NULL,
  type = "win.binary",
  quiet = FALSE
)
bootstrap_package_path <- find.package("renv", lib.loc = bootstrap_library, quiet = TRUE)
if (
  length(bootstrap_package_path) != 1L ||
  !identical(as.character(utils::packageVersion("renv", lib.loc = bootstrap_library)), "1.2.2")
) {
  stop("TASK_LOCAL_RENV_BOOTSTRAP_FAILED", call. = FALSE)
}
renv_namespace <- loadNamespace("renv", lib.loc = bootstrap_library)

.libPaths(unique(c(library, bootstrap_library, .Library)))
install_errors <- character()
for (attempt in seq_len(2L)) {
  result <- tryCatch(
    {
      utils::install.packages(
        pkgs = "Seurat",
        lib = library,
        repos = snapshot_repo,
        type = "binary",
        dependencies = NA,
        quiet = FALSE
      )
      # Reinstall both top-level packages from the independently hash-verified
      # archives so the final task library is bound to those exact bytes.
      utils::install.packages(
        pkgs = seurat_binary_path,
        lib = library,
        repos = NULL,
        type = "win.binary",
        quiet = FALSE
      )
      utils::install.packages(
        pkgs = renv_binary_path,
        lib = library,
        repos = NULL,
        type = "win.binary",
        quiet = FALSE
      )
      NULL
    },
    error = function(condition) condition
  )
  if (is.null(result)) {
    installed_versions <- vapply(names(expected), function(package) {
      if (length(find.package(package, lib.loc = library, quiet = TRUE)) != 1L) {
        return(NA_character_)
      }
      as.character(utils::packageVersion(package, lib.loc = library))
    }, character(1))
    if (all(installed_versions == expected)) break
    result <- simpleError("post-install exact-version validation failed")
  }
  install_errors <- c(install_errors, sprintf("attempt %d: %s", attempt, conditionMessage(result)))
}
if (length(install_errors) == 2L) {
  stop(sprintf("EXACT_BINARY_INSTALL_FAILED: %s", paste(install_errors, collapse = " | ")), call. = FALSE)
}

observed <- vapply(names(expected), function(package) {
  if (length(find.package(package, lib.loc = library, quiet = TRUE)) != 1L) {
    stop(sprintf("PACKAGE_MISSING_AFTER_PROVISION: %s", package), call. = FALSE)
  }
  as.character(utils::packageVersion(package, lib.loc = library))
}, character(1))
mismatch <- names(expected)[observed != expected]
if (length(mismatch) > 0L) {
  details <- paste(sprintf("%s expected=%s observed=%s", mismatch, expected[mismatch], observed[mismatch]), collapse = "; ")
  stop(sprintf("PACKAGE_VERSION_MISMATCH: %s", details), call. = FALSE)
}

create_object <- getExportedValue("Seurat", "CreateSeuratObject")
smoke_counts <- Matrix::Matrix(
  matrix(
    c(1, 0, 2, 0, 3, 1),
    nrow = 3L,
    dimnames = list(c("MS4A1", "CD3D", "LYZ"), c("cell_a", "cell_b"))
  ),
  sparse = TRUE
)
smoke_object <- create_object(counts = smoke_counts, min.cells = 0, min.features = 0)
if (ncol(smoke_object) != 2L || !is.function(getExportedValue("Seurat", "RunUMAP"))) {
  stop("SEURAT_API_SMOKE_FAILED", call. = FALSE)
}

renv_snapshot <- get("snapshot", envir = renv_namespace, inherits = FALSE)
renv_snapshot(
  project = project,
  library = library,
  lockfile = lock_out,
  type = "all",
  prompt = FALSE,
  force = TRUE
)
if (!file.exists(lock_out) || file.info(lock_out)$size <= 0) {
  stop("RENV_LOCK_MISSING_AFTER_SNAPSHOT", call. = FALSE)
}

binary_path <- seurat_binary_path
binary_sha256 <- seurat_binary_sha256
binary_size <- seurat_binary_size
if (length(binary_size) != 1L || is.na(binary_size) || binary_size <= 0) {
  stop("SEURAT_BINARY_SIZE_EVIDENCE_FAILED", call. = FALSE)
}

# The immutable lock is augmented before its completion hash is recorded.  This
# keeps the marker bound to the exact bytes later promoted to 02_environment/renv.lock.
lock_document <- jsonlite::read_json(lock_out, simplifyVector = FALSE)
lock_document$BAA <- list(
  RepositorySnapshot = snapshot_repo,
  PackageType = "win.binary",
  Bootstrap = list(
    HostRenvRequired = FALSE,
    Library = "02_environment/bootstrap-library",
    Version = "1.2.2",
    File = basename(renv_binary_path),
    SHA256 = renv_binary_sha256,
    Size = renv_binary_size
  ),
  Archives = list(
    Seurat = list(
      Version = "5.5.0",
      File = basename(binary_path),
      SHA256 = binary_sha256,
      Size = binary_size
    ),
    renv = list(
      Version = "1.2.2",
      File = basename(renv_binary_path),
      SHA256 = renv_binary_sha256,
      Size = renv_binary_size
    )
  )
)
augmented_lock <- paste0(lock_out, ".augmented")
jsonlite::write_json(
  lock_document,
  augmented_lock,
  auto_unbox = TRUE,
  pretty = TRUE,
  null = "null"
)
if (!file.exists(augmented_lock) || file.info(augmented_lock)$size <= 0) {
  stop("AUGMENTED_RENV_LOCK_MISSING", call. = FALSE)
}
if (!file.remove(lock_out) || !file.rename(augmented_lock, lock_out)) {
  stop("AUGMENTED_RENV_LOCK_PROMOTION_FAILED", call. = FALSE)
}

evidence <- c(
  paste("r_version", observed_r, sep = "="),
  paste("platform", R.version$platform, sep = "="),
  paste("repository_snapshot", snapshot_repo, sep = "="),
  paste("package_type", "binary", sep = "="),
  paste("seurat_binary_file", basename(binary_path), sep = "="),
  paste("seurat_binary_sha256", binary_sha256, sep = "="),
  paste("seurat_binary_size", format(binary_size, scientific = FALSE, trim = TRUE), sep = "="),
  paste("renv_binary_file", basename(renv_binary_path), sep = "="),
  paste("renv_binary_sha256", renv_binary_sha256, sep = "="),
  paste("renv_binary_size", format(renv_binary_size, scientific = FALSE, trim = TRUE), sep = "="),
  paste("bootstrap_renv_version", "1.2.2", sep = "="),
  paste("bootstrap_source", "task-local-verified-binary", sep = "="),
  paste("host_renv_required", "false", sep = "="),
  paste("host_renv_namespace_preloaded", "false", sep = "="),
  paste("renv_version", as.character(utils::packageVersion("renv", lib.loc = library)), sep = "="),
  vapply(names(observed), function(package) paste(package, observed[[package]], sep = "="), character(1))
)
writeLines(evidence, evidence_out, useBytes = TRUE)

lock_sha256 <- sha256_file(lock_out)
probe_sha256 <- sha256_file(evidence_out)
if (identical(inject_error_before_exit, "true")) {
  stop("INJECTED_ERROR_BEFORE_NATIVE_EXIT", call. = FALSE)
}
writeLines(
  c(
    "stage=environment-provision",
    "status=complete",
    "shutdown_mode=native_exit",
    paste("lock_sha256", lock_sha256, sep = "="),
    paste("probe_sha256", probe_sha256, sep = "=")
  ),
  completion_marker,
  useBytes = TRUE
)
cat("TASK_LOCAL_RENV_FROZEN\n")
flush.console()
