#!/usr/bin/env Rscript

args <- commandArgs(trailingOnly = TRUE)
if (!length(args) || length(args) > 2L) {
  stop("usage: ci_r_smoke.R <repository-root> [--check-snapshot]", call. = FALSE)
}

root <- normalizePath(args[[1]], winslash = "/", mustWork = TRUE)
check_snapshot <- length(args) == 2L && identical(args[[2]], "--check-snapshot")
if (length(args) == 2L && !check_snapshot) {
  stop("unknown option: ", args[[2]], call. = FALSE)
}
if (!identical(as.character(getRversion()), "4.5.3")) {
  stop("R 4.5.3 required; observed ", getRversion(), call. = FALSE)
}

all_r <- list.files(
  root,
  pattern = "\\.[Rr]$",
  recursive = TRUE,
  full.names = TRUE,
  all.files = TRUE
)
excluded <- grepl(
  "(?:^|/)(?:\\.git|\\.cache|\\.renv|runs|validation/runtime)(?:/|$)",
  gsub("\\\\", "/", all_r)
)
r_files <- sort(all_r[!excluded])
if (!length(r_files)) stop("no R sources found", call. = FALSE)

parse_failures <- character()
for (path in r_files) {
  tryCatch(
    parse(file = path, keep.source = FALSE),
    error = function(error) {
      relative <- substring(gsub("\\\\", "/", path), nchar(root) + 2L)
      parse_failures <<- c(parse_failures, paste0(relative, ": ", conditionMessage(error)))
    }
  )
}
if (length(parse_failures)) {
  stop("R parse failures:\n", paste(parse_failures, collapse = "\n"), call. = FALSE)
}

snapshot_versions <- NULL
if (check_snapshot) {
  if (.Platform$OS.type != "windows") {
    stop("the reviewed binary snapshot gate is Windows-only", call. = FALSE)
  }
  snapshot <- "https://packagemanager.posit.co/cran/2026-04-23"
  contrib <- utils::contrib.url(snapshot, type = "binary")
  available <- utils::available.packages(contriburl = contrib, type = "binary")
  expected <- c(renv = "1.2.2", Seurat = "5.5.0", hdf5r = "1.3.12")
  missing <- setdiff(names(expected), rownames(available))
  if (length(missing)) {
    stop("reviewed binary index is missing: ", paste(missing, collapse = ", "), call. = FALSE)
  }
  observed <- available[names(expected), "Version"]
  if (!identical(unname(observed), unname(expected))) {
    details <- paste0(names(expected), "=", observed, " (expected ", expected, ")")
    stop("reviewed binary index drift: ", paste(details, collapse = "; "), call. = FALSE)
  }
  snapshot_versions <- as.list(observed)
}

result <- list(
  ok = TRUE,
  r_version = as.character(getRversion()),
  platform = R.version$platform,
  parsed_r_files = length(r_files),
  executed_analysis = FALSE,
  snapshot_checked = check_snapshot,
  snapshot_versions = snapshot_versions
)
dput(result)
