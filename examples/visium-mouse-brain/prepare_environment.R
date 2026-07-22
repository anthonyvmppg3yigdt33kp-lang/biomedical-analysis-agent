#!/usr/bin/env Rscript

options(stringsAsFactors = FALSE, warn = 2L)

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
  if (is.null(value) || !nzchar(value)) {
    stop("Missing required argument --", key, call. = FALSE)
  }
  value
}

write_json_atomic <- function(payload, path) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  temporary <- paste0(path, ".tmp")
  jsonlite::write_json(payload, temporary, auto_unbox = TRUE, pretty = TRUE, null = "null")
  if (file.exists(path) && !file.remove(path)) {
    stop("Cannot remove prior JSON artifact: ", path, call. = FALSE)
  }
  if (!file.rename(temporary, path)) {
    stop("Cannot promote JSON artifact: ", path, call. = FALSE)
  }
}

args <- parse_args(commandArgs(trailingOnly = TRUE))
env_root <- normalizePath(require_arg(args, "env-root"), winslash = "/", mustWork = FALSE)
locked_output <- normalizePath(require_arg(args, "locked-output"), winslash = "/", mustWork = FALSE)
expected_r <- require_arg(args, "expected-r")
expected_seurat <- require_arg(args, "seurat-version")
smoke_h5 <- normalizePath(require_arg(args, "smoke-h5"), winslash = "/", mustWork = TRUE)
repository_snapshot <- require_arg(args, "repository-snapshot")
probe_output <- normalizePath(require_arg(args, "probe-output"), winslash = "/", mustWork = FALSE)
completion_marker <- normalizePath(require_arg(args, "completion-marker"), winslash = "/", mustWork = FALSE)
fault_injection <- require_arg(args, "fault-injection")
bioconductor_pins_path <- normalizePath(
  require_arg(args, "bioconductor-pins"),
  winslash = "/",
  mustWork = TRUE
)
expected_snapshot <- "https://packagemanager.posit.co/cran/2026-04-23"
expected_renv_binary_basename <- "renv_1.2.2.zip"
expected_renv_binary_size <- 2514910
expected_renv_binary_sha256 <- "bcba2170563c65c6d6ed9328e4a624033ad9c5ee4e5bf9132cff7bcc7327cce5"

if (!fault_injection %in% c("none", "before_completion_marker")) {
  stop("Unsupported fault injection: ", fault_injection, call. = FALSE)
}
dir.create(dirname(completion_marker), recursive = TRUE, showWarnings = FALSE)
if (file.exists(completion_marker) && !file.remove(completion_marker)) {
  stop("Cannot clear stale completion marker", call. = FALSE)
}
if (as.character(getRversion()) != expected_r) {
  stop(
    sprintf("Exact R version mismatch: required %s, found %s", expected_r, getRversion()),
    call. = FALSE
  )
}
if (!identical(repository_snapshot, expected_snapshot)) {
  stop(
    sprintf("Repository snapshot mismatch: required %s, received %s", expected_snapshot, repository_snapshot),
    call. = FALSE
  )
}
if (.Platform$OS.type != "windows" || !grepl("w64-mingw32$", R.version$platform)) {
  stop(
    paste0(
      "This frozen teaching environment supports only the reviewed R 4.5 Windows binary platform; found ",
      R.version$platform,
      ". Source compilation and version substitution are prohibited."
    ),
    call. = FALSE
  )
}
processor_architecture <- Sys.getenv("PROCESSOR_ARCHITECTURE", unset = "")
if (!identical(processor_architecture, "AMD64")) {
  stop(
    "PROCESSOR_ARCHITECTURE must be restored to AMD64 in the child-only R environment",
    call. = FALSE
  )
}
options(
  repos = c(CRAN = repository_snapshot),
  pkgType = "binary",
  timeout = max(600L, getOption("timeout", 60L))
)
binary_contrib_url <- utils::contrib.url(repository_snapshot, type = "binary")
binary_available <- utils::available.packages(
  contriburl = binary_contrib_url,
  type = "binary"
)
required <- c(
  "renv", "BiocManager", "Seurat", "SeuratObject", "sctransform",
  "hdf5r", "jsonlite", "digest", "ggplot2", "patchwork"
)
missing_from_binary_index <- setdiff(required, rownames(binary_available))
if (length(missing_from_binary_index)) {
  stop(
    "Required packages absent from the reviewed Windows binary index: ",
    paste(missing_from_binary_index, collapse = ", "),
    call. = FALSE
  )
}
if (binary_available["Seurat", "Version"] != expected_seurat) {
  stop(
    sprintf(
      "Binary index Seurat version mismatch: required %s, found %s at %s",
      expected_seurat,
      binary_available["Seurat", "Version"],
      repository_snapshot
    ),
    call. = FALSE
  )
}
if (binary_available["renv", "Version"] != "1.2.2") {
  stop(
    sprintf(
      "Binary index renv version mismatch: required 1.2.2, found %s at %s",
      binary_available["renv", "Version"],
      repository_snapshot
    ),
    call. = FALSE
  )
}
if (binary_available["hdf5r", "Version"] != "1.3.12") {
  stop(
    sprintf(
      "Binary index hdf5r version mismatch: required 1.3.12, found %s at %s",
      binary_available["hdf5r", "Version"],
      repository_snapshot
    ),
    call. = FALSE
  )
}
if (binary_available["BiocManager", "Version"] != "1.30.27") {
  stop(
    sprintf(
      "Binary index BiocManager version mismatch: required 1.30.27, found %s at %s",
      binary_available["BiocManager", "Version"],
      repository_snapshot
    ),
    call. = FALSE
  )
}
binary_index_gate <- setNames(binary_available[required, "Version"], required)

