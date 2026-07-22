# Multi-omics methods and validation

## Contents

1. Shared harmonization
2. MOFA2
3. DIABLO
4. SNF
5. MCIA
6. Interpretation limits

All code assumes dependencies are already provisioned in a locked task environment. No recipe installs packages.

## 1. Shared harmonization

```r
required <- c("readr")
missing <- required[!vapply(required, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing)) stop("Missing declared packages: ", paste(missing, collapse = ", "))

read_view <- function(path) {
  x <- readr::read_tsv(path, show_col_types = FALSE)
  sample_id <- x[[1]]
  if (anyDuplicated(sample_id)) stop("Duplicated sample IDs in ", path)
  m <- as.matrix(x[-1]); rownames(m) <- sample_id; storage.mode(m) <- "numeric"
  if (any(!is.finite(m), na.rm = TRUE)) stop("Non-finite values in ", path)
  m
}

views <- list(
  RNA = read_view("rna_matrix.tsv"),
  Protein = read_view("protein_matrix.tsv"),
  Metabolome = read_view("metabolome_matrix.tsv")
)
metadata <- readr::read_tsv("sample_metadata.tsv", show_col_types = FALSE)
if (anyDuplicated(metadata$sample_id)) stop("Duplicated metadata sample IDs")
common <- Reduce(intersect, c(lapply(views, rownames), list(metadata$sample_id)))
if (length(common) < 3L) stop("Insufficient matched specimens for integration")
views <- lapply(views, function(x) x[common, , drop = FALSE])
metadata <- metadata[match(common, metadata$sample_id), , drop = FALSE]
stopifnot(identical(common, metadata$sample_id), all(vapply(views, function(x) identical(rownames(x), common), logical(1))))
```

The complete-case intersection is appropriate for DIABLO, SNF, and MCIA. MOFA2 may instead preserve incomplete views by constructing its long/data-list input with missing view values; record that choice.

## 2. MOFA2

```r
required <- c("MOFA2")
missing <- required[!vapply(required, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing)) stop("Missing declared packages: ", paste(missing, collapse = ", "))

# MOFA2 expects features x samples in a list.
mofa_input <- lapply(views, t)
mofa <- MOFA2::create_mofa(mofa_input)
data_opts <- MOFA2::get_default_data_options(mofa)
data_opts$scale_views <- TRUE
model_opts <- MOFA2::get_default_model_options(mofa)
model_opts$num_factors <- 15L
train_opts <- MOFA2::get_default_training_options(mofa)
train_opts$seed <- 42L
train_opts$convergence_mode <- "slow"

mofa <- MOFA2::prepare_mofa(mofa, data_options = data_opts, model_options = model_opts, training_options = train_opts)
mofa <- MOFA2::run_mofa(mofa, outfile = "mofa_model.hdf5", use_basilisk = TRUE)
r2 <- MOFA2::get_variance_explained(mofa)$r2_per_factor
scores <- MOFA2::get_factors(mofa, as.data.frame = TRUE)
weights <- MOFA2::get_weights(mofa, as.data.frame = TRUE)
readr::write_tsv(scores, "integration_scores.tsv")
readr::write_tsv(weights, "integration_weights.tsv")
saveRDS(r2, "mofa_variance_explained.rds")
```

Repeat with several seeds. Correlate scores with batch, site, depth, missingness, and biological covariates. Define factor retention in the plan and run a sensitivity rule; never select only factors that correlate with the outcome.

## 3. DIABLO

The following is the inner training operation. Wrap it in an outer patient/site-safe resampling loop; do not report its same-data tuning result as final performance.

