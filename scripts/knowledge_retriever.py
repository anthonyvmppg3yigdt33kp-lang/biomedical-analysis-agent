#!/usr/bin/env python3
"""Retrieve explainable candidates from the private distilled knowledge index.

The retriever is deliberately read-only. It never emits article code or private
source paths, and it never upgrades maturity. SourceFlowBundle is the retrieval
anchor; linked MethodCards, PackageCards, and CapabilityModules contribute
evidence and constraints to the same candidate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "1.0"
DEFAULT_INDEX = Path(__file__).resolve().parent.parent / "assets" / "private-corpus-index"
MATURITY_ORDER = {
    "raw-extracted": 0,
    "normalized": 1,
    "parse-verified": 2,
    "fixture-verified": 3,
    "data-verified": 4,
    "native-reviewed": 5,
}
EXECUTABLE_MATURITIES = {"fixture-verified", "data-verified", "native-reviewed"}

# Receipt-authenticated deep reviews may contribute bounded, derived semantics
# to retrieval. These limits keep the public output inspectable and prevent a
# review record from becoming a surrogate article or raw-code payload.
REVIEW_SEMANTIC_LIMITS = {
    "scalar_chars": 480,
    "list_items": 6,
    "research_values_per_field": 4,
    "method_steps": 8,
    "package_usages": 12,
    "package_names": 24,
    "figures": 4,
}


DOMAIN_ALIASES: dict[str, tuple[str, ...]] = {
    "single-cell": (
        "single cell", "single-cell", "scrna", "scrna-seq", "seurat", "scanpy",
        "单细胞", "单细胞转录组", "细胞注释",
    ),
    "spatial": (
        "spatial", "spatial transcriptomics", "visium", "stereoseq", "stereo-seq",
        "xenium", "cosmx", "merfish", "空间转录组", "空转", "空间域", "空间邻域",
    ),
    "bulk-rna": (
        "bulk", "bulk rna", "bulk rna-seq", "rnaseq", "rna-seq", "deseq2",
        "edger", "limma", "转录组", "批量转录组", "差异表达",
    ),
    "proteomics": (
        "proteomics", "protein", "peptide", "qfeatures", "maxquant", "msstats",
        "dep", "蛋白组", "定量蛋白组", "肽段",
    ),
    "multi-omics": (
        "multiomics", "multi-omics", "mofa", "mofa2", "diablo", "snf", "mixomics",
        "多组学", "跨组学", "组学整合",
    ),
    "visualization": (
        "visualization", "plot", "figure", "ggplot", "matplotlib", "heatmap", "umap",
        "绘图", "可视化", "图形", "复刻", "热图",
    ),
    "literature-methodology": (
        "literature", "methodology", "method selection", "paper", "review", "文献",
        "方法学", "分析思路", "方法选择", "套路", "解读",
    ),
}

DOMAIN_NORMALIZATION: dict[str, str] = {}
for _domain, _aliases in DOMAIN_ALIASES.items():
    DOMAIN_NORMALIZATION[_domain] = _domain
    for _alias in _aliases:
        DOMAIN_NORMALIZATION[unicodedata.normalize("NFKC", _alias).casefold()] = _domain


INSTALLER_PATTERNS = (
    "install.packages", "biocmanager::install", "pak::pkg_install",
    "remotes::install_github", "devtools::install_github", "pip install",
    "install_github", "pkg_install", "install_packages", "conda install",
    "mamba install", "uv pip install", "pak::pak",
)

SCIENTIFIC_CONCEPTS: dict[str, tuple[str, ...]] = {
    "donor-aware-replication": (
        "donor-aware", "donor aware", "donor", "patient-level", "sample-level",
        "biological replicate", "pseudobulk", "pseudo-bulk", "供体", "患者级",
        "样本级", "生物学重复", "伪bulk", "伪 bulk",
    ),
    "differential-expression": (
        "differential expression", "differential analysis", "pseudobulk", "差异表达",
        "差异分析", "差异基因", "伪bulk", "伪 bulk",
    ),
    "batch-integration": (
        "batch integration", "batch correction", "harmony", "scvi", "批次整合",
        "批次校正", "去批次",
    ),
    "spatial-domain": (
        "spatial domain", "spatial neighborhood", "空间域", "空间邻域", "组织区域",
    ),
    "deconvolution": (
        "deconvolution", "cell2location", "rctd", "card", "解卷积", "细胞组成估计",
    ),
}


def _norm(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _flatten_text(value: Any) -> Iterable[str]:
    """Yield index text while explicitly excluding raw code and private paths."""
    if value is None:
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _norm(key) in {
                "text", "source_locator", "private_source_directory", "source_span",
                "absolute_paths", "relative_to_bundle", "source_relative_path",
            }:
                continue
            yield from _flatten_text(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            yield from _flatten_text(item)
        return
    if isinstance(value, (int, float, bool)):
        yield str(value)


def _tokens(value: Any) -> set[str]:
    """Tokenize English words and Chinese bi/tri-grams deterministically."""
    text = _norm(value)
    result = set(re.findall(r"[a-z][a-z0-9_.+/-]*|\d+(?:\.\d+)?", text))
    for token in tuple(result):
        if any(separator in token for separator in ("-", "/", "_")):
            result.update(part for part in re.split(r"[-/_]", token) if part)
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        result.add(run)
        result.update(run[i : i + 2] for i in range(max(0, len(run) - 1)))
        result.update(run[i : i + 3] for i in range(max(0, len(run) - 2)))
    return {token for token in result if token}


@lru_cache(maxsize=None)
def _domain_alias_entries(domain: str) -> tuple[tuple[str, frozenset[str]], ...]:
    return tuple(
        (_norm(alias), frozenset(_tokens(alias)))
        for alias in (domain, *DOMAIN_ALIASES[domain])
    )


def _canonical_domain(value: str | None) -> str | None:
    if not value:
        return None
    normalized = _norm(value)
    if normalized in DOMAIN_NORMALIZATION:
        return DOMAIN_NORMALIZATION[normalized]
    value_tokens = _tokens(normalized)
    best: tuple[int, str] | None = None
    for domain in DOMAIN_ALIASES:
        alias_tokens = set().union(*(tokens for _, tokens in _domain_alias_entries(domain)))
        overlap = len(value_tokens & alias_tokens)
        candidate = (overlap, domain)
        if overlap and (best is None or candidate > best):
            best = candidate
    return best[1] if best else normalized


def _domain_set(text: str, tokens: set[str] | None = None) -> set[str]:
    normalized = _norm(text)
    tokens = tokens if tokens is not None else _tokens(normalized)
    found: set[str] = set()
    for domain in DOMAIN_ALIASES:
        for alias_norm, alias_tokens in _domain_alias_entries(domain):
            if alias_norm in normalized or alias_tokens <= tokens:
                found.add(domain)
                break
    return found


def _safe_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _is_installer_name(value: Any) -> bool:
    normalized = _norm(value)
    return normalized == "install" or any(pattern in normalized for pattern in INSTALLER_PATTERNS)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_beneath(base: Path, raw_path: Any) -> Path:
    candidate = Path(str(raw_path))
    if not candidate.is_absolute():
        candidate = base / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError as exc:
        raise ValueError("review_file_escapes_manual_review_directory") from exc
    return candidate


def _redact_private_locator(value: Any, limit: int = 1200) -> str:
    """Return bounded prose with local/remote locators removed."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"(?i)file:(?://|\\\\)\S+", "[private-path-redacted]", text)
    text = re.sub(r"(?i)https?://\S+", "[url-redacted]", text)
    text = re.sub(
        r"(?i)(?<![\w])(?:[a-z]:[\\/]|\\\\)[^\r\n\t;，。]+",
        "[private-path-redacted]",
        text,
    )
    text = re.sub(
        r"(?i)(?<![\w+])/(?:mnt|home|users?|tmp|var|opt|srv|data|datasets?|projects?|volumes)(?:/[^\s;，。]*)?",
        "[private-path-redacted]",
        text,
    )
    text = " ".join(text.split())
    return text[:limit]


def _bounded_review_strings(
    value: Any,
    *,
    max_items: int | None = None,
    max_chars: int | None = None,
) -> list[str]:
    """Sanitize a scalar/list into a deterministic unique list of prose."""

    item_limit = max_items or REVIEW_SEMANTIC_LIMITS["list_items"]
    char_limit = max_chars or REVIEW_SEMANTIC_LIMITS["scalar_chars"]
    raw_items = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in raw_items:
        if not isinstance(item, (str, int, float, bool)):
            continue
        sanitized = _redact_private_locator(item, char_limit)
        if sanitized and sanitized not in result:
            result.append(sanitized)
        if len(result) >= item_limit:
            break
    return result


def _bounded_review_object_list(
    items: Iterable[Mapping[str, Any]], max_items: int
) -> list[dict[str, Any]]:
    """Deduplicate already-sanitized objects without exposing source identity."""

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        digest = _canonical_sha256(item)
        if digest in seen:
            continue
        seen.add(digest)
        result.append(dict(item))
        if len(result) >= max_items:
            break
    return result


def _package_name_candidates(value: Any) -> list[str]:
    """Extract bounded package identities, retaining composite source labels."""

    names: list[str] = []
    for label in _bounded_review_strings(value, max_items=1, max_chars=120):
        candidates = [label]
        candidates.extend(re.split(r"\s*(?:/|、|,|;|\|)\s*", label))
        for candidate in candidates:
            candidate = candidate.strip()
            if candidate and not _is_installer_name(candidate) and candidate not in names:
                names.append(candidate)
    return names[: REVIEW_SEMANTIC_LIMITS["package_names"]]


def _derive_deep_review_semantics(record: Mapping[str, Any]) -> dict[str, Any]:
    """Derive a public-safe semantic summary from a complete deep review.

    Selection is whitelist-only. Raw article text, code-review payloads,
    provenance, locators, hashes, and arbitrary extra fields are never copied.
    """

    research = record.get("research_context")
    research = research if isinstance(research, Mapping) else {}
    research_fields = {
        "research_questions": "research_question",
        "input_modalities": "input_modality",
        "cohort_or_sample_structures": "cohort_or_sample_structure",
        "descriptive_units": "descriptive_unit",
        "inferential_units": "inferential_unit",
    }
    research_summary = {
        public_key: _bounded_review_strings(
            research.get(source_key),
            max_items=REVIEW_SEMANTIC_LIMITS["research_values_per_field"],
        )
        for public_key, source_key in research_fields.items()
    }
    research_summary = {key: value for key, value in research_summary.items() if value}

    method_steps: list[dict[str, Any]] = []
    for step in _safe_list(record.get("method_sequence")):
        if not isinstance(step, Mapping):
            continue
        method = _bounded_review_strings(step.get("method"), max_items=1)
        if not method:
            continue
        public_step: dict[str, Any] = {"method": method[0]}
        order = step.get("order")
        if isinstance(order, (int, float)) and not isinstance(order, bool):
            public_step["order"] = order
        for public_key, source_key in (
            ("rationales", "rationale"),
            ("inputs", "inputs"),
            ("outputs", "outputs"),
        ):
            values = _bounded_review_strings(step.get(source_key))
            if values:
                public_step[public_key] = values
        method_steps.append(public_step)
        if len(method_steps) >= REVIEW_SEMANTIC_LIMITS["method_steps"]:
            break

    package_usages: list[dict[str, Any]] = []
    package_names: list[str] = []
    for usage in _safe_list(record.get("package_usage")):
        if not isinstance(usage, Mapping):
            continue
        raw_names = _package_name_candidates(usage.get("package"))
        if not raw_names:
            continue
        for name in raw_names:
            if name not in package_names:
                package_names.append(name)
        public_usage: dict[str, Any] = {"package": raw_names[0]}
        roles = _bounded_review_strings(usage.get("role"), max_items=1)
        functions = [
            item
            for item in _bounded_review_strings(usage.get("functions"), max_chars=120)
            if not _is_installer_name(item)
        ]
        if roles:
            public_usage["role"] = roles[0]
        if functions:
            public_usage["functions"] = functions
        package_usages.append(public_usage)
        if len(package_usages) >= REVIEW_SEMANTIC_LIMITS["package_usages"]:
            break

    scientific = record.get("scientific_review")
    scientific = scientific if isinstance(scientific, Mapping) else {}
    scientific_summary: dict[str, list[str]] = {}
    for public_key, source_key in (
        ("claim_ceilings", "claim_ceiling"),
        ("assumptions", "assumptions"),
        ("scientific_risks", "scientific_risks"),
        ("alternatives", "alternatives"),
        ("validation_required", "validation_required"),
        ("negative_controls", "negative_controls"),
    ):
        values = _bounded_review_strings(scientific.get(source_key))
        if values:
            scientific_summary[public_key] = values

    figure = record.get("figure_context_review")
    if not isinstance(figure, Mapping):
        figure = record.get("figure_context")
    figure = figure if isinstance(figure, Mapping) else {}
    figure_summary: dict[str, Any] = {}
    statuses = _bounded_review_strings(figure.get("status"), max_items=1, max_chars=160)
    rationale = _bounded_review_strings(figure.get("selection_rationale"), max_items=1)
    if statuses:
        figure_summary["statuses"] = statuses
    if rationale:
        figure_summary["selection_rationales"] = rationale
    public_figures: list[dict[str, Any]] = []
    for item in _safe_list(figure.get("figures")):
        if not isinstance(item, Mapping):
            continue
        public_figure: dict[str, Any] = {}
        for public_key, source_key in (
            ("roles", "figure_role"),
            ("reproduction_classes", "reproduction_class"),
            ("visible", "visible"),
            ("interpretable", "interpretable"),
            ("cannot_assert", "cannot_assert"),
            ("visual_issues", "visual_issues"),
        ):
            values = _bounded_review_strings(item.get(source_key))
            if values:
                public_figure[public_key] = values
        if public_figure:
            public_figures.append(public_figure)
        if len(public_figures) >= REVIEW_SEMANTIC_LIMITS["figures"]:
            break
    if public_figures:
        figure_summary["figures"] = public_figures

    result: dict[str, Any] = {
        "derivation": {
            "kind": "receipt_authenticated_deep_review_semantic_summary",
            "source_fields": [
                "research_context", "combination_logic", "method_sequence",
                "package_usage", "scientific_review", "figure_context_review",
            ],
            "sanitization": "whitelist_only_locator_redacted_bounded",
            "limits": dict(REVIEW_SEMANTIC_LIMITS),
        },
    }
    if research_summary:
        result["research_context"] = research_summary
    combination = _bounded_review_strings(record.get("combination_logic"))
    if combination:
        result["combination_logic"] = combination
    if method_steps:
        result["method_sequence"] = method_steps
    if package_usages:
        result["package_usage"] = package_usages
    if scientific_summary:
        result["scientific_review"] = scientific_summary
    if figure_summary:
        result["figure_context"] = figure_summary
    if package_names:
        result["package_names"] = package_names[: REVIEW_SEMANTIC_LIMITS["package_names"]]
    return result


def _aggregate_deep_review_semantics(fragments: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Conservatively merge derived semantics for all reviews of one bundle."""

    semantics = [
        item.get("derived_semantics")
        for item in fragments
        if isinstance(item.get("derived_semantics"), Mapping)
    ]
    result: dict[str, Any] = {
        "derivation": {
            "kind": "receipt_authenticated_deep_review_semantic_summary",
            "source_fields": [
                "research_context", "combination_logic", "method_sequence",
                "package_usage", "scientific_review", "figure_context_review",
            ],
            "sanitization": "whitelist_only_locator_redacted_bounded",
            "limits": dict(REVIEW_SEMANTIC_LIMITS),
        },
    }

    research: dict[str, list[str]] = {}
    for key in (
        "research_questions", "input_modalities", "cohort_or_sample_structures",
        "descriptive_units", "inferential_units",
    ):
        values: list[str] = []
        for item in semantics:
            section = item.get("research_context")
            if isinstance(section, Mapping):
                values.extend(_bounded_review_strings(
                    section.get(key),
                    max_items=REVIEW_SEMANTIC_LIMITS["research_values_per_field"],
                ))
        values = list(dict.fromkeys(values))[: REVIEW_SEMANTIC_LIMITS["research_values_per_field"]]
        if values:
            research[key] = values
    if research:
        result["research_context"] = research

    combinations: list[str] = []
    package_names: list[str] = []
    method_steps: list[Mapping[str, Any]] = []
    package_usages: list[Mapping[str, Any]] = []
    figure_items: list[Mapping[str, Any]] = []
    scientific: dict[str, list[str]] = {}
    figure_statuses: list[str] = []
    figure_rationales: list[str] = []
    for item in semantics:
        combinations.extend(_bounded_review_strings(item.get("combination_logic")))
        package_names.extend(_bounded_review_strings(
            item.get("package_names"),
            max_items=REVIEW_SEMANTIC_LIMITS["package_names"],
            max_chars=120,
        ))
        method_steps.extend(
            step for step in _safe_list(item.get("method_sequence")) if isinstance(step, Mapping)
        )
        package_usages.extend(
            usage for usage in _safe_list(item.get("package_usage")) if isinstance(usage, Mapping)
        )
        scientific_section = item.get("scientific_review")
        if isinstance(scientific_section, Mapping):
            for key in (
                "claim_ceilings", "assumptions", "scientific_risks", "alternatives",
                "validation_required", "negative_controls",
            ):
                scientific.setdefault(key, []).extend(_bounded_review_strings(scientific_section.get(key)))
        figure_section = item.get("figure_context")
        if isinstance(figure_section, Mapping):
            figure_statuses.extend(_bounded_review_strings(figure_section.get("statuses"), max_chars=160))
            figure_rationales.extend(_bounded_review_strings(figure_section.get("selection_rationales")))
            figure_items.extend(
                figure_item
                for figure_item in _safe_list(figure_section.get("figures"))
                if isinstance(figure_item, Mapping)
            )

    combinations = list(dict.fromkeys(combinations))[: REVIEW_SEMANTIC_LIMITS["list_items"]]
    if combinations:
        result["combination_logic"] = combinations
    method_steps_out = _bounded_review_object_list(method_steps, REVIEW_SEMANTIC_LIMITS["method_steps"])
    if method_steps_out:
        result["method_sequence"] = method_steps_out
    package_usages_out = _bounded_review_object_list(package_usages, REVIEW_SEMANTIC_LIMITS["package_usages"])
    if package_usages_out:
        result["package_usage"] = package_usages_out
    package_names = list(dict.fromkeys(package_names))[: REVIEW_SEMANTIC_LIMITS["package_names"]]
    if package_names:
        result["package_names"] = package_names
    scientific_out = {
        key: list(dict.fromkeys(values))[: REVIEW_SEMANTIC_LIMITS["list_items"]]
        for key, values in scientific.items()
        if values
    }
    if scientific_out:
        result["scientific_review"] = scientific_out
    figure_out: dict[str, Any] = {}
    figure_statuses = list(dict.fromkeys(figure_statuses))[: REVIEW_SEMANTIC_LIMITS["list_items"]]
    figure_rationales = list(dict.fromkeys(figure_rationales))[: REVIEW_SEMANTIC_LIMITS["list_items"]]
    figures_out = _bounded_review_object_list(figure_items, REVIEW_SEMANTIC_LIMITS["figures"])
    if figure_statuses:
        figure_out["statuses"] = figure_statuses
    if figure_rationales:
        figure_out["selection_rationales"] = figure_rationales
    if figures_out:
        figure_out["figures"] = figures_out
    if figure_out:
        result["figure_context"] = figure_out
    return result


def _load_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not path.is_file():
        return records, [f"missing_component:{path.name}"]
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, UnicodeError):
                warnings.append(f"invalid_json:{path.name}:{line_number}")
                continue
            if isinstance(item, dict):
                records.append(item)
            else:
                warnings.append(f"invalid_record_type:{path.name}:{line_number}")
    return records, warnings


def _load_capabilities(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.is_file():
        return [], [f"missing_component:{path.name}"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return [], [f"invalid_json:{path.name}"]
    if isinstance(payload, dict):
        payload = payload.get("modules", [])
    if not isinstance(payload, list):
        return [], [f"invalid_component_type:{path.name}"]
    return [item for item in payload if isinstance(item, dict)], []


@dataclass(frozen=True)
class RetrievalRequest:
    query: str
    domain: str | None = None
    package: str | None = None
    figure_intent: str | None = None
    mode: str = "plan"

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "RetrievalRequest":
        nested = payload.get("intent") if isinstance(payload.get("intent"), dict) else {}
        query_parts: list[str] = []
        for key in ("query", "question", "research_question", "analysis_type"):
            value = payload.get(key, nested.get(key))
            if value:
                if isinstance(value, list):
                    query_parts.extend(str(item) for item in value)
                else:
                    query_parts.append(str(value))
        domain_value = payload.get("domain", payload.get("modality", nested.get("domain", nested.get("modality"))))
        if isinstance(domain_value, list):
            domain_value = next((item for item in domain_value if item), None)
        package_value = payload.get("package", payload.get("preferred_package", nested.get("package")))
        if isinstance(package_value, list):
            package_value = next((item for item in package_value if item), None)
        figure = payload.get(
            "figure_intent",
            payload.get("figure", payload.get("desired_figure", nested.get("figure_intent"))),
        )
        if not figure and isinstance(payload.get("requested_outputs"), list):
            figure = " ".join(str(item) for item in payload["requested_outputs"] if item)
        request = cls(
            query=" ".join(dict.fromkeys(part.strip() for part in query_parts if part.strip())),
            domain=_canonical_domain(str(domain_value)) if domain_value else None,
            package=str(package_value).strip() if package_value else None,
            figure_intent=str(figure).strip() if figure else None,
            mode=str(payload.get("mode", "plan")).strip() or "plan",
        )
        if not any((request.query, request.domain, request.package, request.figure_intent)):
            raise ValueError("request requires query, domain, package, or figure_intent")
        return request

    def public_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "domain": self.domain,
            "package": self.package,
            "figure_intent": self.figure_intent,
            "mode": self.mode,
        }


@dataclass
class CandidateContext:
    bundle: dict[str, Any]
    methods: list[dict[str, Any]] = field(default_factory=list)
    packages: list[dict[str, Any]] = field(default_factory=list)
    figures: list[dict[str, Any]] = field(default_factory=list)
    variants: list[dict[str, Any]] = field(default_factory=list)
    manual_review: dict[str, Any] | None = None

    def searchable_text(self) -> str:
        bundle = self.bundle
        metadata = bundle.get("metadata") if isinstance(bundle.get("metadata"), dict) else {}
        preprocessing = bundle.get("preprocessing_match")
        if isinstance(preprocessing, dict):
            preprocessing = preprocessing.get("record", {})
        selected_bundle = {
            "title": bundle.get("title"),
            "source_root_name": bundle.get("source_root_name"),
            "source_type": bundle.get("source_type"),
            "metadata": {"title": metadata.get("title"), "author": metadata.get("author")},
            "preprocessing": preprocessing,
            "packages": (bundle.get("package_index") or {}).get("packages", []),
            "image_plot_hints": [image.get("plot_hints", []) for image in _safe_list(bundle.get("images"))],
        }
        parts = list(_flatten_text(selected_bundle))
        for method in self.methods:
            parts.extend(_flatten_text({
                key: method.get(key) for key in (
                    "title", "category", "data_types", "method_sequence",
                    "research_question_hints", "combination_logic",
                )
            }))
        for package in self.packages:
            parts.extend(_flatten_text({
                # PackageCard capability lists aggregate every linked article and would
                # contaminate bundle-level relevance. Keep identity/functions only.
                "package": package.get("package"),
                "language": package.get("language"),
                "functions": [
                    item for item in _safe_list(package.get("functions"))
                    if not _is_installer_name(item)
                ],
            }))
        for figure in self.figures:
            parts.extend(_flatten_text({
                key: figure.get(key) for key in (
                    "question", "semantics", "directly_visible", "plot_hints", "visible",
                    "supports", "reproduction_class",
                )
            }))
        for link in self.variants:
            module = link["module"]
            variant = link["variant"]
            parts.extend(_flatten_text({
                "capability": module.get("capability"),
                "semantic_key": module.get("semantic_key"),
                "source_plot_label": variant.get("source_plot_label"),
            }))
        if self.manual_review:
            # Only receipt-authenticated, whitelist-derived fields reach this
            # object. They add retrieval semantics, not independent evidence.
            derived = self.manual_review.get("derived_semantics")
            searchable_semantics = {
                key: value for key, value in derived.items() if key != "derivation"
            } if isinstance(derived, Mapping) else {}
            parts.extend(_flatten_text({
                "package_names": self.manual_review.get("package_names"),
                "derived_semantics": searchable_semantics,
            }))
        return "\n".join(dict.fromkeys(str(part) for part in parts if part))

    def domain_text(self) -> str:
        """Use domain-defining metadata, excluding broad package capability prose."""
        bundle = self.bundle
        preprocessing = bundle.get("preprocessing_match")
        record = preprocessing.get("record", {}) if isinstance(preprocessing, dict) else {}
        selected = {
            "title": bundle.get("title"),
            "source_root_id": bundle.get("source_root_id"),
            "source_root_name": bundle.get("source_root_name"),
            "source_type": bundle.get("source_type"),
            "preprocessing": {
                key: record.get(key) for key in ("一级分类", "数据类型", "能力标签", "_tags")
            } if isinstance(record, dict) else {},
            "methods": [
                {"category": item.get("category"), "data_types": item.get("data_types")}
                for item in self.methods
            ],
            "reviewed_semantics": {
                key: value
                for key, value in (self.manual_review.get("derived_semantics") or {}).items()
                if key != "derivation"
            } if self.manual_review else None,
        }
        return "\n".join(dict.fromkeys(_flatten_text(selected)))


def _exact_duplicate_key(context: CandidateContext) -> tuple[str, str]:
    """Return a source-safe key for exact implementation deduplication.

    A multi-file ordered-code sequence is strong enough to fold copied source
    bundles when the normalized title also agrees.  Single-file candidates use
    the stronger flow fingerprint when it exists; otherwise they remain unique
    because a shared installer or one-line helper is not sufficient evidence of
    an equivalent analysis flow.
    """

    bundle = context.bundle
    ordered: list[tuple[str, str]] = []
    for item in _safe_list(bundle.get("ordered_code_files")):
        if not isinstance(item, dict):
            continue
        digest = str(item.get("sha256") or "").lower()
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            ordered = []
            break
        ordered.append((_norm(item.get("normalized_language")), digest))
    if len(ordered) >= 2:
        key = _canonical_sha256({"title": _norm(bundle.get("title")), "ordered_code": ordered})
        return "ordered_code_sequence_sha256", key

    integrity = bundle.get("flow_integrity")
    fingerprint = (
        str(integrity.get("flow_fingerprint_sha256") or "").lower()
        if isinstance(integrity, dict)
        else ""
    )
    if re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        return "flow_fingerprint_sha256", fingerprint

    return "candidate_id", str(bundle.get("bundle_id") or "")


def _concept_matches(request_text: str, document_text: str) -> tuple[list[str], list[str]]:
    request_norm = _norm(request_text)
    document_norm = _norm(document_text)
    requested: list[str] = []
    matched: list[str] = []
    for concept, aliases in SCIENTIFIC_CONCEPTS.items():
        request_has = any(_norm(alias) in request_norm for alias in aliases)
        if not request_has:
            continue
        requested.append(concept)
        if any(_norm(alias) in document_norm for alias in aliases):
            matched.append(concept)
    return requested, matched


class KnowledgeIndex:
    """Read-only in-memory view of the four corpus registries."""

    def __init__(self, index_dir: Path):
        self.index_dir = index_dir
        self.warnings: list[str] = []
        self.layer = "raw-fallback"
        self.component_dir = index_dir
        self.review_overlay: dict[str, dict[str, Any]] = {}
        self.review_overlay_status = "not_checked"
        self.methods: list[dict[str, Any]] = []
        self.packages: list[dict[str, Any]] = []
        self.bundles: list[dict[str, Any]] = []
        self.figures: list[dict[str, Any]] = []
        self.capabilities: list[dict[str, Any]] = []

    def load(self) -> None:
        if not self.index_dir.is_dir():
            self.warnings.append("private_index_unavailable")
            return
        normalized = self.index_dir if (self.index_dir / "registry-manifest.json").is_file() else self.index_dir / "normalized-registry"
        if normalized.is_dir():
            try:
                manifest = json.loads((normalized / "registry-manifest.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                manifest = {}
            if manifest.get("schema_validated") is True:
                self.layer = "normalized-registry"
                self.component_dir = normalized
            else:
                self.warnings.append("normalized_registry_invalid_using_raw_fallback")
        if self.layer == "normalized-registry":
            self._load_normalized()
        else:
            self._load_raw()
        self._load_manual_review_overlay()

    def _manual_review_dir(self) -> Path:
        if self.index_dir.name == "normalized-registry":
            return self.index_dir.parent / "manual-review"
        return self.index_dir / "manual-review"

    def _load_manual_review_overlay(self) -> None:
        review_dir = self._manual_review_dir()
        validation_path = review_dir / "gold-review-validation.json"
        deep_receipts = (
            sorted(
                path for path in review_dir.glob("high-value-review-batch-*-validation.json")
                if re.fullmatch(r"high-value-review-batch-\d+-validation\.json", path.name)
            )
            if review_dir.is_dir() else []
        )
        if not review_dir.is_dir() or (not validation_path.is_file() and not deep_receipts):
            self.review_overlay_status = "missing_ignored"
            self.warnings.append("manual_review_overlay_missing")
            return
        try:
            overlay: dict[str, dict[str, Any]] = {}
            if validation_path.is_file():
                overlay.update(self._load_gold_review_overlay(review_dir, validation_path))
            deep_overlay = self._load_deep_review_overlay(review_dir, deep_receipts)
            for bundle_id, review in deep_overlay.items():
                if bundle_id in overlay:
                    review["additional_review_kinds"] = ["gold_bundle_review"]
                overlay[bundle_id] = review
            if not overlay:
                raise ValueError("validated_overlay_contains_no_bundle_reviews")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            self.review_overlay = {}
            self.review_overlay_status = "invalid_ignored"
            self.warnings.append(f"manual_review_overlay_invalid:{exc}")
            return
        self.review_overlay = overlay
        self.review_overlay_status = "available"

    @staticmethod
    def _load_gold_review_overlay(
        review_dir: Path, validation_path: Path
    ) -> dict[str, dict[str, Any]]:
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
        if validation.get("ok") is not True or _safe_list(validation.get("errors")):
            raise ValueError("validation_not_ok")
        batch_hashes = validation.get("batch_sha256")
        if not isinstance(batch_hashes, dict) or not batch_hashes:
            raise ValueError("batch_hashes_missing")
        records: list[dict[str, Any]] = []
        for filename in sorted(batch_hashes):
            if Path(filename).name != filename:
                raise ValueError("unsafe_batch_filename")
            batch_path = review_dir / filename
            if not batch_path.is_file() or _sha256_file(batch_path) != str(batch_hashes[filename]).lower():
                raise ValueError(f"batch_hash_mismatch:{filename}")
            batch_records, warnings = _load_jsonl(batch_path)
            if warnings:
                raise ValueError(f"invalid_batch:{filename}")
            records.extend(batch_records)
        seen: set[str] = set()
        overlay: dict[str, dict[str, Any]] = {}
        for record in records:
            bundle_id = str(record.get("bundle_id", ""))
            maturity = str(record.get("maturity", ""))
            if not bundle_id or bundle_id in seen:
                raise ValueError("missing_or_duplicate_bundle_id")
            if maturity not in {"normalized", "parse-verified"}:
                raise ValueError("unsupported_review_maturity")
            if not record.get("decision") or not record.get("claim_ceiling"):
                raise ValueError("review_boundary_fields_missing")
            consistency = record.get("code_figure_consistency")
            if isinstance(consistency, dict):
                figure_status = str(
                    consistency.get("status")
                    or consistency.get("figure_maturity")
                    or consistency.get("reproduction_class")
                    or "unknown"
                )
            else:
                figure_status = "unstructured_review"
            status_norm = _norm(figure_status)
            unresolved = any(
                marker in status_norm
                for marker in ("unresolved", "not_reconstructable", "not_directly_generated")
            )
            overlay[bundle_id] = {
                "reviewed": True,
                "review_kind": "gold_bundle_review",
                "decision": _redact_private_locator(record.get("decision"), 240),
                "maturity": maturity,
                "claim_ceiling": _redact_private_locator(record.get("claim_ceiling")),
                "code_figure_status": _redact_private_locator(figure_status, 240),
                "code_figure_unresolved": unresolved,
                "fixture_or_data_verified": False,
                "automatic_execution_allowed": False,
                "source_review_count": 1,
            }
            seen.add(bundle_id)
        expected_reviews = validation.get("reviews")
        expected_unique = validation.get("unique_bundle_ids")
        if expected_reviews is not None and int(expected_reviews) != len(records):
            raise ValueError("review_count_mismatch")
        if expected_unique is not None and int(expected_unique) != len(seen):
            raise ValueError("unique_bundle_count_mismatch")
        return overlay

    def _load_deep_review_overlay(
        self, review_dir: Path, receipt_paths: Sequence[Path]
    ) -> dict[str, dict[str, Any]]:
        """Load source-hash-authenticated record reviews and aggregate by bundle."""

        if not receipt_paths:
            return {}
        index_root = review_dir.parent
        registry_rows, registry_warnings = _load_jsonl(index_root / "preprocessing-records.jsonl")
        crosswalk_rows, crosswalk_warnings = _load_jsonl(index_root / "preprocessing-crosswalk.jsonl")
        if registry_warnings or crosswalk_warnings:
            raise ValueError("deep_review_source_registry_invalid")
        registry_by_id: dict[str, dict[str, Any]] = {}
        crosswalk_by_id: dict[str, dict[str, Any]] = {}
        for row in registry_rows:
            record_id = str(row.get("preprocess_record_id") or "")
            if not record_id or record_id in registry_by_id:
                raise ValueError("deep_review_source_record_duplicate")
            registry_by_id[record_id] = row
        for row in crosswalk_rows:
            record_id = str(row.get("preprocess_record_id") or "")
            if not record_id or record_id in crosswalk_by_id:
                raise ValueError("deep_review_crosswalk_record_duplicate")
            crosswalk_by_id[record_id] = row

        known_bundle_ids = {
            str(bundle.get("bundle_id"))
            for bundle in self.bundles
            if bundle.get("bundle_id")
        }
        for method in self.methods:
            known_bundle_ids.update(str(item) for item in _safe_list(method.get("source_bundle_ids")) if item)
        for package in self.packages:
            known_bundle_ids.update(str(item) for item in _safe_list(package.get("source_bundle_ids")) if item)
        for figure in self.figures:
            known_bundle_ids.update(str(item) for item in _safe_list(figure.get("source_bundle_ids")) if item)
            if figure.get("source_bundle_id"):
                known_bundle_ids.add(str(figure["source_bundle_id"]))
            known_bundle_ids.update(self._article_ids(figure))
        for module in self.capabilities:
            known_bundle_ids.update(
                str(variant.get("source_bundle_id"))
                for variant in _safe_list(module.get("variants"))
                if isinstance(variant, dict) and variant.get("source_bundle_id")
            )
        fragments_by_bundle: dict[str, list[dict[str, Any]]] = {}
        seen_record_ids: set[str] = set()
        for receipt_path in receipt_paths:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if receipt.get("ok") is not True or receipt.get("errors") != []:
                raise ValueError(f"deep_review_receipt_not_ok:{receipt_path.name}")
            review_files = receipt.get("review_files")
            if not isinstance(review_files, dict) or not review_files:
                raise ValueError(f"deep_review_files_missing:{receipt_path.name}")
            receipt_complete = 0
            for raw_path, expected_sha256 in sorted(review_files.items()):
                review_path = _resolve_beneath(review_dir, raw_path)
                if not review_path.is_file():
                    raise ValueError(f"deep_review_file_missing:{review_path.name}")
                if _sha256_file(review_path) != str(expected_sha256).lower():
                    raise ValueError(f"deep_review_file_hash_mismatch:{review_path.name}")
                records, warnings = _load_jsonl(review_path)
                if warnings:
                    raise ValueError(f"deep_review_file_invalid:{review_path.name}")
                for record in records:
                    if record.get("review_state") != "COMPLETE":
                        continue
                    receipt_complete += 1
                    record_id = str(record.get("preprocess_record_id") or "")
                    if not record_id or record_id in seen_record_ids:
                        raise ValueError("deep_review_missing_or_duplicate_record_id")
                    seen_record_ids.add(record_id)
                    registry_row = registry_by_id.get(record_id)
                    crosswalk_row = crosswalk_by_id.get(record_id)
                    if registry_row is None or crosswalk_row is None:
                        raise ValueError(f"deep_review_source_record_missing:{record_id}")
                    evidence = record.get("source_evidence")
                    if not isinstance(evidence, dict):
                        raise ValueError(f"deep_review_source_evidence_missing:{record_id}")
                    if evidence.get("record_sha256") != registry_row.get("record_sha256"):
                        raise ValueError(f"deep_review_record_hash_stale:{record_id}")
                    expected_relation_sha = _canonical_sha256(crosswalk_row.get("relations", []))
                    if evidence.get("source_relation_sha256") != expected_relation_sha:
                        raise ValueError(f"deep_review_relation_hash_stale:{record_id}")
                    decision = record.get("decision")
                    if not isinstance(decision, dict) or decision.get("automatic_execution_allowed") is not False:
                        raise ValueError(f"deep_review_execution_boundary_invalid:{record_id}")
                    maturity = str(record.get("maturity") or "")
                    if maturity not in {"normalized", "parse-verified"}:
                        raise ValueError(f"deep_review_maturity_invalid:{record_id}")
                    scientific = record.get("scientific_review")
                    if not isinstance(scientific, dict) or not scientific.get("claim_ceiling"):
                        raise ValueError(f"deep_review_claim_ceiling_missing:{record_id}")
                    reported_bundles = {
                        str(item.get("bundle_id"))
                        for item in _safe_list(evidence.get("bundles"))
                        if isinstance(item, dict) and item.get("bundle_id")
                    }
                    current_bundles = {
                        str(item.get("bundle_id"))
                        for item in _safe_list(crosswalk_row.get("relations"))
                        if isinstance(item, dict) and item.get("bundle_id")
                    }
                    if reported_bundles != current_bundles:
                        raise ValueError(f"deep_review_bundle_relation_mismatch:{record_id}")
                    if not reported_bundles:
                        if not _safe_list(evidence.get("external_targets")):
                            raise ValueError(f"deep_review_has_no_retrievable_target:{record_id}")
                        # This record belongs to another local Skill. Its receipt is
                        # still authenticated and counted, but it cannot annotate a
                        # biomedical-analysis-agent bundle.
                        continue
                    if not reported_bundles <= known_bundle_ids:
                        raise ValueError(f"deep_review_bundle_not_in_registry:{record_id}")

                    figure_context = record.get("figure_context_review")
                    figure_context = figure_context if isinstance(figure_context, dict) else {}
                    figure_status = str(figure_context.get("status") or "not_reviewed")
                    binding_unresolved = any(
                        str(figure.get("code_binding", {}).get("status")) != "verified_run_artifact"
                        for figure in _safe_list(figure_context.get("figures"))
                        if isinstance(figure, dict)
                    )
                    unreviewed_count = int(figure_context.get("unreviewed_figure_count") or 0)
                    status_norm = _norm(figure_status)
                    unresolved = bool(
                        binding_unresolved
                        or unreviewed_count
                        or any(
                            marker in status_norm
                            for marker in ("unresolved", "not_reconstructable", "not_reviewed")
                        )
                    )
                    fragment = {
                        "decision": _redact_private_locator(decision.get("classification"), 240),
                        "decision_reason": _redact_private_locator(decision.get("reason"), 480),
                        "maturity": maturity,
                        "claim_ceiling": _redact_private_locator(scientific.get("claim_ceiling")),
                        "code_figure_status": _redact_private_locator(figure_status, 240),
                        "code_figure_unresolved": unresolved,
                        "derived_semantics": _derive_deep_review_semantics(record),
                    }
                    for bundle_id in sorted(reported_bundles):
                        fragments_by_bundle.setdefault(bundle_id, []).append(fragment)
            if receipt.get("complete_records") != receipt_complete:
                raise ValueError(f"deep_review_complete_count_mismatch:{receipt_path.name}")
            expected_unique = receipt.get("unique_records")
            if expected_unique is not None and int(expected_unique) != receipt_complete:
                raise ValueError(f"deep_review_unique_count_mismatch:{receipt_path.name}")

        overlay: dict[str, dict[str, Any]] = {}
        for bundle_id, fragments in fragments_by_bundle.items():
            maturities = [str(item["maturity"]) for item in fragments]
            maturity = min(maturities, key=lambda item: MATURITY_ORDER[item])
            decisions = sorted({str(item["decision"]) for item in fragments if item["decision"]})
            reasons = sorted({str(item["decision_reason"]) for item in fragments if item["decision_reason"]})
            ceilings = sorted({str(item["claim_ceiling"]) for item in fragments if item["claim_ceiling"]})
            statuses = sorted({str(item["code_figure_status"]) for item in fragments})
            derived_semantics = _aggregate_deep_review_semantics(fragments)
            overlay[bundle_id] = {
                "reviewed": True,
                "review_kind": "deep_record_review",
                "decision": " | ".join(decisions)[:480],
                "decision_reason": " | ".join(reasons)[:960],
                "maturity": maturity,
                "claim_ceiling": " | ".join(ceilings)[:1200],
                "code_figure_status": " | ".join(statuses)[:480],
                "code_figure_unresolved": any(item["code_figure_unresolved"] for item in fragments),
                "fixture_or_data_verified": False,
                "automatic_execution_allowed": False,
                "source_review_count": len(fragments),
                "package_names": _safe_list(derived_semantics.get("package_names")),
                "derived_semantics": derived_semantics,
            }
        return overlay

    def _load_raw(self) -> None:
        self.methods, warnings = _load_jsonl(self.component_dir / "method-cards.jsonl")
        self.warnings.extend(warnings)
        self.packages, warnings = _load_jsonl(self.component_dir / "package-cards.jsonl")
        self.warnings.extend(warnings)
        self.bundles, warnings = _load_jsonl(self.component_dir / "source-flow-bundles.jsonl")
        self.warnings.extend(warnings)
        self.figures, warnings = _load_jsonl(self.component_dir / "figure-cards.jsonl")
        self.warnings.extend(warnings)
        self.capabilities, warnings = _load_capabilities(self.component_dir / "capability-modules.json")
        self.warnings.extend(warnings)

    @staticmethod
    def _article_ids(record: Mapping[str, Any]) -> list[str]:
        return sorted({
            str(item.get("article_id"))
            for item in _safe_list(record.get("provenance"))
            if isinstance(item, dict) and item.get("article_id")
        })

    @staticmethod
    def _normalized_gap_code(value: Any) -> str:
        normalized = _norm(value)
        if "install" in normalized:
            return "installer_calls_present"
        if "hard-coded" in normalized or "hard coded" in normalized or "hard_coded" in normalized:
            return "hard_coded_paths"
        if "image" in normalized and "mismatch" in normalized:
            return "image_count_mismatch"
        if "undefined" in normalized:
            return "undefined_context"
        return "normalized_registry_gap"

    def _load_normalized(self) -> None:
        raw_methods, warnings = _load_jsonl(self.component_dir / "method-cards.jsonl")
        self.warnings.extend(warnings)
        self.methods = [{
            "method_card_id": item.get("method_id"),
            "title": item.get("question"),
            "category": "normalized_method_card",
            "data_types": _safe_list(item.get("applicability")),
            "method_sequence": _safe_list(item.get("combination_logic")),
            "research_question_hints": [item.get("question")] if item.get("question") else [],
            "combination_logic": item.get("combination_logic"),
            "required_validation": _safe_list(item.get("validation")),
            "review_status": "not_manually_reviewed" if item.get("maturity") == "raw-extracted" else "maturity_governed",
            "analysis_unit": item.get("statistical_unit", "unknown"),
            "maturity": item.get("maturity", "unknown"),
            "source_bundle_ids": self._article_ids(item),
        } for item in raw_methods]

        raw_packages, warnings = _load_jsonl(self.component_dir / "package-cards.jsonl")
        self.warnings.extend(warnings)
        self.packages = [{
            "package_card_id": item.get("package_id"),
            "package": item.get("name"),
            "capability_hints": _safe_list(item.get("complete_workflow")),
            "functions": _safe_list(item.get("functions")),
            "language": item.get("language", "unknown"),
            "maturity": item.get("maturity", "unknown"),
            "source_bundle_ids": self._article_ids(item),
        } for item in raw_packages]

        raw_bundles, warnings = _load_jsonl(self.component_dir / "source-flow-bundles.jsonl")
        self.warnings.extend(warnings)
        self.bundles = []
        for item in raw_bundles:
            gaps = _safe_list(item.get("gaps"))
            ordered_code = _safe_list(item.get("ordered_code"))
            images = _safe_list(item.get("images"))
            self.bundles.append({
                "bundle_id": item.get("bundle_id"),
                "title": item.get("title"),
                "source_root_id": "normalized-private-registry",
                "source_root_name": "normalized private registry",
                "source_type": "normalized_source_flow_bundle",
                "maturity": item.get("maturity", "unknown"),
                "flow_integrity": {"reconstructable_from_sources": bool(ordered_code and item.get("provenance"))},
                "issues": [
                    {"code": self._normalized_gap_code(gap), "severity": "requires_review"}
                    for gap in gaps
                ],
                "metadata": {"title": item.get("title")},
                "preprocessing_match": None,
                "package_index": {"packages": _safe_list(item.get("packages")), "qualified_functions": []},
                "ordered_code_files": [
                    {
                        "ordinal": code.get("order"),
                        "normalized_language": code.get("language"),
                        "sha256": code.get("sha256"),
                        "static_facts": {"installer_calls": []},
                    }
                    for code in ordered_code if isinstance(code, dict)
                ],
                "article": {"fenced_code_blocks": []},
                "images": [
                    {
                        "sha256": image.get("sha256"),
                        "link_confidence": "normalized_registry_link",
                        "native_review_status": "not_reviewed",
                    }
                    for image in images if isinstance(image, dict)
                ],
                "normalized_gaps": gaps,
            })

        self.figures, warnings = _load_jsonl(self.component_dir / "figure-cards.jsonl")
        self.warnings.extend(warnings)
        raw_variants, warnings = _load_jsonl(self.component_dir / "variant-sets.jsonl")
        self.warnings.extend(warnings)
        self.capabilities = []
        for item in raw_variants:
            variants = []
            for variant in _safe_list(item.get("variants")):
                if not isinstance(variant, dict):
                    continue
                variants.append({
                    "variant_id": variant.get("variant_id"),
                    "source_bundle_id": variant.get("implementation_ref"),
                    "source_plot_label": item.get("capability"),
                    "equivalence": variant.get("classification", "unknown"),
                    "maturity": item.get("maturity", "unknown"),
                    "differences": _safe_list(variant.get("differences")),
                })
            self.capabilities.append({
                "capability_module_id": item.get("variant_set_id"),
                "capability": item.get("capability"),
                "semantic_key": item.get("capability"),
                "variants": variants,
            })

    def contexts(self) -> list[CandidateContext]:
        by_bundle: dict[str, CandidateContext] = {}
        for bundle in self.bundles:
            bundle_id = str(bundle.get("bundle_id", ""))
            if bundle_id:
                by_bundle[bundle_id] = CandidateContext(bundle=bundle)

        def ensure_knowledge_only_context(
            source_reference: str,
            title: Any,
            maturity: Any,
            source_type: str,
        ) -> CandidateContext:
            key = f"knowledge-ref-{source_reference}"
            if key not in by_bundle:
                by_bundle[key] = CandidateContext(bundle={
                    "bundle_id": key,
                    "title": title or source_reference,
                    "source_root_id": "normalized-private-registry",
                    "source_root_name": "normalized private registry",
                    "source_type": source_type,
                    "retrieval_kind": "knowledge_object_without_source_flow",
                    "source_reference_ids": [source_reference],
                    "maturity": maturity or "unknown",
                    "flow_integrity": {"reconstructable_from_sources": False},
                    "issues": [{"code": "no_code", "severity": "informational"}],
                    "metadata": {"title": title},
                    "preprocessing_match": None,
                    "package_index": {"packages": [], "qualified_functions": []},
                    "ordered_code_files": [],
                    "article": {"fenced_code_blocks": []},
                    "images": [],
                })
            return by_bundle[key]

        for method in self.methods:
            source_ids = _safe_list(method.get("source_bundle_ids")) or [str(method.get("method_card_id", "unknown-method"))]
            for bundle_id in source_ids:
                if bundle_id in by_bundle:
                    by_bundle[bundle_id].methods.append(method)
                elif self.layer == "normalized-registry":
                    ensure_knowledge_only_context(
                        str(bundle_id), method.get("title"), method.get("maturity"),
                        "method_card_without_source_flow",
                    ).methods.append(method)
        for package in self.packages:
            source_ids = _safe_list(package.get("source_bundle_ids")) or [str(package.get("package_card_id", "unknown-package"))]
            for bundle_id in source_ids:
                if bundle_id in by_bundle:
                    by_bundle[bundle_id].packages.append(package)
                elif self.layer == "normalized-registry":
                    ensure_knowledge_only_context(
                        str(bundle_id), package.get("package"), package.get("maturity"),
                        "package_card_without_source_flow",
                    ).packages.append(package)
        for figure in self.figures:
            bundle_ids = _safe_list(figure.get("source_bundle_ids"))
            if not bundle_ids and figure.get("source_bundle_id"):
                bundle_ids = [figure.get("source_bundle_id")]
            if not bundle_ids:
                bundle_ids = self._article_ids(figure)
            for bundle_id in bundle_ids:
                if bundle_id in by_bundle:
                    by_bundle[bundle_id].figures.append(figure)
                elif self.layer == "normalized-registry":
                    ensure_knowledge_only_context(
                        str(bundle_id), figure.get("question", figure.get("figure_id")),
                        figure.get("maturity"), "figure_card_without_source_flow",
                    ).figures.append(figure)
        for module in self.capabilities:
            for variant in _safe_list(module.get("variants")):
                bundle_id = str(variant.get("source_bundle_id", ""))
                if bundle_id in by_bundle:
                    by_bundle[bundle_id].variants.append({"module": module, "variant": variant})
                elif bundle_id and self.layer == "normalized-registry":
                    ensure_knowledge_only_context(
                        bundle_id, module.get("capability"), variant.get("maturity"),
                        "variant_set_without_source_flow",
                    ).variants.append({"module": module, "variant": variant})
        for context in by_bundle.values():
            context.methods.sort(key=lambda item: str(item.get("method_card_id", "")))
            context.packages.sort(key=lambda item: str(item.get("package_card_id", "")))
            context.figures.sort(key=lambda item: str(item.get("figure_id", item.get("figure_card_id", ""))))
            context.variants.sort(key=lambda item: (
                str(item["module"].get("capability_module_id", "")),
                str(item["variant"].get("variant_id", "")),
            ))
            review_keys = [str(context.bundle.get("bundle_id", ""))]
            review_keys.extend(str(item) for item in _safe_list(context.bundle.get("source_reference_ids")))
            context.manual_review = next(
                (self.review_overlay[key] for key in review_keys if key in self.review_overlay),
                None,
            )
        return [by_bundle[key] for key in sorted(by_bundle)]

    def status(self) -> dict[str, Any]:
        counts = {
            "method_cards": len(self.methods),
            "package_cards": len(self.packages),
            "source_flow_bundles": len(self.bundles),
            "capability_modules": len(self.capabilities),
            "figure_cards": len(self.figures),
        }
        missing = sorted(warning for warning in self.warnings if warning.startswith("missing_component"))
        if not self.index_dir.is_dir():
            status = "unavailable"
        elif missing or any(value == 0 for value in counts.values()):
            status = "partial"
        else:
            status = "available"
        return {
            "status": status,
            "layer": self.layer,
            "counts": counts,
            "warnings": sorted(set(self.warnings)),
            "manual_review_overlay": {
                "status": self.review_overlay_status,
                "record_count": len(self.review_overlay),
            },
            "safe_degradation": (
                "Use installed domain Skills and request manually verified evidence; do not infer private corpus content."
                if status != "available" else None
            ),
        }


def _flow_classification(bundle: Mapping[str, Any]) -> tuple[str, int]:
    issues = {str(item.get("code")) for item in _safe_list(bundle.get("issues")) if isinstance(item, dict)}
    code_files = len(_safe_list(bundle.get("ordered_code_files")))
    article = bundle.get("article") if isinstance(bundle.get("article"), dict) else {}
    code_blocks = len(_safe_list(article.get("fenced_code_blocks")))
    integrity = bundle.get("flow_integrity") if isinstance(bundle.get("flow_integrity"), dict) else {}
    reconstructable = bool(integrity.get("reconstructable_from_sources", False))
    preprocessing = bundle.get("preprocessing_match")
    record = preprocessing.get("record", {}) if isinstance(preprocessing, dict) else {}
    declared = _norm(record.get("代码资产完整度", "")) if isinstance(record, dict) else ""
    if "no_code" in issues or (code_files + code_blocks == 0):
        return "no_code", 0
    complete_hint = any(term in declared for term in ("完整", "多脚本", "全流程", "notebook"))
    if reconstructable and (complete_hint or code_files >= 3 or code_blocks >= 4):
        return "complete_candidate_unverified", 8
    return "partial_candidate_unverified", 3


def _installer_count(bundle: Mapping[str, Any]) -> int:
    count = 0
    for code_file in _safe_list(bundle.get("ordered_code_files")):
        if not isinstance(code_file, dict):
            continue
        facts = code_file.get("static_facts") if isinstance(code_file.get("static_facts"), dict) else {}
        count += len(_safe_list(facts.get("installer_calls")))
    article = bundle.get("article") if isinstance(bundle.get("article"), dict) else {}
    for block in _safe_list(article.get("fenced_code_blocks")):
        if not isinstance(block, dict):
            continue
        facts = block.get("static_facts") if isinstance(block.get("static_facts"), dict) else {}
        count += len(_safe_list(facts.get("installer_calls")))
    return count


def _effective_maturity(context: CandidateContext) -> tuple[str, dict[str, list[str]]]:
    object_maturities: dict[str, list[str]] = {
        "source_flow_bundle": [str(context.bundle.get("maturity", "unknown"))],
        "method_cards": sorted({str(item.get("maturity", "unknown")) for item in context.methods}),
        "package_cards": sorted({str(item.get("maturity", "unknown")) for item in context.packages}),
        "variants": sorted({str(item["variant"].get("maturity", "unknown")) for item in context.variants}),
    }
    observed = [value for values in object_maturities.values() for value in values if value]
    effective = min(observed, key=lambda value: MATURITY_ORDER.get(value, -1)) if observed else "unknown"
    return effective, object_maturities


def _evidence_summary(context: CandidateContext) -> dict[str, Any]:
    bundle = context.bundle
    article = bundle.get("article") if isinstance(bundle.get("article"), dict) else {}
    code_files = _safe_list(bundle.get("ordered_code_files"))
    code_blocks = _safe_list(article.get("fenced_code_blocks"))
    images = _safe_list(bundle.get("images"))
    image_evidence_count = max(len(images), len(context.figures))
    bundle_packages = _safe_list((bundle.get("package_index") or {}).get("packages"))
    linked_packages = [str(item.get("package")) for item in context.packages if item.get("package")]
    packages = sorted(set(str(item) for item in (*bundle_packages, *linked_packages)))
    functions: set[str] = set()
    for item in _safe_list((bundle.get("package_index") or {}).get("qualified_functions")):
        function_name = f"{item[0]}::{item[1]}" if isinstance(item, list) and len(item) >= 2 else str(item)
        if not _is_installer_name(function_name):
            functions.add(function_name)
    for package in context.packages:
        for function_name in _safe_list(package.get("functions")):
            if not _is_installer_name(function_name):
                functions.add(str(function_name))
    flow_class, _ = _flow_classification(bundle)
    return {
        "flow_classification": flow_class,
        "ordered_code_file_count": len(code_files),
        "ordered_code_block_count": len(code_blocks),
        "image_count": image_evidence_count,
        "explicit_image_link_count": sum(
            1 for image in images
            if isinstance(image, dict) and image.get("link_confidence") == "explicit_article_reference"
        ),
        "package_names": packages[:30],
        "non_installer_function_names": sorted(functions)[:20],
        "method_card_ids": [str(item.get("method_card_id")) for item in context.methods],
        "package_card_ids": [str(item.get("package_card_id")) for item in context.packages],
        "figure_card_ids": [
            str(item.get("figure_id", item.get("figure_card_id"))) for item in context.figures
        ],
        "capability_module_ids": sorted({
            str(item["module"].get("capability_module_id")) for item in context.variants
        }),
        "raw_code_emitted": False,
    }


def _variant_decisions(context: CandidateContext, execution_eligible: bool) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for link in context.variants:
        module = link["module"]
        variant = link["variant"]
        equivalence = str(variant.get("equivalence", "unknown"))
        policy_auto = equivalence == "exact"
        boundaries = {
            "compatible": "explain_differences_before_selection",
            "alternative_method": "explicit_method_choice_required_no_auto_substitution",
            "exact": "automatic_trial_policy_permitted_after_execution_gate",
        }
        decisions.append({
            "capability_module_id": module.get("capability_module_id"),
            "variant_id": variant.get("variant_id"),
            "equivalence": equivalence,
            "policy_allows_auto_trial": policy_auto,
            "currently_auto_trialable": bool(policy_auto and execution_eligible),
            "boundary": boundaries.get(equivalence, "semantic_review_required"),
        })
    return decisions


def _score(context: CandidateContext, request: RetrievalRequest) -> tuple[float, float, list[dict[str, Any]], set[str]]:
    text = context.searchable_text()
    doc_tokens = _tokens(text)
    request_text = " ".join(filter(None, (request.query, request.package, request.figure_intent)))
    query_tokens = _tokens(request_text)
    matched_terms = query_tokens & doc_tokens
    explanations: list[dict[str, Any]] = []

    lexical = min(32.0, 32.0 * len(matched_terms) / max(2, len(query_tokens)))
    normalized_query = _norm(request.query)
    phrase = 6.0 if normalized_query and len(normalized_query) >= 3 and normalized_query in _norm(text) else 0.0
    title_phrase = 4.0 if normalized_query and len(normalized_query) >= 3 and normalized_query in _norm(context.bundle.get("title")) else 0.0
    if lexical or phrase or title_phrase:
        explanations.append({
            "component": "lexical_overlap", "label_zh": "中英文词项匹配",
            "points": round(lexical + phrase + title_phrase, 3),
            "matched_terms": sorted(matched_terms)[:30],
        })

    domain_text = context.domain_text()
    domains = _domain_set(domain_text)
    if context.methods:
        domains.add("literature-methodology")
    if context.figures or context.variants:
        domains.add("visualization")
    domain_score = 0.0
    if request.domain:
        domain_score = 20.0 if request.domain in domains else 0.0
        explanations.append({
            "component": "domain_match", "label_zh": "领域匹配", "points": domain_score,
            "requested": request.domain, "candidate_domains": sorted(domains),
        })

    package_score = 0.0
    if request.package:
        requested_package = _norm(request.package)
        candidate_packages = {_norm(item.get("package")) for item in context.packages if item.get("package")}
        candidate_packages.update(_norm(item) for item in _safe_list((context.bundle.get("package_index") or {}).get("packages")))
        reviewed_packages = {
            _norm(item)
            for item in _safe_list((context.manual_review or {}).get("package_names"))
            if item
        }
        candidate_packages.update(reviewed_packages)
        if requested_package in candidate_packages:
            package_score = 20.0
        elif any(requested_package in item or item in requested_package for item in candidate_packages if item):
            package_score = 10.0
        matched_packages = sorted(
            item for item in candidate_packages
            if requested_package in item or item in requested_package
        )[:10]
        explanations.append({
            "component": "package_match", "label_zh": "软件包匹配", "points": package_score,
            "requested": request.package,
            "matched": matched_packages,
            "reviewed_package_match": sorted(
                item for item in reviewed_packages
                if requested_package in item or item in requested_package
            )[:10],
        })

    figure_score = 0.0
    if request.figure_intent:
        figure_tokens = _tokens(request.figure_intent)
        overlap = figure_tokens & doc_tokens
        figure_score = min(14.0, 14.0 * len(overlap) / max(2, min(8, len(figure_tokens))))
        explanations.append({
            "component": "figure_intent_match", "label_zh": "图形意图匹配",
            "points": round(figure_score, 3), "matched_terms": sorted(overlap)[:20],
        })

    requested_concepts, matched_concepts = _concept_matches(request_text, text)
    concept_score = 0.0
    if requested_concepts:
        concept_score = 20.0 * len(matched_concepts) / len(requested_concepts)
        explanations.append({
            "component": "scientific_concept_match",
            "label_zh": "科学分析概念匹配",
            "points": round(concept_score, 3),
            "requested_concepts": requested_concepts,
            "matched_concepts": matched_concepts,
        })

    flow_class, flow_score = _flow_classification(context.bundle)
    explanations.append({
        "component": "flow_completeness", "label_zh": "完整流程候选优先",
        "points": float(flow_score), "classification": flow_class, "verified": False,
    })

    images = _safe_list(context.bundle.get("images"))
    article = context.bundle.get("article") if isinstance(context.bundle.get("article"), dict) else {}
    code_evidence = bool(_safe_list(context.bundle.get("ordered_code_files"))) or bool(_safe_list(article.get("fenced_code_blocks")))
    figure_evidence = bool(images or context.figures)
    evidence_score = (4.0 if code_evidence else 0.0) + (3.0 if figure_evidence else 0.0)
    explanations.append({
        "component": "code_figure_evidence", "label_zh": "代码与结果图证据",
        "points": evidence_score, "code_present": code_evidence, "figure_present": figure_evidence,
    })

    effective_maturity, _ = _effective_maturity(context)
    maturity_score = 2.0 * max(0, MATURITY_ORDER.get(effective_maturity, -1))
    explanations.append({
        "component": "maturity", "label_zh": "成熟度证据", "points": maturity_score,
        "effective_maturity": effective_maturity,
    })

    issue_codes = {str(item.get("code")) for item in _safe_list(context.bundle.get("issues")) if isinstance(item, dict)}
    penalties = 0.0
    penalty_reasons: list[str] = []
    for issue, amount in {
        "no_code": 8.0, "hard_coded_paths": 3.0, "installer_calls_present": 2.0,
        "image_count_mismatch": 1.0, "fenced_code_count_mismatch": 2.0,
    }.items():
        if issue in issue_codes:
            penalties += amount
            penalty_reasons.append(issue)
    if penalties:
        explanations.append({
            "component": "source_issues", "label_zh": "来源缺口扣分",
            "points": -penalties, "issues": sorted(penalty_reasons),
        })

    review_score = 0.0
    if context.manual_review:
        review_maturity = context.manual_review["maturity"]
        review_score = 12.0 if review_maturity == "parse-verified" else 8.0
        deep_review = context.manual_review.get("review_kind") == "deep_record_review"
        explanations.append({
            "component": "manual_deep_review" if deep_review else "manual_gold_review",
            "label_zh": "人工逐记录深审" if deep_review else "人工金标准复核",
            "points": review_score,
            "review_maturity": review_maturity,
            "code_figure_unresolved": context.manual_review["code_figure_unresolved"],
        })

    relevance = lexical + phrase + title_phrase + domain_score + package_score + figure_score + concept_score
    total = max(0.0, min(100.0, relevance + flow_score + evidence_score + maturity_score + review_score - penalties))
    return round(total, 3), round(relevance, 3), explanations, matched_terms


def _gaps(context: CandidateContext, effective_maturity: str, installer_count: int) -> list[str]:
    gaps: set[str] = set()
    if effective_maturity not in EXECUTABLE_MATURITIES:
        gaps.add(f"execution_evidence_missing:maturity={effective_maturity}")
    for method in context.methods:
        gaps.update(str(item) for item in _safe_list(method.get("required_validation")))
        if method.get("review_status") == "not_manually_reviewed":
            gaps.add("manual_methodology_review")
        if str(method.get("analysis_unit", "unknown")) == "unknown":
            gaps.add("statistical_unit_review")
    for issue in _safe_list(context.bundle.get("issues")):
        if isinstance(issue, dict) and issue.get("code"):
            gaps.add(str(issue["code"]))
    if installer_count:
        gaps.add("strip_installers_from_recipe")
    images = _safe_list(context.bundle.get("images"))
    if images and any(image.get("native_review_status") != "reviewed" for image in images if isinstance(image, dict)):
        gaps.add("native_figure_review")
    if context.manual_review:
        gaps.add("manual_review_not_fixture_or_data_verified")
    return sorted(gaps)


def _make_candidate(context: CandidateContext, request: RetrievalRequest) -> tuple[dict[str, Any], float]:
    score, relevance, components, matched_terms = _score(context, request)
    effective_maturity, object_maturities = _effective_maturity(context)
    installer_count = _installer_count(context.bundle)
    issue_codes = {str(item.get("code")) for item in _safe_list(context.bundle.get("issues")) if isinstance(item, dict)}
    installer_detected = installer_count > 0 or "installer_calls_present" in issue_codes
    manual_review_only = context.manual_review is not None
    executable = (
        effective_maturity in EXECUTABLE_MATURITIES
        and not installer_detected
        and not ({"no_code", "fenced_code_count_mismatch"} & issue_codes)
        and not manual_review_only
    )
    integrity = context.bundle.get("flow_integrity") if isinstance(context.bundle.get("flow_integrity"), dict) else {}
    materializable = bool(integrity.get("reconstructable_from_sources", False))
    candidate = {
        "candidate_id": context.bundle.get("bundle_id"),
        "kind": context.bundle.get("retrieval_kind", "source_flow_bundle_with_linked_knowledge"),
        "title": context.bundle.get("title"),
        "source_root_id": context.bundle.get("source_root_id"),
        "source_reference_ids": _safe_list(context.bundle.get("source_reference_ids")),
        "score": score,
        "score_explanation": components,
        "matched_terms": sorted(matched_terms)[:30],
        "maturity": {
            "effective": effective_maturity, "by_object_type": object_maturities,
            "raw_extracted_is_executable": False,
        },
        "evidence": _evidence_summary(context),
        "gaps": _gaps(context, effective_maturity, installer_count),
        "manual_review": context.manual_review,
        "materialization": {
            "eligible": materializable,
            "purpose": "private_review_or_task_local_reconstruction",
            "conditions": [
                "explicit_task_run_directory",
                "verify_every_source_hash_with_corpus_distiller_materialize",
                "do_not_publish_article_code_or_images",
            ] if materializable else ["source_flow_not_reconstructable"],
        },
        "recipe_safety": {
            "eligible": executable,
            "installer_calls_detected": installer_detected,
            "installer_call_count": installer_count,
            "raw_installation_code_emitted": False,
            "required_action": (
                "remove_installation_calls_and_declare_dependencies_for_EnvironmentManager"
                if installer_detected else "declare_dependencies_only"
            ),
        },
        "execution": {
            "eligible": executable,
            "requires_explicit_authorization": True,
            "reason": (
                "eligible_only_after_request_specific_scientific_gates_and_environment_lock"
                if executable else (
                    "manual_review_overlay_has_no_fixture_or_data_verification"
                    if manual_review_only
                    else "candidate_lacks_fixture-or-data-verified_executable_evidence_or_has_blocking_source_issues"
                )
            ),
        },
        "variant_decisions": _variant_decisions(context, executable),
    }
    return candidate, relevance


def _exact_duplicate_representative_priority(candidate: Mapping[str, Any]) -> int:
    """Prefer the reviewed canonical source without changing candidate evidence.

    Deep-review decisions are exposed as a deterministic ``" | "`` joined
    classification string.  An explicit ``canonical`` decision is strongest;
    otherwise any source not classified as ``duplicate_reuse`` is preferred.
    The caller retains the existing score-and-ID order as the stable fallback,
    so this function adds no ranking points and borrows no maturity or execution
    status from another member of the exact-duplicate group.
    """

    review = candidate.get("manual_review")
    if not isinstance(review, Mapping):
        return 1
    decisions = {
        _norm(item)
        for item in str(review.get("decision") or "").split("|")
        if _norm(item)
    }
    if "canonical" in decisions:
        return 0
    if "duplicate_reuse" in decisions:
        return 2
    return 1


def retrieve(index: KnowledgeIndex, request: RetrievalRequest, limit: int) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    status = index.status()
    candidates: list[tuple[dict[str, Any], float, str, str]] = []
    for context in index.contexts():
        candidate, relevance = _make_candidate(context, request)
        if relevance > 0:
            dedupe_basis, dedupe_key = _exact_duplicate_key(context)
            candidates.append((candidate, relevance, dedupe_basis, dedupe_key))
    candidates.sort(key=lambda item: (-float(item[0]["score"]), str(item[0]["candidate_id"])))
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate, _relevance, dedupe_basis, dedupe_key in candidates:
        group_key = (dedupe_basis, dedupe_key)
        group = groups.get(group_key)
        if group is None:
            group = {
                "members": [candidate],
                "source_candidate_ids": [str(candidate["candidate_id"])],
                "reviewed_source_count": int(candidate.get("manual_review") is not None),
                "basis": dedupe_basis,
            }
            groups[group_key] = group
        else:
            group["members"].append(candidate)
            group["source_candidate_ids"].append(str(candidate["candidate_id"]))
            group["reviewed_source_count"] += int(candidate.get("manual_review") is not None)

    duplicate_groups = 0
    folded_sources = 0
    deduplicated: list[dict[str, Any]] = []
    for group in groups.values():
        # Members retain the pre-existing score-descending, candidate-ID order.
        # min() therefore changes only the displayed source identity when an
        # explicit canonical/non-duplicate review decision is available.
        representative = min(
            group["members"],
            key=_exact_duplicate_representative_priority,
        )
        deduplicated.append(representative)
        source_ids = sorted(set(group["source_candidate_ids"]))
        if len(source_ids) < 2 or group["basis"] == "candidate_id":
            continue
        duplicate_groups += 1
        folded_sources += len(source_ids) - 1
        representative["duplicate_provenance"] = {
            "dedupe_basis": group["basis"],
            "source_count": len(source_ids),
            "source_candidate_ids": source_ids,
            "reviewed_source_count": group["reviewed_source_count"],
            "evidence_multiplier_applied": False,
        }
        representative["score_explanation"].append({
            "component": "exact_duplicate_folding",
            "label_zh": "精确重复来源折叠",
            "points": 0.0,
            "source_count": len(source_ids),
        })

    # A preferred canonical member can have a different request-specific score
    # from a duplicate-reuse member.  Rank the selected candidates by their own
    # untouched scores; never transfer the discarded member's score or gates.
    deduplicated.sort(key=lambda item: (-float(item["score"]), str(item["candidate_id"])))
    selected = deduplicated[:limit]
    for rank, candidate in enumerate(selected, 1):
        candidate["rank"] = rank
    if status["status"] == "unavailable":
        decision = "private_index_unavailable_no_corpus_claims_made"
    elif not selected:
        decision = "no_relevant_candidate_manual_method_review_required"
    else:
        decision = "candidates_only_scientific_approval_and_execution_gates_still_required"
    return {
        "schema_version": SCHEMA_VERSION,
        "request": request.public_dict(),
        "index": status,
        "policy": {
            "raw_extracted_never_executable": True,
            "source_code_and_private_paths_emitted": False,
            "installation_commands_must_not_enter_analysis_recipe": True,
            "exact_duplicate_folding": (
                "same normalized title plus multi-file ordered-code sequence, or flow fingerprint; "
                "retain provenance without evidence or score multiplication"
            ),
            "variant_boundary": {
                "exact": "may_be_auto_trialed_only_after_execution_gate",
                "compatible": "must_explain_differences_before_selection",
                "alternative_method": "explicit_method_choice_required_no_auto_substitution",
            },
        },
        "decision": decision,
        "deduplication": {
            "relevant_candidates_before_folding": len(candidates),
            "unique_candidates_after_folding": len(deduplicated),
            "exact_duplicate_groups_folded": duplicate_groups,
            "folded_source_candidates": folded_sources,
        },
        "candidate_count": len(selected),
        "candidates": selected,
    }


def _request_from_args(args: argparse.Namespace) -> RetrievalRequest:
    payload: dict[str, Any] = {}
    if args.request:
        raw = json.loads(Path(args.request).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("request JSON must be an object")
        payload.update(raw)
    for key, value in {
        "query": args.query, "domain": args.domain, "package": args.package,
        "figure_intent": args.figure, "mode": args.mode,
    }.items():
        if value is not None:
            payload[key] = value
    return RetrievalRequest.from_mapping(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Jointly retrieve private MethodCards, PackageCards, SourceFlowBundles, and CapabilityModules."
    )
    parser.add_argument("--request", help="UTF-8 JSON request object")
    parser.add_argument("--query", help="Chinese or English analysis requirement")
    parser.add_argument("--domain", help="Requested domain, for example single-cell or 空间转录组")
    parser.add_argument("--package", help="Requested R or Python package")
    parser.add_argument("--figure", help="Figure intent, for example donor-aware composition plot")
    parser.add_argument("--mode", choices=("plan", "run", "resume", "reproduce-figure", "explain"))
    parser.add_argument("--index", default=str(DEFAULT_INDEX), help="Private corpus index directory")
    parser.add_argument("--limit", type=int, default=5, help="Maximum candidates (default: 5)")
    parser.add_argument("--output", help="Write JSON to this file instead of stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        request = _request_from_args(args)
        index = KnowledgeIndex(Path(args.index).resolve())
        index.load()
        result = retrieve(index, request, args.limit)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