dir.create(env_root, recursive = TRUE, showWarnings = FALSE)
env_prefix <- paste0(normalizePath(env_root, winslash = "/", mustWork = TRUE), "/")
bootstrap_library <- file.path(env_root, "bootstrap-library")
bootstrap_binary_dir <- file.path(env_root, "bootstrap-binaries")
dir.create(bootstrap_library, recursive = TRUE, showWarnings = FALSE)
dir.create(bootstrap_binary_dir, recursive = TRUE, showWarnings = FALSE)
for (path in c(bootstrap_library, bootstrap_binary_dir)) {
  normalized <- normalizePath(path, winslash = "/", mustWork = TRUE)
  if (!startsWith(paste0(normalized, "/"), env_prefix)) {
    stop("Task-local bootstrap path escapes the environment root", call. = FALSE)
  }
}

# Fetch the exact reviewed binary before loading any renv namespace. PPM's
# Windows PACKAGES index currently exposes the exact version and repository but
# no MD5sum for this row, so the committed size/SHA-256 pin is the fail-closed
# content gate before installation.
if (!identical(binary_available["renv", "Repository"], binary_contrib_url)) {
  stop("renv binary-index repository differs from the reviewed snapshot contribution URL", call. = FALSE)
}
renv_download <- utils::download.packages(
  pkgs = "renv",
  destdir = bootstrap_binary_dir,
  available = binary_available,
  repos = repository_snapshot,
  type = "binary"
)
if (!is.matrix(renv_download) || nrow(renv_download) != 1L || !file.exists(renv_download[1L, 2L])) {
  stop("Exact renv 1.2.2 Windows binary download did not produce one archive", call. = FALSE)
}
renv_binary_path <- normalizePath(renv_download[1L, 2L], winslash = "/", mustWork = TRUE)
if (!identical(basename(renv_binary_path), expected_renv_binary_basename)) {
  stop("Downloaded renv binary basename differs from the exact index-selected artifact", call. = FALSE)
}
renv_binary_size <- unname(file.info(renv_binary_path)$size)
if (!identical(renv_binary_size, expected_renv_binary_size)) {
  stop(
    sprintf("Downloaded renv binary size mismatch: required %s, found %s", expected_renv_binary_size, renv_binary_size),
    call. = FALSE
  )
}
powershell <- Sys.which("powershell")
if (!nzchar(powershell)) stop("Windows PowerShell is required for pre-install SHA-256 verification", call. = FALSE)
hash_script <- "& { param([string]$Target) (Get-FileHash -LiteralPath $Target -Algorithm SHA256).Hash.ToLowerInvariant() }"
hash_output <- system2(
  powershell,
  args = c("-NoProfile", "-NonInteractive", "-Command", shQuote(hash_script), shQuote(renv_binary_path)),
  stdout = TRUE,
  stderr = TRUE
)
hash_status <- attr(hash_output, "status")
if (!is.null(hash_status) && hash_status != 0L) {
  stop("PowerShell Get-FileHash failed while hashing the task-local renv binary", call. = FALSE)
}
hash_candidates <- tolower(trimws(hash_output))
hash_candidates <- hash_candidates[grepl("^[0-9a-f]{64}$", hash_candidates)]
if (length(hash_candidates) != 1L) stop("Cannot parse one SHA-256 from PowerShell Get-FileHash output", call. = FALSE)
renv_binary_sha256 <- hash_candidates[[1L]]
if (!identical(renv_binary_sha256, expected_renv_binary_sha256)) {
  stop(
    sprintf("Downloaded renv binary SHA-256 mismatch: required %s, found %s", expected_renv_binary_sha256, renv_binary_sha256),
    call. = FALSE
  )
}

.libPaths(c(bootstrap_library, .Library))
bootstrap_description <- tryCatch(
  utils::packageDescription("renv", lib.loc = bootstrap_library),
  warning = function(...) NULL,
  error = function(...) NULL
)
if (is.null(bootstrap_description) || !identical(as.character(bootstrap_description$Version), "1.2.2")) {
  if (!is.null(bootstrap_description)) utils::remove.packages("renv", lib = bootstrap_library)
  utils::install.packages(
    pkgs = renv_binary_path,
    lib = bootstrap_library,
    repos = NULL,
    type = "binary",
    dependencies = NA
  )
}
bootstrap_renv_version <- as.character(utils::packageVersion("renv", lib.loc = bootstrap_library))
if (!identical(bootstrap_renv_version, "1.2.2")) {
  stop("Task-local bootstrap renv is not exact 1.2.2", call. = FALSE)
}
bootstrap_package_path <- normalizePath(
  find.package("renv", lib.loc = bootstrap_library),
  winslash = "/",
  mustWork = TRUE
)
if (!startsWith(paste0(bootstrap_package_path, "/"), paste0(normalizePath(bootstrap_library, winslash = "/"), "/"))) {
  stop("renv bootstrap was not loaded from the task-local bootstrap library", call. = FALSE)
}
loadNamespace("renv", lib.loc = bootstrap_library)
if (!identical(as.character(utils::packageVersion("renv", lib.loc = bootstrap_library)), "1.2.2")) {
  stop("Loaded bootstrap renv namespace version mismatch", call. = FALSE)
}
if (!file.exists(file.path(env_root, "renv", "activate.R"))) {
  getExportedValue("renv", "init")(project = env_root, bare = TRUE, restart = FALSE)
}
project_library <- normalizePath(
  getExportedValue("renv", "paths")$library(project = env_root),
  winslash = "/",
  mustWork = FALSE
)
if ("renv" %in% loadedNamespaces()) {
  unloadNamespace("renv")
}
if ("renv" %in% loadedNamespaces()) {
  stop("Task-local bootstrap renv namespace could not be unloaded before project provisioning", call. = FALSE)
}
options(repos = c(CRAN = repository_snapshot), pkgType = "binary")
dir.create(project_library, recursive = TRUE, showWarnings = FALSE)
if (!startsWith(paste0(project_library, "/"), env_prefix)) {
  stop("renv project library escapes the task-local environment root", call. = FALSE)
}
.libPaths(c(project_library, .Library))

installed_version <- function(package) {
  description <- tryCatch(
    utils::packageDescription(package, lib.loc = project_library),
    warning = function(...) NULL,
    error = function(...) NULL
  )
  if (is.null(description)) return(NA_character_)
  as.character(description$Version)
}

