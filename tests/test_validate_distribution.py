import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_distribution.py"
SPEC = importlib.util.spec_from_file_location("validate_distribution", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_private_home_pattern_detects_user_profiles_only():
    assert MODULE.PRIVATE_HOME.search("C:" + r"\Users\Alice\private\file.txt")
    assert MODULE.PRIVATE_HOME.search(
        "C:" + "\\\\" + "Users" + "\\\\" + "Alice" + "\\\\private\\\\file.txt"
    )
    assert MODULE.PRIVATE_HOME.search("/home/" + "alice/private/file.txt")
    assert not MODULE.PRIVATE_HOME.search(r"C:\Program Files\R\bin\Rscript.exe")
    assert not MODULE.PRIVATE_HOME.search("D:/data/example.tsv")
    assert not MODULE.PRIVATE_HOME.search(r"[A-Z]:[\\/]Users[\\/][^\\/\s\"']+")
    assert not MODULE.PRIVATE_HOME.search(r"/home/[^/\s\"']+")


def test_high_confidence_secret_patterns_do_not_match_documentation_literals():
    assert MODULE.SECRET_TOKEN.search("github_pat_" + "a" * 24)
    assert MODULE.SECRET_TOKEN.search("ghp_" + "A" * 24)
    assert MODULE.SECRET_TOKEN.search("AKIA" + "A1" * 8)
    assert not MODULE.SECRET_TOKEN.search(r"github_pat_[A-Za-z0-9_]{20,}")
    assert MODULE.AUTHORIZATION_CREDENTIAL.search(
        "Authorization: Bearer " + "a" * 20
    )
    assert MODULE.PRIVATE_KEY_MATERIAL.search(
        "-----BEGIN " + "PRIVATE KEY-----"
    )


def test_expected_output_absolute_path_pattern_allows_urls_and_relative_paths():
    assert MODULE.EXPECTED_OUTPUT_ABSOLUTE.search("C:" + r"\Users\Alice\run.json")
    assert MODULE.EXPECTED_OUTPUT_ABSOLUTE.search("/home/" + "alice/run.json")
    assert MODULE.EXPECTED_OUTPUT_ABSOLUTE.search("file://example/run.json")
    assert not MODULE.EXPECTED_OUTPUT_ABSOLUTE.search(
        "https://example.org/data/file.h5"
    )
    assert not MODULE.EXPECTED_OUTPUT_ABSOLUTE.search(
        "manifest/execution-summary.json"
    )


def test_validate_reports_credentials_and_expected_output_absolute_paths(tmp_path):
    (tmp_path / "expected-output").mkdir()
    (tmp_path / "expected-output" / "summary.json").write_text(
        '{"source": "D:/portable/run.json"}\n', encoding="utf-8"
    )
    (tmp_path / "credentials.txt").write_text(
        "github_pat_" + "a" * 24 + "\n", encoding="utf-8"
    )

    report = MODULE.validate(tmp_path)
    codes = {finding["code"] for finding in report["findings"]}
    assert "credential-token" in codes
    assert "expected-output-absolute-path" in codes


def test_distribution_requires_both_public_expected_output_snapshots(tmp_path):
    report = MODULE.validate(tmp_path)
    missing = {
        finding["path"]
        for finding in report["findings"]
        if finding["code"] == "missing-root-file"
    }
    for case in ("pbmc3k", "visium-mouse-brain"):
        assert f"examples/{case}/expected-output/README.md" in missing
        assert (
            f"examples/{case}/expected-output/manifest/verification-summary.json"
            in missing
        )


def test_public_tree_has_no_private_corpus_or_binary_inputs():
    report = MODULE.validate(ROOT)
    prohibited = {
        "symlink",
        "private-corpus",
        "oversized-public-file",
        "binary-data",
        "non-utf8-text",
        "private-home-locator",
        "credential-token",
        "authorization-credential",
        "private-key-material",
        "expected-output-absolute-path",
        "unsanitized-corpus-source-config",
        "invalid-original-code-license",
        "ambiguous-license-boundary",
        "missing-third-party-data-attribution",
    }
    findings = [
        finding for finding in report["findings"] if finding["code"] in prohibited
    ]
    assert findings == []


def test_distribution_rejects_ambiguous_license_and_data_boundaries(tmp_path):
    (tmp_path / "LICENSE").write_text("All rights reserved.\n", encoding="utf-8")
    (tmp_path / "NOTICE").write_text("Everything is MIT.\n", encoding="utf-8")
    (tmp_path / "THIRD_PARTY_DATA.md").write_text(
        "PBMC3K and Mouse Brain data.\n", encoding="utf-8"
    )

    report = MODULE.validate(tmp_path)
    codes = {finding["code"] for finding in report["findings"]}
    assert "invalid-original-code-license" in codes
    assert "ambiguous-license-boundary" in codes
    assert "missing-third-party-data-attribution" in codes


def test_distribution_rejects_an_unreviewed_visualization_remote(tmp_path):
    lock = {
        "schema_version": "1.0.0",
        "dependencies": {
            "visualization-2026718-v1": {
                "repository": "https://example.org/unreviewed.git",
                "commit": "a" * 40,
                "subdirectory": MODULE.EXPECTED_VISUALIZATION_SUBDIRECTORY,
                "distribution_profile_file": "public-install-profile.json",
                "excluded_paths": [
                    "assets/previews-curated",
                    "assets/scheme-candidates",
                    "assets/source_archive",
                    "references/catalog.jsonl",
                ],
                "content_sha256": "b" * 64,
                "original_code_license": "MIT",
                "license_file": "LICENSE",
                "third_party_notice_file": "NOTICE.md",
                "rights_status": "mixed-original-and-third-party-not-relicensed",
            }
        },
    }
    (tmp_path / "skills.lock.json").write_text(json.dumps(lock), encoding="utf-8")
    report = MODULE.validate(tmp_path)
    assert "invalid-skills-lock" in {item["code"] for item in report["findings"]}


def test_distribution_requires_visualization_notice_declaration(tmp_path):
    lock = {
        "schema_version": "1.0.0",
        "dependencies": {
            "visualization-2026718-v1": {
                "repository": MODULE.EXPECTED_VISUALIZATION_REPOSITORY,
                "commit": "a" * 40,
                "subdirectory": MODULE.EXPECTED_VISUALIZATION_SUBDIRECTORY,
                "distribution_profile_file": "public-install-profile.json",
                "excluded_paths": [
                    "assets/previews-curated",
                    "assets/scheme-candidates",
                    "assets/source_archive",
                    "references/catalog.jsonl",
                ],
                "content_sha256": "b" * 64,
                "original_code_license": "MIT",
                "license_file": "LICENSE",
                "rights_status": "mixed-original-and-third-party-not-relicensed",
            }
        },
    }
    (tmp_path / "skills.lock.json").write_text(json.dumps(lock), encoding="utf-8")
    report = MODULE.validate(tmp_path)
    findings = [item for item in report["findings"] if item["code"] == "invalid-skills-lock"]
    assert len(findings) == 1
    assert "NOTICE.md" in findings[0]["path"]


def test_distribution_rejects_ambiguous_legacy_visualization_license(tmp_path):
    lock = {
        "schema_version": "1.0.0",
        "dependencies": {
            "visualization-2026718-v1": {
                "repository": MODULE.EXPECTED_VISUALIZATION_REPOSITORY,
                "commit": "a" * 40,
                "subdirectory": MODULE.EXPECTED_VISUALIZATION_SUBDIRECTORY,
                "distribution_profile_file": "public-install-profile.json",
                "excluded_paths": [
                    "assets/previews-curated",
                    "assets/scheme-candidates",
                    "assets/source_archive",
                    "references/catalog.jsonl",
                ],
                "content_sha256": "b" * 64,
                "original_code_license": "MIT",
                "license_file": "LICENSE",
                "third_party_notice_file": "NOTICE.md",
                "rights_status": "mixed-original-and-third-party-not-relicensed",
                "license": "MIT",
            }
        },
    }
    (tmp_path / "skills.lock.json").write_text(json.dumps(lock), encoding="utf-8")
    report = MODULE.validate(tmp_path)
    findings = [item for item in report["findings"] if item["code"] == "invalid-skills-lock"]
    assert len(findings) == 1
    assert "ambiguous legacy rights fields" in findings[0]["path"]
