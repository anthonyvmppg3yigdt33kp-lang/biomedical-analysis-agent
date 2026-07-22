#!/usr/bin/env Rscript

# Maintainer-only lock builder. Tutorial analysis scripts never install packages;
# authorized environment preparation restores the resulting immutable lock.

args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) {
  stop("usage: build_tutorial_lock.R <task-local-project>", call. = FALSE)
}

expected_r <- "4.5.3"
expected_seurat <- "5.5.0"
expected_renv <- "1.2.2"
snapshot_repo <- "https://packagemanager.posit.co/cran/2026-04-23"
if (as.character(getRversion()) != expected_r) {
  stop(sprintf("R %s required; observed %s", expected_r, getRversion()), call. = FALSE)
}
if (!requireNamespace("renv", quietly = TRUE)) {
  stop("renv must be available to the environment manager", call. = FALSE)
}

project <- normalizePath(args[[1]], winslash = "/", mustWork = FALSE)
if (dir.exists(project) && length(list.files(project, all.files = TRUE, no.. = TRUE))) {
  stop(sprintf("refusing non-empty lock-build project: %s", project), call. = FALSE)
}
dir.create(project, recursive = TRUE, showWarnings = FALSE)
if (.Platform$OS.type != "windows") {
  stop("The v1.0.0 binary lock builder is validated only on Windows.", call. = FALSE)
}
options(repos = c(CRAN = snapshot_repo))

renv::init(project = project, bare = TRUE, restart = FALSE)
available <- utils::available.packages(repos = snapshot_repo, type = "binary")
if (!"Seurat" %in% rownames(available) || available["Seurat", "Version"] != expected_seurat) {
  stop("The frozen snapshot no longer resolves Seurat 5.5.0.", call. = FALSE)
}
if (!"hdf5r" %in% rownames(available) || available["hdf5r", "Version"] != "1.3.12") {
  stop("The frozen snapshot no longer resolves hdf5r 1.3.12.", call. = FALSE)
}
if (!"renv" %in% rownames(available) || available["renv", "Version"] != expected_renv) {
  stop("The frozen snapshot no longer resolves renv 1.2.2.", call. = FALSE)
}
project_library <- renv::paths$library(project = project)
dir.create(project_library, recursive = TRUE, showWarnings = FALSE)
utils::install.packages(
  pkgs = c("Seurat", "hdf5r", "renv"),
  lib = project_library,
  repos = snapshot_repo,
  type = "binary",
  dependencies = NA
)
renv::snapshot(project = project, type = "all", prompt = FALSE, force = TRUE)
renv::load(project = project)

observed <- as.character(utils::packageVersion("Seurat"))
if (observed != expected_seurat) {
  stop(sprintf("Seurat %s required; observed %s", expected_seurat, observed), call. = FALSE)
}
observed_renv <- utils::packageDescription(
  "renv", lib.loc = project_library, fields = "Version"
)
if (!identical(as.character(observed_renv), expected_renv)) {
  stop(sprintf("renv %s required; observed %s", expected_renv, observed_renv), call. = FALSE)
}
cat(sprintf("LOCK_READY R=%s Seurat=%s renv=%s\n", getRversion(), observed, observed_renv))