installed <- vapply(required, installed_version, character(1L))
# Reviewed Windows-binary pins-first transaction. dependencies=NA installs only
# required Depends/Imports/LinkingTo dependencies from the same binary snapshot.
if (is.na(installed[["renv"]]) || installed[["renv"]] != "1.2.2") {
  if (!is.na(installed[["renv"]])) utils::remove.packages("renv", lib = project_library)
  utils::install.packages(
    pkgs = renv_binary_path,
    lib = project_library,
    repos = NULL,
    type = "binary",
    dependencies = NA
  )
}

install_snapshot_packages <- function(packages, label) {
  for (attempt in seq_len(2L)) {
    observed <- vapply(packages, installed_version, character(1L))
    needed <- packages[is.na(observed) | observed != binary_index_gate[packages]]
    if (!length(needed)) return(invisible(TRUE))
    message(sprintf("%s binary install attempt %d/2: %s", label, attempt, paste(needed, collapse = ", ")))
    utils::install.packages(
      pkgs = needed,
      lib = project_library,
      repos = repository_snapshot,
      type = "binary",
      dependencies = NA
    )
  }
  observed <- vapply(packages, installed_version, character(1L))
  needed <- packages[is.na(observed) | observed != binary_index_gate[packages]]
  if (length(needed)) {
    stop(label, " exact Windows binary transaction failed after two attempts: ", paste(needed, collapse = ", "), call. = FALSE)
  }
  invisible(TRUE)
}

preinstall_order <- c(
  "BiocManager", "hdf5r", "SeuratObject", "sctransform",
  "jsonlite", "digest", "ggplot2", "patchwork"
)
install_snapshot_packages(preinstall_order, "direct prerequisite")

installed <- vapply(required, installed_version, character(1L))
install_snapshot_packages("Seurat", "Seurat")

installed <- vapply(required, installed_version, character(1L))
if (anyNA(installed)) {
  stop("Task-local environment is missing packages: ", paste(names(installed)[is.na(installed)], collapse = ", "), call. = FALSE)
}
if (installed[["Seurat"]] != expected_seurat) {
  stop(
    sprintf("Exact Seurat version mismatch after provision: required %s, found %s", expected_seurat, installed[["Seurat"]]),
    call. = FALSE
  )
}
if (installed[["renv"]] != "1.2.2") {
  stop(
    sprintf("Exact task-local renv version mismatch after provision: required 1.2.2, found %s", installed[["renv"]]),
    call. = FALSE
  )
}
if (installed[["hdf5r"]] != "1.3.12") {
  stop(
    sprintf("Exact hdf5r version mismatch after provision: required 1.3.12, found %s", installed[["hdf5r"]]),
    call. = FALSE
  )
}
if (installed[["BiocManager"]] != "1.30.27") {
  stop(
    sprintf(
      "Exact BiocManager version mismatch after provision: required 1.30.27, found %s",
      installed[["BiocManager"]]
    ),
    call. = FALSE
  )
}
if (any(installed != binary_index_gate[names(installed)])) {
  mismatched <- names(installed)[installed != binary_index_gate[names(installed)]]
  stop(
    "Installed package versions differ from the reviewed binary index: ",
    paste(sprintf("%s=%s (index %s)", mismatched, installed[mismatched], binary_index_gate[mismatched]), collapse = ", "),
    call. = FALSE
  )
}

# Install one fully frozen Bioconductor 3.21 dependency cohort.  Every archive
# is downloaded and hash/DESCRIPTION verified before any cohort package is
# installed.  The sole source-format archive is platform-independent annotation
# data with NeedsCompilation=no; source compilation is never allowed.
pins <- jsonlite::read_json(bioconductor_pins_path, simplifyVector = FALSE)
pins_sha256 <- digest::digest(file = bioconductor_pins_path, algo = "sha256", serialize = FALSE)
expected_bioc_repositories <- list(
  BioCsoft = "https://bioconductor.org/packages/3.21/bioc",
  BioCann = "https://bioconductor.org/packages/3.21/data/annotation",
  BioCexp = "https://bioconductor.org/packages/3.21/data/experiment",
  CRAN = expected_snapshot
)
if (!identical(pins$schema_version, "1.0") ||
    !identical(pins$bioconductor_release, "3.21") ||
    !identical(pins$bioconductor_version, "3.21.1") ||
    !identical(pins$exact_runtime_r, expected_r) ||
    !identical(pins$supported_platform, "x86_64-w64-mingw32") ||
    !identical(pins$repositories, expected_bioc_repositories) ||
    !identical(pins$requested$glmGamPoi, "1.20.0") ||
    !identical(pins$requested$SparseArray, "1.8.1")) {
  stop("Bioconductor pin document violates the reviewed 3.21/R 4.5.3 contract", call. = FALSE)
}
closure <- vapply(pins$dependency_closure, identity, character(1L))
install_order <- vapply(pins$install_order, identity, character(1L))
archive_records <- pins$archives
archive_packages <- vapply(archive_records, function(record) record$package, character(1L))
if (anyDuplicated(closure) || anyDuplicated(install_order) || anyDuplicated(archive_packages) ||
    !setequal(closure, install_order) || !setequal(closure, archive_packages) ||
    length(closure) != 47L) {
  stop("Bioconductor pin document does not contain one exact 47-package closure", call. = FALSE)
}
names(archive_records) <- archive_packages
archive_urls <- vapply(archive_records, function(record) record$url, character(1L))
if (any(grepl("/3\\.(22|23)/", archive_urls)) ||
    any(grepl("bioconductor[.]org", archive_urls) & !grepl("/packages/3[.]21/", archive_urls)) ||
    any(vapply(archive_records, function(record) {
      !identical(basename(record$url), record$archive)
    }, logical(1L)))) {
  stop("Bioconductor pin document contains a cross-release or basename-mismatched URL", call. = FALSE)
}
source_records <- archive_records[vapply(
  archive_records,
  function(record) identical(record$archive_type, "source_data_no_compilation"),
  logical(1L)
)]
if (!identical(names(source_records), "GenomeInfoDbData") ||
    !identical(source_records[[1L]]$needs_compilation, FALSE)) {
  stop("Only GenomeInfoDbData NeedsCompilation=no may use a source-format archive", call. = FALSE)
}
if (any(vapply(archive_records[names(archive_records) != "GenomeInfoDbData"], function(record) {
  !identical(record$archive_type, "win.binary") || !endsWith(record$archive, ".zip")
}, logical(1L)))) {
  stop("Every non-data cohort archive must be an official Windows binary", call. = FALSE)
}