```r
required <- c("mixOmics")
missing <- required[!vapply(required, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing)) stop("Missing declared packages: ", paste(missing, collapse = ", "))

Y <- factor(metadata$condition)
if (nlevels(Y) < 2L) stop("DIABLO requires at least two outcome classes")

make_design <- function(blocks, off_diagonal) {
  d <- matrix(off_diagonal, length(blocks), length(blocks), dimnames = list(names(blocks), names(blocks)))
  diag(d) <- 0
  d
}

# Treat these as sensitivity variants; choose the primary value in the analysis plan.
designs <- list(low = make_design(views, 0.1), moderate = make_design(views, 0.5))
design <- designs[["moderate"]]
initial <- mixOmics::block.splsda(views, Y, ncomp = min(5L, nlevels(Y) + 2L), design = design)
component_perf <- mixOmics::perf(initial, validation = "Mfold", folds = 5, nrepeat = 10, progressBar = FALSE)

# Derive ncomp from the documented BER component and tune keepX only within this training partition.
ncomp <- component_perf$choice.ncomp$WeightedVote["Overall.BER", "max.dist"]
keep_grid <- lapply(views, function(x) sort(unique(pmin(ncol(x), c(5L, 10L, 25L, 50L)))))
tuned <- mixOmics::tune.block.splsda(
  views, Y, ncomp = ncomp, test.keepX = keep_grid, design = design,
  validation = "Mfold", folds = 5, nrepeat = 5, progressBar = FALSE
)
model <- mixOmics::block.splsda(views, Y, ncomp = ncomp, keepX = tuned$choice.keepX, design = design)
saveRDS(model, "diablo_training_model.rds")
```

In each outer fold: derive unsupervised filters from training data, tune design/ncomp/keepX on training only, predict the held-out fold, and aggregate balanced error with uncertainty. Refit on all development data only after performance estimation, then evaluate an untouched external set if available.

## 4. SNF

```r
required <- c("SNFtool")
missing <- required[!vapply(required, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing)) stop("Missing declared packages: ", paste(missing, collapse = ", "))

K_neighbors <- min(20L, nrow(views[[1]]) - 1L)
if (K_neighbors < 2L) stop("Too few specimens for an SNF neighborhood graph")
sigma <- 0.5
scaled <- lapply(views, SNFtool::standardNormalization)
affinity <- lapply(scaled, function(x) SNFtool::affinityMatrix(SNFtool::dist2(x, x)^(1/2), K_neighbors, sigma))
fused <- SNFtool::SNF(affinity, K = K_neighbors, t = 20L)

candidate_clusters <- 2:min(8L, nrow(fused) - 1L)
estimates <- SNFtool::estimateNumberOfClustersGivenGraph(fused, NUMC = candidate_clusters)
chosen_clusters <- 3L  # replace with a predeclared, stability-supported value
clusters <- SNFtool::spectralClustering(fused, K = chosen_clusters)
readr::write_tsv(data.frame(sample_id = rownames(views[[1]]), cluster = clusters), "snf_clusters.tsv")
saveRDS(list(fused = fused, affinities = affinity, estimates = estimates), "snf_model.rds")
```

Bootstrap specimens and vary neighborhood size, scale, feature filter, and cluster count. Test clinical variables after clusters are frozen. A fused heatmap alone does not establish a disease subtype.

## 5. MCIA

MCIA requires complete, matched samples and operates on numeric blocks. The orientation expected by `omicade4::mcia` is features by samples.

```r
required <- c("omicade4")
missing <- required[!vapply(required, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing)) stop("Missing declared packages: ", paste(missing, collapse = ", "))

mcia_blocks <- lapply(views, function(x) {
  z <- scale(x)
  z[, apply(z, 2, function(v) all(is.finite(v)) && stats::sd(v) > 0), drop = FALSE]
})
mcia_fit <- omicade4::mcia(lapply(mcia_blocks, t), cia.nf = min(5L, length(common) - 1L))
saveRDS(mcia_fit, "mcia_model.rds")
```

Export sample scores, feature loadings, and view contributions using accessors available in the locked omicade4 version. Verify object component names with `str(mcia_fit)` rather than assuming an API from another release. Assess axes under bootstrap/permutation and screen technical covariates before biological interpretation.

## 6. Interpretation limits

- MOFA/MCIA axes are latent associations, not mechanisms.
- DIABLO selected variables are a multivariate signature conditional on preprocessing, design, and tuning; they are not individually causal biomarkers.
- SNF clusters are candidate groupings until stable and replicated externally.
- Cross-omic correlations can reflect abundance, composition, batch, or shared confounding. Report alternative explanations.
