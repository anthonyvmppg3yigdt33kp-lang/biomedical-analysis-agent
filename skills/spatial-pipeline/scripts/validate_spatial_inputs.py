#!/usr/bin/env python3
"""Validate spatial-transcriptomics manifests and tabular contracts.

This module intentionally uses only the Python standard library.  It performs
structural validation before large platform objects are loaded; it does not
claim biological, image-registration, or segmentation validity.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, TextIO


PLATFORM_CONTRACTS: dict[str, tuple[str, set[str]]] = {
    "visium": ("capture", {"spot"}),
    "visium_hd": ("capture", {"bin", "cell"}),
    "stereo_seq": ("capture", {"bin", "cell"}),
    "xenium": ("imaging", {"cell"}),
    "cosmx": ("imaging", {"cell"}),
    "merfish": ("imaging", {"cell"}),
    "other_capture": ("capture", {"spot", "bin", "cell"}),
    "other_imaging": ("imaging", {"cell"}),
}

COORDINATE_UNITS = {"pixel", "micron", "array", "other"}
KNOWN_MODULES = {
    "domains",
    "svg",
    "deconvolution",
    "neighborhoods",
    "gradients",
    "communication",
    "scrna_mapping",
    "image_overlay",
    "image_analysis",
    "group_contrast",
}
PATH_FIELDS = {
    "input_root",
    "matrix_path",
    "coordinates_path",
    "metadata_path",
    "image_path",
    "transform_path",
    "transcripts_path",
    "segmentation_path",
    "controls_path",
    "reference_path",
}


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    message: str
    location: str | None = None


@dataclass
class ValidationResult:
    target: str
    kind: str
    issues: list[Issue]
    summary: dict[str, Any]

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" for item in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "target": self.target,
            "kind": self.kind,
            "summary": self.summary,
            "issues": [asdict(item) for item in self.issues],
            "scope_note": (
                "Structural validation only; image registration, segmentation, "
                "count integrity, statistical design, and biological adequacy "
                "require stage-specific review."
            ),
        }


def _issue(
    issues: list[Issue],
    severity: str,
    code: str,
    message: str,
    location: str | None = None,
) -> None:
    issues.append(Issue(severity, code, message, location))


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def _resolve_path(manifest_path: Path, raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = manifest_path.parent / candidate
    return candidate.resolve(strict=False)


def _has_explicit_matrix_and_coordinates(sample: dict[str, Any]) -> bool:
    return _nonempty_string(sample.get("matrix_path")) and _nonempty_string(
        sample.get("coordinates_path")
    )


def validate_manifest(path: Path, check_paths: bool = False) -> ValidationResult:
    issues: list[Issue] = []
    summary: dict[str, Any] = {"samples": 0, "subjects": 0, "sections": 0}

    try:
        payload = _read_json(path)
    except FileNotFoundError:
        _issue(issues, "error", "file_not_found", "Manifest does not exist.")
        return ValidationResult(str(path), "manifest", issues, summary)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _issue(issues, "error", "invalid_json", f"Cannot read manifest: {exc}")
        return ValidationResult(str(path), "manifest", issues, summary)

    if not isinstance(payload, dict):
        _issue(issues, "error", "manifest_not_object", "Manifest must be a JSON object.")
        return ValidationResult(str(path), "manifest", issues, summary)

    if payload.get("schema_version") != "1.0":
        _issue(
            issues,
            "error",
            "schema_version",
            "schema_version must equal '1.0'.",
            "schema_version",
        )

    platform = payload.get("platform")
    if platform not in PLATFORM_CONTRACTS:
        _issue(
            issues,
            "error",
            "unsupported_platform",
            f"platform must be one of: {', '.join(sorted(PLATFORM_CONTRACTS))}.",
            "platform",
        )
        expected_class = None
        allowed_units: set[str] = set()
    else:
        expected_class, allowed_units = PLATFORM_CONTRACTS[platform]

    assay_class = payload.get("assay_class")
    if expected_class is not None and assay_class != expected_class:
        _issue(
            issues,
            "error",
            "assay_class_mismatch",
            f"{platform} requires assay_class='{expected_class}'.",
            "assay_class",
        )

    assay_unit = payload.get("assay_unit")
    if allowed_units and assay_unit not in allowed_units:
        _issue(
            issues,
            "error",
            "assay_unit_mismatch",
            f"{platform} permits assay_unit: {', '.join(sorted(allowed_units))}.",
            "assay_unit",
        )

    if not _nonempty_string(payload.get("species")):
        _issue(issues, "error", "missing_species", "species is required.", "species")

    coordinate_unit = payload.get("coordinate_unit")
    if coordinate_unit not in COORDINATE_UNITS:
        _issue(
            issues,
            "error",
            "coordinate_unit",
            f"coordinate_unit must be one of: {', '.join(sorted(COORDINATE_UNITS))}.",
            "coordinate_unit",
        )

    modules = payload.get("requested_modules", [])
    if not isinstance(modules, list) or any(not _nonempty_string(x) for x in modules):
        _issue(
            issues,
            "error",
            "invalid_requested_modules",
            "requested_modules must be an array of non-empty strings.",
            "requested_modules",
        )
        modules_set: set[str] = set()
    else:
        modules_set = set(modules)
        unknown = sorted(modules_set - KNOWN_MODULES)
        if unknown:
            _issue(
                issues,
                "warning",
                "unknown_modules",
                "Unrecognized modules require an explicit contract: " + ", ".join(unknown),
                "requested_modules",
            )
        if len(modules_set) != len(modules):
            _issue(
                issues,
                "warning",
                "duplicate_modules",
                "requested_modules contains duplicates.",
                "requested_modules",
            )

    if assay_class == "imaging" and assay_unit == "cell" and "deconvolution" in modules_set:
        _issue(
            issues,
            "error",
            "deconvolution_unit_mismatch",
            "Cell-level imaging data should not be deconvolved unless an explicitly aggregated mixed-unit contract is supplied.",
            "requested_modules",
        )

    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples:
        _issue(issues, "error", "missing_samples", "samples must be a non-empty array.", "samples")
        samples = []

    sample_ids: set[str] = set()
    subject_ids: set[str] = set()
    sections: set[tuple[str, str]] = set()
    groups: set[str] = set()

    for index, sample in enumerate(samples):
        location = f"samples[{index}]"
        if not isinstance(sample, dict):
            _issue(issues, "error", "sample_not_object", "Sample must be an object.", location)
            continue

        for field in ("sample_id", "subject_id", "section_id"):
            if not _nonempty_string(sample.get(field)):
                _issue(
                    issues,
                    "error",
                    f"missing_{field}",
                    f"{field} is required and must be non-empty.",
                    f"{location}.{field}",
                )

        sample_id = sample.get("sample_id")
        if _nonempty_string(sample_id):
            if sample_id in sample_ids:
                _issue(
                    issues,
                    "error",
                    "duplicate_sample_id",
                    f"Duplicate sample_id: {sample_id}",
                    f"{location}.sample_id",
                )
            sample_ids.add(sample_id)

        subject_id = sample.get("subject_id")
        section_id = sample.get("section_id")
        if _nonempty_string(subject_id):
            subject_ids.add(subject_id)
        if _nonempty_string(subject_id) and _nonempty_string(section_id):
            section_key = (subject_id, section_id)
            if section_key in sections:
                _issue(
                    issues,
                    "warning",
                    "duplicate_subject_section",
                    "Repeated subject_id/section_id must be justified (for example distinct libraries).",
                    location,
                )
            sections.add(section_key)

        group = sample.get("group")
        if _nonempty_string(group):
            groups.add(group)

        root_supplied = _nonempty_string(sample.get("input_root"))
        explicit_supplied = _has_explicit_matrix_and_coordinates(sample)
        if not (root_supplied or explicit_supplied):
            _issue(
                issues,
                "error",
                "insufficient_sample_inputs",
                "Provide input_root or both matrix_path and coordinates_path.",
                location,
            )

        for field in PATH_FIELDS:
            raw = sample.get(field)
            if raw is not None and not _nonempty_string(raw):
                _issue(
                    issues,
                    "error",
                    "invalid_path_value",
                    f"{field} must be a non-empty path string when present.",
                    f"{location}.{field}",
                )

        if not root_supplied:
            if "image_overlay" in modules_set:
                for field in ("image_path", "transform_path"):
                    if not _nonempty_string(sample.get(field)):
                        _issue(
                            issues,
                            "error",
                            "missing_overlay_input",
                            f"image_overlay requires {field} when input_root is not supplied.",
                            f"{location}.{field}",
                        )
            if assay_class == "imaging" and "image_analysis" in modules_set:
                for field in ("image_path", "segmentation_path"):
                    if not _nonempty_string(sample.get(field)):
                        _issue(
                            issues,
                            "error",
                            "missing_image_analysis_input",
                            f"image_analysis requires {field} when input_root is not supplied.",
                            f"{location}.{field}",
                        )

        if check_paths:
            _validate_sample_paths(path, sample, index, issues)

    if "group_contrast" in modules_set:
        missing_group = [
            i for i, sample in enumerate(samples)
            if isinstance(sample, dict) and not _nonempty_string(sample.get("group"))
        ]
        if missing_group:
            _issue(
                issues,
                "error",
                "missing_group",
                "group_contrast requires group for every sample.",
                "samples",
            )
        if len(subject_ids) < 2:
            _issue(
                issues,
                "error",
                "insufficient_independent_units",
                "group_contrast requires at least two distinct subject_id values; practical inference normally requires more.",
                "samples",
            )
        if len(groups) < 2:
            _issue(
                issues,
                "error",
                "insufficient_groups",
                "group_contrast requires at least two groups.",
                "samples",
            )

    summary.update(
        {
            "samples": len(sample_ids),
            "subjects": len(subject_ids),
            "sections": len(sections),
            "groups": len(groups),
            "platform": platform,
            "assay_class": assay_class,
            "assay_unit": assay_unit,
            "requested_modules": sorted(modules_set),
            "paths_checked": check_paths,
        }
    )
    return ValidationResult(str(path), "manifest", issues, summary)


def _validate_sample_paths(
    manifest_path: Path,
    sample: dict[str, Any],
    sample_index: int,
    issues: list[Issue],
) -> None:
    for field in sorted(PATH_FIELDS):
        raw = sample.get(field)
        if not _nonempty_string(raw):
            continue
        resolved = _resolve_path(manifest_path, raw)
        location = f"samples[{sample_index}].{field}"
        if not resolved.exists():
            _issue(
                issues,
                "error",
                "path_not_found",
                f"Path does not exist: {resolved}",
                location,
            )
            continue
        if field == "input_root" and not resolved.is_dir():
            _issue(issues, "error", "input_root_not_directory", "input_root must be a directory.", location)
        elif field != "input_root" and not resolved.is_file():
            _issue(issues, "error", "expected_file", f"{field} must resolve to a file.", location)
        elif field in {"coordinates_path", "metadata_path"}:
            table_kind = "coordinates" if field == "coordinates_path" else "metadata"
            table_result = validate_table(resolved, table_kind)
            for item in table_result.issues:
                nested_location = location
                if item.location:
                    nested_location += f":{item.location}"
                _issue(
                    issues,
                    item.severity,
                    f"{table_kind}_{item.code}",
                    item.message,
                    nested_location,
                )


def _open_text(path: Path) -> TextIO:
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8-sig", newline="")
    return path.open("r", encoding="utf-8-sig", newline="")


def _delimiter(path: Path, first_line: str) -> str:
    lower_name = path.name.lower()
    if lower_name.endswith((".tsv", ".tsv.gz", ".txt", ".txt.gz")):
        return "\t"
    if lower_name.endswith((".csv", ".csv.gz")):
        return ","
    return "\t" if first_line.count("\t") > first_line.count(",") else ","


def _rows(path: Path) -> tuple[list[str], Iterable[dict[str, str]], TextIO]:
    handle = _open_text(path)
    first_line = handle.readline()
    if not first_line:
        handle.close()
        raise ValueError("table is empty")
    delimiter = _delimiter(path, first_line)
    handle.seek(0)
    raw_reader = csv.reader(handle, delimiter=delimiter)
    try:
        header = next(raw_reader)
    except StopIteration as exc:
        handle.close()
        raise ValueError("table is empty") from exc
    handle.seek(0)
    reader = csv.DictReader(handle, delimiter=delimiter)
    return [item.strip() for item in header], reader, handle


def validate_table(path: Path, kind: str) -> ValidationResult:
    issues: list[Issue] = []
    summary: dict[str, Any] = {"rows": 0, "columns": 0}
    try:
        header, reader, handle = _rows(path)
    except FileNotFoundError:
        _issue(issues, "error", "file_not_found", "Table does not exist.")
        return ValidationResult(str(path), kind, issues, summary)
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        _issue(issues, "error", "table_read_error", f"Cannot read table: {exc}")
        return ValidationResult(str(path), kind, issues, summary)

    try:
        summary["columns"] = len(header)
        if any(not item for item in header):
            _issue(issues, "error", "empty_column_name", "Column names must be non-empty.", "header")
        duplicates = sorted({item for item in header if header.count(item) > 1})
        if duplicates:
            _issue(
                issues,
                "error",
                "duplicate_columns",
                "Duplicate column names: " + ", ".join(duplicates),
                "header",
            )

        if kind == "coordinates":
            required = {"sample_id", "unit_id", "x", "y", "coordinate_system"}
        elif kind == "metadata":
            required = {"sample_id"}
        else:
            raise ValueError(f"Unsupported table kind: {kind}")

        missing = sorted(required - set(header))
        if missing:
            _issue(
                issues,
                "error",
                "missing_columns",
                "Missing required columns: " + ", ".join(missing),
                "header",
            )

        if kind == "metadata" and "unit_id" not in header:
            sample_required = {"subject_id", "section_id"}
            missing_sample = sorted(sample_required - set(header))
            if missing_sample:
                _issue(
                    issues,
                    "error",
                    "metadata_level_ambiguous",
                    "Metadata without unit_id must include subject_id and section_id.",
                    "header",
                )

        seen: set[tuple[str, ...]] = set()
        coordinate_systems: set[str] = set()
        for row_number, row in enumerate(reader, start=2):
            summary["rows"] += 1
            if None in row:
                _issue(
                    issues,
                    "error",
                    "extra_fields",
                    "Row contains more fields than the header.",
                    f"row:{row_number}",
                )
                continue

            sample_id = (row.get("sample_id") or "").strip()
            if not sample_id:
                _issue(issues, "error", "empty_sample_id", "sample_id is empty.", f"row:{row_number}")

            if "unit_id" in header:
                unit_id = (row.get("unit_id") or "").strip()
                if not unit_id:
                    _issue(issues, "error", "empty_unit_id", "unit_id is empty.", f"row:{row_number}")
                key = (sample_id, unit_id)
            else:
                subject_id = (row.get("subject_id") or "").strip()
                section_id = (row.get("section_id") or "").strip()
                if not subject_id or not section_id:
                    _issue(
                        issues,
                        "error",
                        "empty_subject_or_section",
                        "subject_id and section_id must be non-empty for sample metadata.",
                        f"row:{row_number}",
                    )
                key = (sample_id,)

            if key in seen:
                _issue(
                    issues,
                    "error",
                    "duplicate_identity",
                    "Duplicate composite table identity.",
                    f"row:{row_number}",
                )
            seen.add(key)

            if kind == "coordinates":
                for column in ("x", "y"):
                    raw = (row.get(column) or "").strip()
                    try:
                        value = float(raw)
                    except ValueError:
                        value = math.nan
                    if not math.isfinite(value):
                        _issue(
                            issues,
                            "error",
                            "non_finite_coordinate",
                            f"{column} must be a finite number.",
                            f"row:{row_number}.{column}",
                        )
                coordinate_system = (row.get("coordinate_system") or "").strip()
                if not coordinate_system:
                    _issue(
                        issues,
                        "error",
                        "empty_coordinate_system",
                        "coordinate_system is empty.",
                        f"row:{row_number}",
                    )
                else:
                    coordinate_systems.add(coordinate_system)

        if summary["rows"] == 0:
            _issue(issues, "error", "no_data_rows", "Table contains no data rows.")
        if kind == "coordinates":
            summary["coordinate_systems"] = sorted(coordinate_systems)
            if len(coordinate_systems) > 1:
                _issue(
                    issues,
                    "warning",
                    "multiple_coordinate_systems",
                    "Multiple coordinate systems require an explicit transform/harmonization step.",
                )
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        _issue(issues, "error", "table_parse_error", f"Cannot parse table: {exc}")
    finally:
        handle.close()

    return ValidationResult(str(path), kind, issues, summary)


def _print_result(result: ValidationResult, json_output: bool) -> None:
    payload = result.to_dict()
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"{'PASS' if result.ok else 'FAIL'} {result.kind}: {result.target}")
    print(json.dumps(result.summary, ensure_ascii=False, sort_keys=True))
    for issue in result.issues:
        location = f" [{issue.location}]" if issue.location else ""
        print(f"{issue.severity.upper()} {issue.code}{location}: {issue.message}")
    print(payload["scope_note"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest_parser = subparsers.add_parser("manifest", help="Validate an input manifest.")
    manifest_parser.add_argument("path", type=Path)
    manifest_parser.add_argument("--check-paths", action="store_true")
    manifest_parser.add_argument("--json", action="store_true", dest="json_output")

    table_parser = subparsers.add_parser("table", help="Validate a CSV/TSV table.")
    table_parser.add_argument("path", type=Path)
    table_parser.add_argument("--kind", required=True, choices=("coordinates", "metadata"))
    table_parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "manifest":
        result = validate_manifest(args.path, check_paths=args.check_paths)
    else:
        result = validate_table(args.path, args.kind)
    _print_result(result, args.json_output)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