bioc_archive_dir <- file.path(env_root, "bioconductor-3.21-archives")
dir.create(bioc_archive_dir, recursive = TRUE, showWarnings = FALSE)
bioc_archive_dir <- normalizePath(bioc_archive_dir, winslash = "/", mustWork = TRUE)
if (!startsWith(paste0(bioc_archive_dir, "/"), env_prefix)) {
  stop("Bioconductor archive cache escapes the task-local environment root", call. = FALSE)
}

read_archive_description <- function(path, record) {
  member <- paste0(record$package, "/DESCRIPTION")
  if (identical(record$archive_type, "win.binary")) {
    listing <- utils::unzip(path, list = TRUE)
    if (!(member %in% listing$Name)) stop("Binary archive lacks DESCRIPTION: ", record$package, call. = FALSE)
    connection <- unz(path, member, open = "rt")
    on.exit(close(connection), add = TRUE)
    return(as.list(read.dcf(connection)[1L, , drop = TRUE]))
  }
  members <- utils::untar(path, list = TRUE)
  if (!(member %in% members)) stop("Data archive lacks DESCRIPTION: ", record$package, call. = FALSE)
  extract_root <- tempfile(pattern = "baa-bioc-description-", tmpdir = env_root)
  dir.create(extract_root, recursive = TRUE, showWarnings = FALSE)
  on.exit(unlink(extract_root, recursive = TRUE, force = TRUE), add = TRUE)
  utils::untar(path, files = member, exdir = extract_root)
  as.list(read.dcf(file.path(extract_root, member))[1L, , drop = TRUE])
}

archive_paths <- setNames(character(length(archive_records)), names(archive_records))
for (package in names(archive_records)) {
  record <- archive_records[[package]]
  destination <- file.path(bioc_archive_dir, record$archive)
  if (!file.exists(destination)) {
    download_error <- NULL
    for (attempt in seq_len(3L)) {
      if (file.exists(destination)) file.remove(destination)
      download_error <- tryCatch({
        utils::download.file(record$url, destination, mode = "wb", quiet = TRUE)
        NULL
      }, error = function(condition) condition)
      if (is.null(download_error) && file.exists(destination)) break
      download_detail <- if (inherits(download_error, "condition")) {
        conditionMessage(download_error)
      } else {
        "download did not create the expected archive"
      }
      transient_failure <- grepl(
        paste(
          "ssl connect error", "timed? out", "timeout", "connection reset",
          "could not connect", "couldn't connect", "failure when receiving data",
          "temporary failure", "http status was ['\"]5[0-9][0-9]",
          sep = "|"
        ),
        tolower(download_detail),
        perl = TRUE
      )
      if (!transient_failure) {
        stop(
          "Non-transient pinned archive download failure for ", package,
          ": ", download_detail,
          call. = FALSE
        )
      }
      if (attempt < 3L) {
        message(
          "Exact pinned archive download attempt ", attempt,
          "/3 failed for ", package, "; retrying the same URL"
        )
        Sys.sleep(2L)
      }
    }
    if (!is.null(download_error) || !file.exists(destination)) {
      stop(
        "Exact pinned archive download failed after 3 attempts for ", package,
        ": ", download_detail,
        call. = FALSE
      )
    }
  }
  destination <- normalizePath(destination, winslash = "/", mustWork = TRUE)
  if (!identical(unname(file.info(destination)$size), as.numeric(record$size_bytes))) {
    stop("Pinned archive size mismatch for ", package, call. = FALSE)
  }
  observed_sha256 <- digest::digest(file = destination, algo = "sha256", serialize = FALSE)
  if (!identical(observed_sha256, record$sha256)) {
    stop("Pinned archive SHA-256 mismatch for ", package, call. = FALSE)
  }
  description <- read_archive_description(destination, record)
  if (!identical(description$Package, package) || !identical(description$Version, record$version)) {
    stop("Pinned archive DESCRIPTION mismatch for ", package, call. = FALSE)
  }
  if (identical(package, "GenomeInfoDbData") &&
      !identical(tolower(description$NeedsCompilation), "no")) {
    stop("GenomeInfoDbData unexpectedly requires compilation", call. = FALSE)
  }
  archive_paths[[package]] <- destination
}
computed_archive_manifest_sha256 <- digest::digest(
  paste(vapply(archive_records, function(record) {
    paste(
      record$package, record$version, record$archive, record$size_bytes,
      record$sha256, record$repository, sep = "|"
    )
  }, character(1L)), collapse = "\n"),
  algo = "sha256",
  serialize = FALSE
)
if (!identical(computed_archive_manifest_sha256, pins$archive_manifest_sha256)) {
  stop("Computed Bioconductor archive manifest hash differs from the committed pin", call. = FALSE)
}

if ("jsonlite" %in% loadedNamespaces()) unloadNamespace("jsonlite")
for (package in install_order) {
  record <- archive_records[[package]]
  observed <- installed_version(package)
  if (!is.na(observed) && !identical(observed, record$version)) {
    utils::remove.packages(package, lib = project_library)
    observed <- NA_character_
  }
  if (is.na(observed)) {
    if (identical(record$archive_type, "source_data_no_compilation")) {
      utils::install.packages(
        archive_paths[[package]], lib = project_library, repos = NULL,
        type = "source", dependencies = FALSE, quiet = TRUE
      )
    } else {
      utils::install.packages(
        archive_paths[[package]], lib = project_library, repos = NULL,
        type = "win.binary", dependencies = FALSE, quiet = TRUE
      )
    }
  }
  installed_path <- normalizePath(
    find.package(package, lib.loc = project_library), winslash = "/", mustWork = TRUE
  )
  if (!startsWith(paste0(installed_path, "/"), paste0(project_library, "/")) ||
      !identical(installed_version(package), record$version)) {
    stop("Installed pinned package version/path mismatch for ", package, call. = FALSE)
  }
}

installed_bioc <- vapply(closure, installed_version, character(1L))
expected_bioc_versions <- vapply(archive_records[closure], function(record) record$version, character(1L))
if (anyNA(installed_bioc) || !identical(unname(installed_bioc), unname(expected_bioc_versions))) {
  stop("Installed Bioconductor 3.21 closure differs from the frozen archive cohort", call. = FALSE)
}
all_installed <- installed
all_installed[names(installed_bioc)] <- installed_bioc
installed <- all_installed
bioc_package_paths <- vapply(closure, function(package) {
  path <- normalizePath(find.package(package, lib.loc = project_library), winslash = "/", mustWork = TRUE)
  if (!startsWith(paste0(path, "/"), paste0(project_library, "/"))) {
    stop("Pinned package escaped the project library: ", package, call. = FALSE)
  }
  substring(path, nchar(env_prefix) + 1L)
}, character(1L))
for (package in c("SparseArray", "glmGamPoi", "BiocVersion")) {
  namespace <- loadNamespace(package, lib.loc = project_library)
  loaded_path <- normalizePath(getNamespaceInfo(namespace, "path"), winslash = "/", mustWork = TRUE)
  if (!startsWith(paste0(loaded_path, "/"), paste0(project_library, "/"))) {
    stop("Loaded key Bioconductor namespace escaped the project library: ", package, call. = FALSE)
  }
}
if (!identical(installed[["BiocVersion"]], "3.21.1") ||
    !identical(installed[["glmGamPoi"]], "1.20.0") ||
    !identical(installed[["SparseArray"]], "1.8.1")) {
  stop("Exact BiocVersion/glmGamPoi/SparseArray contract failed", call. = FALSE)
}
glm_smoke <- glmGamPoi::glm_gp(matrix(c(0L, 1L, 2L, 3L), nrow = 2L), design = ~1)
if (is.null(glm_smoke$Beta) || any(!is.finite(glm_smoke$Beta))) {
  stop("glmGamPoi numeric smoke returned non-finite coefficients", call. = FALSE)
}
rm(glm_smoke)

bioc_repositories <- unlist(expected_bioc_repositories, use.names = TRUE)
options(repos = bioc_repositories, pkgType = "binary")
observed_biocmanager_release <- as.character(BiocManager::version())
if (!identical(observed_biocmanager_release, "3.21")) {
  stop(
    "BiocManager release resolution mismatch: required 3.21, found ",
    observed_biocmanager_release,
    call. = FALSE
  )
}
annotation_source_repository <- expected_bioc_repositories[["BioCann"]]
annotation_source_contrib <- utils::contrib.url(annotation_source_repository, type = "source")
annotation_source_index <- utils::available.packages(
  repos = annotation_source_repository,
  type = "source"
)
if (!("GenomeInfoDbData" %in% rownames(annotation_source_index))) {
  stop("BioCann source index does not resolve GenomeInfoDbData", call. = FALSE)
}
annotation_source_record <- annotation_source_index["GenomeInfoDbData", , drop = TRUE]
if (!identical(unname(annotation_source_record[["Version"]]), "1.2.14") ||
    !identical(tolower(unname(annotation_source_record[["NeedsCompilation"]])), "no")) {
  stop("BioCann source index GenomeInfoDbData metadata violates the 1.2.14/NeedsCompilation=no pin", call. = FALSE)
}
annotation_source_index_gate_sha256 <- digest::digest(
  paste(
    "GenomeInfoDbData", annotation_source_record[["Version"]],
    annotation_source_record[["NeedsCompilation"]], annotation_source_contrib,
    sep = "|"
  ),
  algo = "sha256",
  serialize = FALSE
)

required_apis <- list(
  Load10X_Spatial = c("data.dir", "filename", "assay", "slice", "filter.matrix"),
  SpatialDimPlot = c("object", "group.by", "images", "image.scale", "pt.size.factor", "image.alpha"),
  SpatialFeaturePlot = c("object", "features", "images", "image.scale", "pt.size.factor", "image.alpha"),
  GetImage = c("object", "mode"),
  ScaleFactors = c("object")
)
for (api in names(required_apis)) {
  fun <- getExportedValue("Seurat", api)
  available <- names(formals(fun))
  missing <- setdiff(required_apis[[api]], available)
  if (length(missing)) {
    stop(api, " API smoke test failed; missing formals: ", paste(missing, collapse = ", "), call. = FALSE)
  }
}
for (api in c("GetTissueCoordinates", "Images", "Assays", "Cells", "Embeddings", "DefaultAssay")) {
  getExportedValue("SeuratObject", api)
}

# A namespace check is insufficient because hdf5r is a Seurat Suggests package.
# Exercise the real reader against the frozen tutorial H5 before snapshot/freeze.
h5_smoke <- Seurat::Read10X_h5(smoke_h5, use.names = TRUE, unique.features = TRUE)
if (is.list(h5_smoke)) {
  if ("Gene Expression" %in% names(h5_smoke)) {
    h5_matrix <- h5_smoke[["Gene Expression"]]
  } else if (length(h5_smoke) == 1L) {
    h5_matrix <- h5_smoke[[1L]]
  } else {
    stop("H5 smoke test found multiple modalities without Gene Expression", call. = FALSE)
  }
} else {
  h5_matrix <- h5_smoke
}
if (nrow(h5_matrix) < 1L || ncol(h5_matrix) < 1L) {
  stop("Read10X_h5 smoke test returned an empty matrix", call. = FALSE)
}
h5_smoke_dimensions <- c(features = nrow(h5_matrix), barcodes = ncol(h5_matrix))
rm(h5_smoke, h5_matrix)
invisible(gc())

# Generate the lock in a clean child process whose first library is the
# task-local project library. Both the bootstrap and project copies of renv
# come from the hash-verified 1.2.2 binary; no host renv namespace is used.
snapshot_script <- file.path(env_root, "snapshot-with-task-local-renv.R")
snapshot_status_output <- file.path(env_root, "renv-status.json")
snapshot_lines <- c(
  paste0("project_library <- ", deparse(project_library)),
  paste0("project_root <- ", deparse(env_root)),
  paste0("repositories <- ", paste(deparse(bioc_repositories), collapse = "")),
  paste0("status_output <- ", deparse(snapshot_status_output)),
  ".libPaths(c(project_library, .Library))",
  "base_library <- normalizePath(.Library, winslash = '/', mustWork = TRUE)",
  "if (!startsWith(paste0(base_library, '/'), paste0(normalizePath(R.home(), winslash = '/', mustWork = TRUE), '/'))) stop('base library is not inside exact R home')",
  "reviewed_recommended <- c('boot', 'class', 'cluster', 'codetools', 'foreign', 'KernSmooth', 'MASS', 'mgcv', 'nlme', 'nnet', 'rpart', 'spatial', 'survival')",
  "base_installed <- rownames(utils::installed.packages(lib.loc = base_library))",
  "project_installed <- rownames(utils::installed.packages(lib.loc = project_library))",
  "if (!all(reviewed_recommended %in% base_installed) || any(reviewed_recommended %in% project_installed)) stop('reviewed R recommended-package library boundary mismatch')",
  # Provenance lookup must include BioCann's platform-independent source index
  # so GenomeInfoDbData 1.2.14 is resolvable. This child only snapshots/statuses;
  # the preceding installation transaction remains binary-only except for the
  # one hash-pinned NeedsCompilation=no data archive.
  "options(warn = 2L, repos = repositories, pkgType = 'source')",
  "if (as.character(utils::packageVersion('renv', lib.loc = project_library)) != '1.2.2') stop('task-local renv 1.2.2 is required')",
  "if (as.character(utils::packageVersion('BiocManager', lib.loc = project_library)) != '1.30.27') stop('task-local BiocManager 1.30.27 is required')",
  "if (as.character(BiocManager::version()) != '3.21') stop('BiocManager must resolve exact release 3.21')",
  "renv::settings$bioconductor.version('3.21', project = project_root)",
  "renv::settings$snapshot.type('all', project = project_root)",
  "renv::snapshot(project = project_root, prompt = FALSE)",
  "status <- renv::status(project = project_root, library = c(project_library, base_library), sources = TRUE, cache = FALSE)",
  "status_diff <- get('renv_lockfile_diff_packages', asNamespace('renv'))(status$library, status$lockfile)",
  "status_differences <- lapply(names(status_diff), function(package) list(package = package, action = unname(as.character(status_diff[[package]]))))",
  "restore_actions <- get('renv_actions_restore', asNamespace('renv'))(project = project_root, library = c(project_library, base_library), lockfile = file.path(project_root, 'renv.lock'), clean = FALSE)",
  "if (!is.data.frame(restore_actions)) stop('renv restore plan did not return a data frame')",
  "restore_action_count <- nrow(restore_actions)",
  "project_only_mismatch <- lapply(reviewed_recommended, function(package) list(package = package, action = 'install'))",
  "jsonlite::write_json(list(schema_version = '1.0', status = if (isTRUE(status$synchronized) && length(status_diff) == 0L && restore_action_count == 0L) 'passed' else 'blocked', synchronized = isTRUE(status$synchronized), sources_checked = TRUE, status_difference_count = length(status_diff), status_differences = status_differences, restore_action_count = restore_action_count, installation_package_type = 'binary', provenance_lookup_package_type = 'source', project_library_role = 'task_local_renv_project_library', validated_library_search_set = list('task_local_renv_project_library', 'exact_R_home_library'), r_home_base_library_asserted = TRUE, base_library_role = 'exact_R_recommended_packages', project_only_mismatch_difference_count = length(project_only_mismatch), project_only_mismatch_differences = project_only_mismatch, project_only_mismatch_provenance = 'v4_failed_status_scope_audit', bioconductor_release = as.character(BiocManager::version()), repositories = as.list(repositories)), status_output, auto_unbox = TRUE, pretty = TRUE)",
  "if (!isTRUE(status$synchronized) || length(status_diff) != 0L) stop('renv status is not synchronized after snapshot; see renv-status.json')",
  "if (restore_action_count != 0L) stop('renv restore plan is non-empty immediately after snapshot; see renv-status.json')"
)
writeLines(snapshot_lines, snapshot_script, useBytes = TRUE)
snapshot_status <- system2(
  command = file.path(R.home("bin"), "Rscript.exe"),
  args = c("--vanilla", shQuote(snapshot_script)),
  stdout = "",
  stderr = ""
)
if (!identical(as.integer(snapshot_status), 0L)) {
  stop(
    "Task-local renv 1.2.2 snapshot subprocess failed with exit status ",
    snapshot_status,
    "; diagnostic script retained at ",
    snapshot_script,
    call. = FALSE
  )
}
if (!file.remove(snapshot_script)) stop("Cannot remove successful snapshot helper", call. = FALSE)
lock_path <- file.path(env_root, "renv.lock")
if (!file.exists(lock_path)) stop("renv snapshot did not create renv.lock", call. = FALSE)
if (!file.exists(snapshot_status_output)) {
  stop("renv snapshot did not create synchronized status evidence", call. = FALSE)
}

snapshot_status_payload <- jsonlite::read_json(snapshot_status_output, simplifyVector = FALSE)
if (!identical(snapshot_status_payload$status, "passed") ||
    !identical(snapshot_status_payload$synchronized, TRUE) ||
    !identical(snapshot_status_payload$sources_checked, TRUE) ||
    !identical(snapshot_status_payload$status_difference_count, 0L) ||
    length(snapshot_status_payload$status_differences) != 0L ||
    !identical(snapshot_status_payload$restore_action_count, 0L) ||
    !identical(snapshot_status_payload$installation_package_type, "binary") ||
    !identical(snapshot_status_payload$provenance_lookup_package_type, "source") ||
    !identical(snapshot_status_payload$project_library_role, "task_local_renv_project_library") ||
    !identical(unlist(snapshot_status_payload$validated_library_search_set), c("task_local_renv_project_library", "exact_R_home_library")) ||
    !identical(snapshot_status_payload$r_home_base_library_asserted, TRUE) ||
    !identical(snapshot_status_payload$base_library_role, "exact_R_recommended_packages") ||
    !identical(snapshot_status_payload$project_only_mismatch_difference_count, 13L) ||
    !identical(vapply(snapshot_status_payload$project_only_mismatch_differences, function(record) record$package, character(1L)), c("boot", "class", "cluster", "codetools", "foreign", "KernSmooth", "MASS", "mgcv", "nlme", "nnet", "rpart", "spatial", "survival")) ||
    any(vapply(snapshot_status_payload$project_only_mismatch_differences, function(record) !identical(record$action, "install"), logical(1L))) ||
    !identical(snapshot_status_payload$project_only_mismatch_provenance, "v4_failed_status_scope_audit") ||
    !identical(snapshot_status_payload$bioconductor_release, "3.21") ||
    !identical(unlist(snapshot_status_payload$repositories, use.names = TRUE), bioc_repositories)) {
  stop("renv synchronized status evidence violates the reviewed repository contract", call. = FALSE)
}

lock_payload <- jsonlite::read_json(lock_path, simplifyVector = FALSE)
lock_repositories <- lock_payload$R$Repositories
lock_repository_urls <- vapply(lock_repositories, function(record) record$URL, character(1L))
lock_repository_names <- vapply(lock_repositories, function(record) record$Name, character(1L))
locked_repository_vector <- setNames(lock_repository_urls, lock_repository_names)
if (!identical(locked_repository_vector[names(bioc_repositories)], bioc_repositories)) {
  stop("renv.lock does not freeze the reviewed CRAN/Bioconductor repositories", call. = FALSE)
}
locked_gates <- c(
  renv = "1.2.2", BiocManager = "1.30.27", Seurat = expected_seurat, hdf5r = "1.3.12",
  BiocVersion = "3.21.1", glmGamPoi = "1.20.0", SparseArray = "1.8.1"
)
for (package in names(locked_gates)) {
  locked_version <- lock_payload$Packages[[package]]$Version
  if (!identical(locked_version, locked_gates[[package]])) {
    stop(
      sprintf("renv.lock %s version mismatch: required %s, found %s", package, locked_gates[[package]], locked_version),
      call. = FALSE
    )
  }
}
locked_bioc_versions <- vapply(closure, function(package) {
  record <- lock_payload$Packages[[package]]
  if (is.null(record) || is.null(record$Version)) return(NA_character_)
  as.character(record$Version)
}, character(1L))
if (anyNA(locked_bioc_versions) ||
    !identical(unname(locked_bioc_versions), unname(expected_bioc_versions))) {
  stop("renv.lock does not contain the exact full Bioconductor 3.21 closure", call. = FALSE)
}

lock_sha256 <- digest::digest(file = lock_path, algo = "sha256", serialize = FALSE)
renv_status_sha256 <- digest::digest(
  file = snapshot_status_output, algo = "sha256", serialize = FALSE
)
binary_index_gate_sha256 <- digest::digest(
  paste(names(binary_index_gate), binary_index_gate, sep = "=", collapse = "\n"),
  algo = "sha256",
  serialize = FALSE
)
shutdown_mode <- "native_exit"
h5_sha256 <- digest::digest(file = smoke_h5, algo = "sha256", serialize = FALSE)
probe_payload <- list(
  schema_version = "1.0",
  status = "passed",
  r_version = as.character(getRversion()),
  platform = R.version$platform,
  task_local_renv_version = installed[["renv"]],
  BiocManager_version = installed[["BiocManager"]],
  bootstrap_renv_version = bootstrap_renv_version,
  bootstrap_renv_source = "task_local_reviewed_snapshot_binary",
  bootstrap_renv_binary_sha256 = renv_binary_sha256,
  bootstrap_renv_binary_index_repository = binary_available["renv", "Repository"],
  bootstrap_renv_binary_index_md5_available = !is.na(binary_available["renv", "MD5sum"]),
  seurat_version = installed[["Seurat"]],
  hdf5r_version = installed[["hdf5r"]],
  repository_snapshot = repository_snapshot,
  binary_index_gate_sha256 = binary_index_gate_sha256,
  renv_lock_sha256 = lock_sha256,
  renv_status_sha256 = renv_status_sha256,
  renv_status_synchronized = TRUE,
  renv_status_difference_count = 0L,
  renv_restore_action_count = 0L,
  installation_package_type = "binary",
  provenance_lookup_package_type = "source",
  annotation_source_index_gate_sha256 = annotation_source_index_gate_sha256,
  bioconductor_release = "3.21",
  bioconductor_version = "3.21.1",
  bioconductor_pins_sha256 = pins_sha256,
  bioconductor_archive_manifest_sha256 = computed_archive_manifest_sha256,
  glmGamPoi_version = installed[["glmGamPoi"]],
  SparseArray_version = installed[["SparseArray"]],
  processor_architecture = processor_architecture,
  shutdown_mode = shutdown_mode,
  seurat_spatial_api_smoke = TRUE,
  read10x_h5_smoke = list(
    passed = TRUE,
    input_basename = basename(smoke_h5),
    input_sha256 = h5_sha256,
    matrix_dimensions = as.list(h5_smoke_dimensions)
  )
)
write_json_atomic(probe_payload, probe_output)
probe_sha256 <- digest::digest(file = probe_output, algo = "sha256", serialize = FALSE)
payload <- list(
  schema_version = "1.0",
  status = "frozen",
  backend = "r",
  platform = R.version$platform,
  r_version = as.character(getRversion()),
  seurat_version = installed[["Seurat"]],
  task_local_renv_version = installed[["renv"]],
  bootstrap_renv_version = bootstrap_renv_version,
  bootstrap_renv_role = "task_local_snapshot_bootstrap_not_host",
  bootstrap = list(
    host_renv_required = FALSE,
    library_basename = basename(bootstrap_library),
    binary_basename = basename(renv_binary_path),
    binary_version = "1.2.2",
    binary_sha256 = renv_binary_sha256,
    expected_binary_sha256 = expected_renv_binary_sha256,
    binary_index_repository = binary_available["renv", "Repository"],
    binary_index_md5_available = !is.na(binary_available["renv", "MD5sum"]),
    binary_size_bytes = renv_binary_size,
    expected_binary_size_bytes = expected_renv_binary_size
  ),
  packages = as.list(installed),
  repository = list(
    name = "Posit Public Package Manager CRAN snapshot",
    snapshot_url = repository_snapshot,
    binary_contrib_url = binary_contrib_url,
    package_type = "binary",
    provenance_lookup_package_type = "source",
    dependencies_argument = "NA",
    binary_index_gate = as.list(binary_index_gate),
    binary_index_gate_sha256 = binary_index_gate_sha256,
    bioconductor_repositories = as.list(bioc_repositories),
    annotation_source_index_gate = list(
      repository = annotation_source_repository,
      contrib_url = annotation_source_contrib,
      package = "GenomeInfoDbData",
      version = "1.2.14",
      NeedsCompilation = "no",
      sha256 = annotation_source_index_gate_sha256
    )
  ),
  shutdown_mode = shutdown_mode,
  bioconductor = list(
    release = "3.21",
    version = "3.21.1",
    pins_basename = basename(bioconductor_pins_path),
    pins_sha256 = pins_sha256,
    archive_manifest_sha256 = computed_archive_manifest_sha256,
    closure_size = length(closure),
    glmGamPoi_version = installed[["glmGamPoi"]],
    SparseArray_version = installed[["SparseArray"]],
    GenomeInfoDbData_version = installed[["GenomeInfoDbData"]],
    same_release_closure = TRUE,
    source_compilation_allowed = FALSE,
    source_data_no_compilation_packages = list("GenomeInfoDbData"),
    cross_release_packages_detected = FALSE,
    lock_closure_version_match = TRUE,
    restore_plan_empty = TRUE,
    package_paths_relative = as.list(bioc_package_paths)
  ),
  renv_lock_sha256 = lock_sha256,
  renv_status = list(
    basename = basename(snapshot_status_output),
    sha256 = renv_status_sha256,
    synchronized = TRUE,
    sources_checked = TRUE,
    status_difference_count = 0L,
    restore_action_count = 0L,
    installation_package_type = "binary",
    provenance_lookup_package_type = "source",
    project_library_role = "task_local_renv_project_library",
    validated_library_search_set = list("task_local_renv_project_library", "exact_R_home_library"),
    r_home_base_library_asserted = TRUE,
    base_library_role = "exact_R_recommended_packages",
    project_only_mismatch_difference_count = 13L,
    project_only_mismatch_packages = as.list(c(
      "boot", "class", "cluster", "codetools", "foreign", "KernSmooth", "MASS",
      "mgcv", "nlme", "nnet", "rpart", "spatial", "survival"
    )),
    bioconductor_release = observed_biocmanager_release
  ),
  environment_root_basename = basename(env_root),
  probe = list(
    basename = basename(probe_output),
    sha256 = probe_sha256,
    status = "passed"
  ),
  verification = list(
    exact_r = TRUE,
    exact_task_local_renv = TRUE,
    exact_bootstrap_renv = TRUE,
    bootstrap_renv_excluded_from_run_library = TRUE,
    host_renv_not_required = TRUE,
    bootstrap_binary_index_version_before_install = TRUE,
    bootstrap_binary_index_repository_before_install = TRUE,
    bootstrap_binary_pinned_sha256_before_install = TRUE,
    exact_seurat = TRUE,
    exact_hdf5r = TRUE,
    windows_binary_snapshot_gate = TRUE,
    binary_only_install = TRUE,
    renv_lock_snapshot_url = TRUE,
    exact_BiocManager_1_30_27 = TRUE,
    renv_status_synchronized = TRUE,
    renv_restore_plan_empty = TRUE,
    provenance_source_lookup_only = TRUE,
    annotation_source_index_resolved = TRUE,
    exact_bioconductor_3_21 = TRUE,
    exact_glmGamPoi_1_20_0 = TRUE,
    exact_SparseArray_1_8_1 = TRUE,
    complete_same_release_closure = TRUE,
    all_archives_hash_verified_before_install = TRUE,
    all_archive_descriptions_verified_before_install = TRUE,
    annotation_data_requires_no_compilation = TRUE,
    no_cross_release_packages = TRUE,
    native_r_shutdown = TRUE,
    child_processor_architecture_amd64 = TRUE,
    required_packages = TRUE,
    seurat_spatial_api_smoke = TRUE,
    read10x_h5_smoke = TRUE
  ),
  h5_reader_smoke = list(
    input_basename = basename(smoke_h5),
    input_sha256 = h5_sha256,
    matrix_dimensions = as.list(h5_smoke_dimensions)
  )
)
write_json_atomic(payload, locked_output)
environment_marker_sha256 <- digest::digest(file = locked_output, algo = "sha256", serialize = FALSE)
if (identical(fault_injection, "before_completion_marker")) {
  stop("FAULT_INJECTION_BEFORE_COMPLETION_MARKER", call. = FALSE)
}
completion_payload <- list(
  schema_version = "1.0",
  stage = "environment-provision",
  status = "complete",
  shutdown_mode = shutdown_mode,
  renv_lock_sha256 = lock_sha256,
  renv_status_sha256 = renv_status_sha256,
  probe_sha256 = probe_sha256,
  environment_marker_sha256 = environment_marker_sha256,
  bioconductor_pins_sha256 = pins_sha256,
  bioconductor_archive_manifest_sha256 = computed_archive_manifest_sha256,
  fault_injection = "none"
)
write_json_atomic(completion_payload, completion_marker)
cat(jsonlite::toJSON(payload, auto_unbox = TRUE, pretty = TRUE), "\n")
flush.console()
