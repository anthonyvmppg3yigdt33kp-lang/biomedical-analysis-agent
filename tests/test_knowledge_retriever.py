import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from knowledge_retriever import KnowledgeIndex, RetrievalRequest, retrieve  # noqa: E402


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in records),
        encoding="utf-8",
    )


def bundle(
    bundle_id: str,
    title: str,
    description: str,
    package: str,
    maturity: str,
    *,
    installer: bool = False,
) -> dict:
    installer_calls = ["BiocManager::install"] if installer else []
    issues = [{"code": "installer_calls_present", "severity": "info"}] if installer else []
    return {
        "bundle_id": bundle_id,
        "title": title,
        "source_root_id": "fixture-root",
        "source_root_name": "fixture",
        "maturity": maturity,
        "private_source_directory": r"D:\private\must-not-leak",
        "flow_integrity": {"reconstructable_from_sources": True},
        "issues": issues,
        "metadata": {"author": "fixture", "title": title},
        "preprocessing_match": {
            "record": {
                "内容要点": description,
                "数据类型": description,
                "能力标签": description,
                "代码资产完整度": "多脚本",
            }
        },
        "package_index": {"packages": [package], "qualified_functions": [[package, "run"]]},
        "ordered_code_files": [
            {
                "relative_to_bundle": "code/a.R",
                "source_locator": r"D:\private\a.R",
                "static_facts": {"installer_calls": installer_calls},
            },
            {"relative_to_bundle": "code/b.R", "static_facts": {"installer_calls": []}},
            {"relative_to_bundle": "code/c.R", "static_facts": {"installer_calls": []}},
        ],
        "article": {
            "fenced_code_blocks": [
                {
                    "text": "BiocManager::install('SecretPkg')\nsecret_raw_code <- TRUE",
                    "static_facts": {"installer_calls": installer_calls},
                }
            ]
        },
        "images": [
            {
                "plot_hints": [description],
                "link_confidence": "explicit_article_reference",
                "native_review_status": "reviewed" if maturity == "native-reviewed" else "not_reviewed",
                "source_locator": r"D:\private\figure.png",
            }
        ],
    }


class KnowledgeRetrieverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.index_dir = Path(self.temp.name) / "index"
        self.index_dir.mkdir()
        self.bundles = [
            bundle(
                "flow-raw", "Seurat donor pseudobulk volcano",
                "单细胞 donor-aware pseudobulk 差异分析 火山图", "Seurat", "raw-extracted",
                installer=True,
            ),
            bundle(
                "flow-exact", "Scanpy UMAP workflow",
                "single-cell Scanpy UMAP cell annotation", "scanpy", "fixture-verified",
            ),
            bundle(
                "flow-compatible", "Giotto spatial domains",
                "空间转录组 Giotto spatial domain tissue overlay", "Giotto", "fixture-verified",
            ),
            bundle(
                "flow-alternative", "DESeq2 count model",
                "bulk RNA-seq DESeq2 differential expression volcano", "DESeq2", "data-verified",
            ),
        ]
        methods = []
        packages = []
        variants = []
        for item, equivalence in zip(
            self.bundles,
            ("exact", "exact", "compatible", "alternative_method"),
        ):
            maturity = item["maturity"]
            bundle_id = item["bundle_id"]
            package = item["package_index"]["packages"][0]
            methods.append({
                "method_card_id": f"method-{bundle_id}",
                "title": item["title"],
                "category": item["preprocessing_match"]["record"]["数据类型"],
                "data_types": [item["preprocessing_match"]["record"]["数据类型"]],
                "method_sequence": [package, "analysis"],
                "research_question_hints": [item["preprocessing_match"]["record"]["内容要点"]],
                "maturity": maturity,
                "analysis_unit": "donor" if bundle_id == "flow-raw" else "sample",
                "review_status": "reviewed" if maturity != "raw-extracted" else "not_manually_reviewed",
                "required_validation": [] if maturity != "raw-extracted" else ["fixture_execution"],
                "source_bundle_ids": [bundle_id],
            })
            packages.append({
                "package_card_id": f"package-{bundle_id}",
                "package": package,
                "language": "r" if package != "scanpy" else "python",
                "capability_hints": [item["preprocessing_match"]["record"]["能力标签"]],
                "functions": ["run", "install"],
                "maturity": maturity,
                "source_bundle_ids": [bundle_id],
            })
            variants.append({
                "variant_id": f"variant-{bundle_id}",
                "source_bundle_id": bundle_id,
                "source_plot_label": item["title"],
                "equivalence": equivalence,
                "maturity": maturity,
            })
        write_jsonl(self.index_dir / "source-flow-bundles.jsonl", self.bundles)
        write_jsonl(self.index_dir / "method-cards.jsonl", methods)
        write_jsonl(self.index_dir / "package-cards.jsonl", packages)
        (self.index_dir / "capability-modules.json").write_text(
            json.dumps({
                "schema_version": "1.0",
                "modules": [{
                    "capability_module_id": "capability-fixture",
                    "capability": "workflow figure variants",
                    "semantic_key": "fixture",
                    "variants": variants,
                }],
            }, ensure_ascii=False),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_request(self, **kwargs) -> dict:
        index = KnowledgeIndex(self.index_dir)
        index.load()
        return retrieve(index, RetrievalRequest.from_mapping(kwargs), kwargs.get("limit", 5))

    def write_review_overlay(self, records: list[dict], *, corrupt_hash: bool = False) -> None:
        review_dir = self.index_dir / "manual-review"
        review_dir.mkdir()
        batch_path = review_dir / "gold-review-batch-fixture.jsonl"
        write_jsonl(batch_path, records)
        digest = hashlib.sha256(batch_path.read_bytes()).hexdigest()
        if corrupt_hash:
            digest = "0" * 64
        validation = {
            "schema_version": "1.0",
            "ok": True,
            "errors": [],
            "reviews": len(records),
            "unique_bundle_ids": len({item["bundle_id"] for item in records}),
            "batch_sha256": {batch_path.name: digest},
        }
        (review_dir / "gold-review-validation.json").write_text(
            json.dumps(validation, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )

    def write_deep_review_overlay(
        self, bundle_id: str, *, maturity: str = "parse-verified", corrupt_hash: bool = False
    ) -> None:
        review_dir = self.index_dir / "manual-review"
        review_dir.mkdir(exist_ok=True)
        record_id = f"prep-{bundle_id}"
        record_sha = hashlib.sha256(record_id.encode("utf-8")).hexdigest()
        relations = [{
            "bundle_id": bundle_id,
            "relation_type": "article_source",
            "sha256": "7" * 64,
            "verified": True,
        }]
        relation_sha = hashlib.sha256(
            json.dumps(relations, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        write_jsonl(self.index_dir / "preprocessing-records.jsonl", [{
            "preprocess_record_id": record_id,
            "record_sha256": record_sha,
            "record": {"记录标题": "deep review fixture"},
        }])
        write_jsonl(self.index_dir / "preprocessing-crosswalk.jsonl", [{
            "preprocess_record_id": record_id,
            "record_sha256": record_sha,
            "relations": relations,
        }])
        review_path = review_dir / "high-value-review-batch-999-a.jsonl"
        write_jsonl(review_path, [{
            "review_id": "deep-review-fixture",
            "review_state": "COMPLETE",
            "preprocess_record_id": record_id,
            "maturity": maturity,
            "decision": {
                "classification": "workflow_candidate_with_gates",
                "reason": r"Review reason D:\private\must-not-leak\article.md",
                "automatic_execution_allowed": False,
            },
            "scientific_review": {
                "claim_ceiling": "只支持经深审的方法学候选，不代表运行复现。",
                "assumptions": ["RNA and ATAC donor labels are aligned"],
                "scientific_risks": [
                    "Cell-level testing creates donor pseudoreplication and inflated significance",
                    "Batch-confounded modalities can mimic regulatory links",
                ],
                "alternatives": ["RNA-only pySCENIC is not equivalent to joint chromatin evidence"],
                "validation_required": ["Donor-held-out eRegulon validation"],
            },
            "figure_context_review": {
                "status": "native_sample_reviewed",
                "selection_rationale": "Workflow diagram chosen for method ordering",
                "unreviewed_figure_count": 1,
                "figures": [{
                    "figure_role": "SCENIC+ eRegulon workflow overview",
                    "reproduction_class": "semantic_candidate",
                    "visible": ["joint RNA and ATAC stages"],
                    "cannot_assert": ["causal TF regulation"],
                    "code_binding": {
                        "status": "inferred_nearby_code",
                        "source_locator": r"D:\private\figure-binding.txt",
                    },
                }],
            },
            "source_evidence": {
                "record_sha256": record_sha,
                "source_relation_sha256": relation_sha,
                "bundles": [{"bundle_id": bundle_id}],
            },
            "research_context": {
                "research_question": "Joint scRNA-seq and scATAC-seq eRegulon inference",
                "input_modality": "non-paired scRNA-seq and scATAC-seq",
                "cohort_or_sample_structure": "multiple independent donors",
                "descriptive_unit": "cell and regulatory edge",
                "inferential_unit": "donor",
                "raw_private_text": "DEEP_REVIEW_RAW_TEXT_MUST_NOT_LEAK",
                "source_locator": r"D:\private\research-context.md",
            },
            "combination_logic": "pycisTopic accessibility topics feed SCENIC+ TF-region-gene inference",
            "method_sequence": [{
                "order": 1,
                "method": "Build accessibility topics and infer eRegulons",
                "rationale": "Integrate motif, region accessibility, and gene expression evidence",
                "inputs": ["scRNA-seq", "scATAC-seq"],
                "outputs": ["eRegulon activity"],
                "raw_code": "RAW_REVIEW_CODE_MUST_NOT_LEAK",
            }],
            "package_usage": [{
                "package": "SCENIC+",
                "role": "Joint eRegulon inference using resources at /mnt/private/motifs",
                "functions": ["run_scenicplus", "prepare_GEX_ACC"],
                "evidence": "ARTICLE_TEXT_MUST_NOT_LEAK",
            }, {
                "package": "pycisTopic",
                "role": "Chromatin accessibility topic modeling",
                "functions": ["create_cistopic_object"],
            }],
            "article_text": "FULL_ARTICLE_TEXT_MUST_NOT_LEAK",
        }])
        digest = hashlib.sha256(review_path.read_bytes()).hexdigest()
        if corrupt_hash:
            digest = "0" * 64
        (review_dir / "high-value-review-batch-999-validation.json").write_text(
            json.dumps({
                "schema_version": "1.0",
                "ok": True,
                "errors": [],
                "complete_records": 1,
                "unique_records": 1,
                "review_files": {str(review_path.resolve()): digest},
            }, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

    @staticmethod
    def review_record(bundle_id: str, maturity: str = "normalized") -> dict:
        return {
            "bundle_id": bundle_id,
            "title": "review fixture",
            "decision": "candidate_with_scientific_gate",
            "maturity": maturity,
            "claim_ceiling": "只支持描述性候选，不支持因果或已复现声明。",
            "code_figure_consistency": {"status": "viewed_unresolved"},
            "review_evidence": {"article_locator": r"D:\private\must-not-leak\article.md"},
            "research_question": "REVIEW_RAW_TEXT_MUST_NOT_LEAK",
        }

    def test_missing_private_index_degrades_safely(self) -> None:
        index = KnowledgeIndex(Path(self.temp.name) / "absent")
        index.load()
        result = retrieve(index, RetrievalRequest.from_mapping({"query": "Seurat"}), 5)
        self.assertEqual(result["index"]["status"], "unavailable")
        self.assertEqual(result["candidate_count"], 0)
        self.assertIn("no_corpus_claims", result["decision"])

    def test_missing_review_overlay_warns_and_is_ignored(self) -> None:
        index = KnowledgeIndex(self.index_dir)
        index.load()
        self.assertEqual(index.status()["manual_review_overlay"]["status"], "missing_ignored")
        self.assertIn("manual_review_overlay_missing", index.status()["warnings"])

    def test_raw_registry_is_an_explicit_fallback_layer(self) -> None:
        index = KnowledgeIndex(self.index_dir)
        index.load()
        self.assertEqual(index.status()["layer"], "raw-fallback")

    def test_schema_validated_normalized_registry_is_preferred(self) -> None:
        root = Path(self.temp.name) / "layered-index"
        normalized = root / "normalized-registry"
        normalized.mkdir(parents=True)
        (normalized / "registry-manifest.json").write_text(
            json.dumps({"schema_validated": True, "schema_version": "1.0"}), encoding="utf-8"
        )
        normalized_bundle = {
            "bundle_id": "flow-normalized",
            "title": "Normalized Scanpy UMAP",
            "maturity": "raw-extracted",
            "ordered_code": [{"order": 1, "language": "python", "sha256": "a" * 64}],
            "images": [],
            "packages": ["scanpy"],
            "gaps": [],
            "provenance": [{"article_id": "flow-normalized", "source_path": r"D:\private\article.md"}],
        }
        write_jsonl(normalized / "source-flow-bundles.jsonl", [normalized_bundle])
        write_jsonl(normalized / "method-cards.jsonl", [
            {
                "method_id": "method-normalized", "question": "single-cell Scanpy UMAP",
                "applicability": ["scRNA-seq"], "combination_logic": ["scanpy", "UMAP"],
                "validation": ["fixture_execution"], "statistical_unit": "cell",
                "maturity": "raw-extracted", "provenance": [{"article_id": "flow-normalized"}],
            },
            {
                "method_id": "method-no-code", "question": "文献方法学 cohort confounding 研究设计",
                "applicability": ["临床队列"], "combination_logic": ["cohort", "confounding"],
                "validation": ["primary_method_review"], "statistical_unit": "patient",
                "maturity": "raw-extracted", "provenance": [{"article_id": "flow-no-code"}],
            },
        ])
        write_jsonl(normalized / "package-cards.jsonl", [{
            "package_id": "package-normalized", "name": "scanpy", "language": "python",
            "complete_workflow": ["UMAP"], "functions": [], "maturity": "raw-extracted",
            "provenance": [{"article_id": "flow-normalized"}],
        }])
        write_jsonl(normalized / "figure-cards.jsonl", [])
        write_jsonl(normalized / "variant-sets.jsonl", [{
            "variant_set_id": "variant-set-normalized", "capability": "UMAP", "maturity": "raw-extracted",
            "variants": [{
                "variant_id": "variant-normalized", "implementation_ref": "flow-normalized",
                "classification": "exact", "differences": [],
            }],
        }])
        write_jsonl(root / "source-flow-bundles.jsonl", [
            bundle("flow-raw-ignored", "Raw fallback must be ignored", "scanpy", "scanpy", "data-verified")
        ])
        index = KnowledgeIndex(root)
        index.load()
        result = retrieve(index, RetrievalRequest.from_mapping({"query": "Scanpy UMAP"}), 5)
        self.assertEqual(result["index"]["layer"], "normalized-registry")
        self.assertEqual(result["candidates"][0]["candidate_id"], "flow-normalized")
        self.assertNotIn("flow-raw-ignored", [item["candidate_id"] for item in result["candidates"]])

        method_only = retrieve(
            index,
            RetrievalRequest.from_mapping({"query": "文献方法学 cohort confounding", "domain": "literature-methodology"}),
            3,
        )["candidates"][0]
        self.assertEqual(method_only["candidate_id"], "knowledge-ref-flow-no-code")
        self.assertEqual(method_only["kind"], "knowledge_object_without_source_flow")
        self.assertFalse(method_only["materialization"]["eligible"])
        self.assertFalse(method_only["execution"]["eligible"])
        self.assertEqual(method_only["evidence"]["ordered_code_file_count"], 0)

    def test_joint_retrieval_links_all_object_types(self) -> None:
        result = self.run_request(query="donor-aware 单细胞差异分析", domain="single-cell", package="Seurat")
        top = result["candidates"][0]
        self.assertEqual(top["candidate_id"], "flow-raw")
        self.assertEqual(top["evidence"]["method_card_ids"], ["method-flow-raw"])
        self.assertEqual(top["evidence"]["package_card_ids"], ["package-flow-raw"])
        self.assertEqual(top["evidence"]["capability_module_ids"], ["capability-fixture"])

    def test_raw_extracted_is_never_executable(self) -> None:
        result = self.run_request(query="Seurat pseudobulk", package="Seurat")
        top = result["candidates"][0]
        self.assertEqual(top["maturity"]["effective"], "raw-extracted")
        self.assertFalse(top["execution"]["eligible"])
        self.assertTrue(top["materialization"]["eligible"])

    def test_fixture_verified_without_blockers_can_pass_preliminary_gate(self) -> None:
        result = self.run_request(query="Scanpy UMAP", package="scanpy")
        top = result["candidates"][0]
        self.assertEqual(top["candidate_id"], "flow-exact")
        self.assertTrue(top["execution"]["eligible"])
        self.assertTrue(top["execution"]["requires_explicit_authorization"])

    def test_installer_code_never_enters_output_or_recipe(self) -> None:
        result = self.run_request(query="Seurat donor pseudobulk", package="Seurat")
        top = result["candidates"][0]
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertTrue(top["recipe_safety"]["installer_calls_detected"])
        self.assertFalse(top["recipe_safety"]["eligible"])
        self.assertNotIn("BiocManager::install", rendered)
        self.assertNotIn("secret_raw_code", rendered)
        self.assertNotIn(r"D:\private", rendered)

    def test_exact_variant_auto_trial_requires_execution_gate(self) -> None:
        result = self.run_request(query="Scanpy UMAP", package="scanpy")
        decision = result["candidates"][0]["variant_decisions"][0]
        self.assertEqual(decision["equivalence"], "exact")
        self.assertTrue(decision["policy_allows_auto_trial"])
        self.assertTrue(decision["currently_auto_trialable"])

    def test_compatible_variant_requires_explanation(self) -> None:
        result = self.run_request(query="空间域", domain="空间转录组", package="Giotto")
        decision = result["candidates"][0]["variant_decisions"][0]
        self.assertEqual(decision["equivalence"], "compatible")
        self.assertFalse(decision["policy_allows_auto_trial"])
        self.assertIn("explain_differences", decision["boundary"])

    def test_alternative_method_never_auto_substitutes(self) -> None:
        result = self.run_request(query="DESeq2 count model", domain="bulk RNA-seq", package="DESeq2")
        decision = result["candidates"][0]["variant_decisions"][0]
        self.assertEqual(decision["equivalence"], "alternative_method")
        self.assertFalse(decision["currently_auto_trialable"])
        self.assertIn("no_auto_substitution", decision["boundary"])

    def test_chinese_domain_is_normalized_and_matched(self) -> None:
        result = self.run_request(query="组织切片空间叠加图", domain="空间转录组")
        self.assertEqual(result["request"]["domain"], "spatial")
        self.assertEqual(result["candidates"][0]["candidate_id"], "flow-compatible")

    def test_same_query_is_byte_deterministic(self) -> None:
        first = self.run_request(query="Scanpy UMAP", domain="single-cell", package="scanpy")
        second = self.run_request(query="Scanpy UMAP", domain="single-cell", package="scanpy")
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )

    def test_exact_ordered_code_duplicates_fold_without_score_multiplication(self) -> None:
        first = bundle(
            "flow-duplicate-raw", "Exact duplicate sentinel workflow",
            "exact duplicate sentinel workflow", "scanpy", "raw-extracted",
        )
        second = bundle(
            "flow-duplicate-verified", "Exact duplicate sentinel workflow",
            "exact duplicate sentinel workflow", "scanpy", "data-verified",
        )
        for record in (first, second):
            for index, code_file in enumerate(record["ordered_code_files"], 1):
                code_file["sha256"] = hashlib.sha256(f"shared-code-{index}".encode("utf-8")).hexdigest()
                code_file["normalized_language"] = "python"
        write_jsonl(self.index_dir / "source-flow-bundles.jsonl", [*self.bundles, first, second])

        index = KnowledgeIndex(self.index_dir)
        index.load()
        result = retrieve(
            index,
            RetrievalRequest.from_mapping({"query": "Exact duplicate sentinel workflow"}),
            10,
        )
        duplicates = [
            item for item in result["candidates"]
            if item["candidate_id"] in {"flow-duplicate-raw", "flow-duplicate-verified"}
        ]
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0]["candidate_id"], "flow-duplicate-verified")
        self.assertEqual(duplicates[0]["duplicate_provenance"], {
            "dedupe_basis": "ordered_code_sequence_sha256",
            "source_count": 2,
            "source_candidate_ids": ["flow-duplicate-raw", "flow-duplicate-verified"],
            "reviewed_source_count": 0,
            "evidence_multiplier_applied": False,
        })
        self.assertEqual(result["deduplication"]["exact_duplicate_groups_folded"], 1)
        self.assertEqual(result["deduplication"]["folded_source_candidates"], 1)
        component = next(
            item for item in duplicates[0]["score_explanation"]
            if item["component"] == "exact_duplicate_folding"
        )
        self.assertEqual(component["points"], 0.0)

    def test_exact_duplicate_display_prefers_explicit_canonical_review(self) -> None:
        canonical = bundle(
            "flow-display-canonical", "Canonical display sentinel workflow",
            "canonical display sentinel workflow", "scanpy", "raw-extracted",
        )
        duplicate = bundle(
            "flow-display-duplicate", "Canonical display sentinel workflow",
            "canonical display sentinel workflow", "scanpy", "data-verified",
        )
        for record in (canonical, duplicate):
            for index, code_file in enumerate(record["ordered_code_files"], 1):
                code_file["sha256"] = hashlib.sha256(
                    f"canonical-display-shared-{index}".encode("utf-8")
                ).hexdigest()
                code_file["normalized_language"] = "python"
        write_jsonl(self.index_dir / "source-flow-bundles.jsonl", [*self.bundles, canonical, duplicate])
        canonical_review = self.review_record("flow-display-canonical")
        canonical_review["decision"] = "canonical"
        duplicate_review = self.review_record("flow-display-duplicate")
        duplicate_review["decision"] = "duplicate_reuse"
        self.write_review_overlay([canonical_review, duplicate_review])

        index = KnowledgeIndex(self.index_dir)
        index.load()
        result = retrieve(
            index,
            RetrievalRequest.from_mapping({"query": "Canonical display sentinel workflow"}),
            10,
        )
        folded = [
            item for item in result["candidates"]
            if (item.get("duplicate_provenance") or {}).get("source_count") == 2
            and "flow-display-canonical" in item["duplicate_provenance"]["source_candidate_ids"]
        ]
        self.assertEqual(len(folded), 1)
        representative = folded[0]
        self.assertEqual(representative["candidate_id"], "flow-display-canonical")
        self.assertEqual(representative["manual_review"]["decision"], "canonical")
        # The representative keeps its own lower maturity and closed execution
        # gate; it does not borrow either field from the verified duplicate.
        self.assertEqual(representative["maturity"]["effective"], "raw-extracted")
        self.assertFalse(representative["execution"]["eligible"])
        component = next(
            item for item in representative["score_explanation"]
            if item["component"] == "exact_duplicate_folding"
        )
        self.assertEqual(component["points"], 0.0)

    def test_exact_duplicate_display_has_stable_fallback_when_all_are_reuse(self) -> None:
        first = bundle(
            "flow-display-fallback-a", "Duplicate fallback sentinel workflow",
            "duplicate fallback sentinel workflow", "scanpy", "raw-extracted",
        )
        second = bundle(
            "flow-display-fallback-z", "Duplicate fallback sentinel workflow",
            "duplicate fallback sentinel workflow", "scanpy", "raw-extracted",
        )
        for record in (first, second):
            for index, code_file in enumerate(record["ordered_code_files"], 1):
                code_file["sha256"] = hashlib.sha256(
                    f"duplicate-fallback-shared-{index}".encode("utf-8")
                ).hexdigest()
                code_file["normalized_language"] = "python"
        write_jsonl(self.index_dir / "source-flow-bundles.jsonl", [*self.bundles, first, second])
        reviews = [
            self.review_record("flow-display-fallback-a"),
            self.review_record("flow-display-fallback-z"),
        ]
        for review in reviews:
            review["decision"] = "duplicate_reuse"
        self.write_review_overlay(reviews)

        index = KnowledgeIndex(self.index_dir)
        index.load()
        result = retrieve(
            index,
            RetrievalRequest.from_mapping({"query": "Duplicate fallback sentinel workflow"}),
            10,
        )
        folded = [
            item for item in result["candidates"]
            if (item.get("duplicate_provenance") or {}).get("source_count") == 2
            and "flow-display-fallback-a" in item["duplicate_provenance"]["source_candidate_ids"]
        ]
        self.assertEqual(len(folded), 1)
        self.assertEqual(folded[0]["candidate_id"], "flow-display-fallback-a")
        self.assertEqual(folded[0]["manual_review"]["decision"], "duplicate_reuse")

    def test_live_reviewed_exact_group_displays_non_reuse_source_when_available(self) -> None:
        private_index = SKILL_ROOT / "assets" / "private-corpus-index"
        if not (private_index / "source-flow-bundles.jsonl").exists():
            self.skipTest("private corpus index is not installed")
        index = KnowledgeIndex(private_index)
        index.load()
        if index.status()["manual_review_overlay"]["status"] != "available":
            self.skipTest("authenticated private review overlay is not available")
        result = retrieve(
            index,
            RetrievalRequest.from_mapping({
                "query": "Augur affected cell type prioritization",
                "domain": "single-cell",
                "package": "Augur",
            }),
            20,
        )
        folded = [
            item for item in result["candidates"]
            if (item.get("duplicate_provenance") or {}).get("source_count") == 4
            and "augur" in str(item.get("title") or "").casefold()
        ]
        self.assertEqual(len(folded), 1)
        decision = str((folded[0].get("manual_review") or {}).get("decision") or "")
        self.assertNotIn("duplicate_reuse", {item.strip() for item in decision.split("|")})
        self.assertEqual(
            next(
                item["points"] for item in folded[0]["score_explanation"]
                if item["component"] == "exact_duplicate_folding"
            ),
            0.0,
        )

    def test_live_rogue_random_pseudo_samples_are_quarantined_as_negative_control(self) -> None:
        private_index = SKILL_ROOT / "assets" / "private-corpus-index"
        if not (private_index / "source-flow-bundles.jsonl").exists():
            self.skipTest("private corpus index is not installed")
        index = KnowledgeIndex(private_index)
        index.load()
        if index.status()["manual_review_overlay"]["status"] != "available":
            self.skipTest("authenticated private review overlay is not available")
        result = retrieve(
            index,
            RetrievalRequest.from_mapping({
                "query": "ROGUE random A B C D pseudo-samples pseudoreplication pancreatic purity",
                "domain": "single-cell",
                "package": "ROGUE",
                "mode": "explain",
            }),
            3,
        )
        candidate = next(
            item for item in result["candidates"]
            if item["candidate_id"] == "flow-5bc6b8a1bc6fbcd4de4a"
        )
        self.assertEqual(candidate["rank"], 1)
        self.assertEqual(
            candidate["manual_review"]["decision"],
            "quarantine_as_negative_test_retain_plot_only",
        )
        self.assertFalse(candidate["manual_review"]["automatic_execution_allowed"])
        self.assertFalse(candidate["execution"]["eligible"])
        self.assertIn("fixture_or_data_verification", candidate["execution"]["reason"])
        self.assertTrue(candidate["recipe_safety"]["installer_calls_detected"])
        self.assertFalse(candidate["recipe_safety"]["raw_installation_code_emitted"])

    def test_valid_manual_review_deterministically_boosts_candidate(self) -> None:
        request = RetrievalRequest.from_mapping({"query": "workflow figure variants"})
        baseline_index = KnowledgeIndex(self.index_dir)
        baseline_index.load()
        baseline = retrieve(baseline_index, request, 10)
        baseline_score = next(
            item["score"] for item in baseline["candidates"] if item["candidate_id"] == "flow-compatible"
        )
        self.write_review_overlay([self.review_record("flow-compatible")])
        reviewed_index = KnowledgeIndex(self.index_dir)
        reviewed_index.load()
        reviewed = retrieve(reviewed_index, request, 10)
        candidate = next(
            item for item in reviewed["candidates"] if item["candidate_id"] == "flow-compatible"
        )
        self.assertGreater(candidate["score"], baseline_score)
        self.assertEqual(reviewed["index"]["manual_review_overlay"], {"status": "available", "record_count": 1})
        self.assertEqual(candidate["manual_review"]["decision"], "candidate_with_scientific_gate")
        self.assertEqual(candidate["manual_review"]["maturity"], "normalized")
        self.assertTrue(candidate["manual_review"]["code_figure_unresolved"])
        self.assertIn("只支持描述性候选", candidate["manual_review"]["claim_ceiling"])
        components = {item["component"]: item for item in candidate["score_explanation"]}
        self.assertEqual(components["manual_gold_review"]["points"], 8.0)
        rendered = json.dumps(reviewed, ensure_ascii=False)
        self.assertNotIn(r"D:\private", rendered)
        self.assertNotIn("REVIEW_RAW_TEXT_MUST_NOT_LEAK", rendered)

    def test_invalid_manual_review_overlay_warns_and_is_ignored(self) -> None:
        self.write_review_overlay([self.review_record("flow-compatible")], corrupt_hash=True)
        index = KnowledgeIndex(self.index_dir)
        index.load()
        result = retrieve(index, RetrievalRequest.from_mapping({"query": "Giotto spatial domains"}), 5)
        self.assertEqual(result["index"]["manual_review_overlay"]["status"], "invalid_ignored")
        self.assertTrue(any(item.startswith("manual_review_overlay_invalid") for item in result["index"]["warnings"]))
        self.assertTrue(all(item["manual_review"] is None for item in result["candidates"]))

    def test_receipt_authenticated_deep_review_is_retrieved_but_never_executes(self) -> None:
        request = RetrievalRequest.from_mapping({"query": "workflow figure variants"})
        baseline_index = KnowledgeIndex(self.index_dir)
        baseline_index.load()
        baseline = retrieve(baseline_index, request, 10)
        baseline_score = next(
            item["score"] for item in baseline["candidates"] if item["candidate_id"] == "flow-compatible"
        )
        self.write_deep_review_overlay("flow-compatible")
        index = KnowledgeIndex(self.index_dir)
        index.load()
        result = retrieve(index, request, 10)
        candidate = next(
            item for item in result["candidates"] if item["candidate_id"] == "flow-compatible"
        )
        self.assertGreater(candidate["score"], baseline_score)
        self.assertEqual(result["index"]["manual_review_overlay"], {"status": "available", "record_count": 1})
        self.assertEqual(candidate["manual_review"]["review_kind"], "deep_record_review")
        self.assertEqual(candidate["manual_review"]["source_review_count"], 1)
        self.assertFalse(candidate["manual_review"]["automatic_execution_allowed"])
        self.assertFalse(candidate["execution"]["eligible"])
        components = {item["component"]: item for item in candidate["score_explanation"]}
        self.assertEqual(components["manual_deep_review"]["points"], 12.0)
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(r"D:\private", rendered)
        self.assertNotIn("DEEP_REVIEW_RAW_TEXT_MUST_NOT_LEAK", rendered)

    def test_deep_review_semantics_make_reviewed_package_an_exact_top_match(self) -> None:
        self.write_deep_review_overlay("flow-compatible")
        index = KnowledgeIndex(self.index_dir)
        index.load()
        result = retrieve(
            index,
            RetrievalRequest.from_mapping({
                "query": "SCENIC+ joint scRNA scATAC eRegulon",
                "domain": "single-cell",
                "package": "SCENIC+",
            }),
            5,
        )
        candidate = result["candidates"][0]
        self.assertEqual(candidate["candidate_id"], "flow-compatible")
        package_component = next(
            item for item in candidate["score_explanation"]
            if item["component"] == "package_match"
        )
        self.assertEqual(package_component["points"], 20.0)
        self.assertEqual(package_component["reviewed_package_match"], ["scenic+"])
        self.assertIn("SCENIC+", candidate["manual_review"]["package_names"])
        semantics = candidate["manual_review"]["derived_semantics"]
        self.assertEqual(
            semantics["derivation"]["sanitization"],
            "whitelist_only_locator_redacted_bounded",
        )
        self.assertFalse(candidate["execution"]["eligible"])
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertIn("[private-path-redacted]", rendered)
        for forbidden in (
            r"D:\private", "/mnt/private", "DEEP_REVIEW_RAW_TEXT_MUST_NOT_LEAK",
            "RAW_REVIEW_CODE_MUST_NOT_LEAK", "ARTICLE_TEXT_MUST_NOT_LEAK",
            "FULL_ARTICLE_TEXT_MUST_NOT_LEAK",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_deep_review_scientific_risk_semantics_are_searchable(self) -> None:
        self.write_deep_review_overlay("flow-compatible")
        index = KnowledgeIndex(self.index_dir)
        index.load()
        result = retrieve(
            index,
            RetrievalRequest.from_mapping({
                "query": "donor pseudoreplication inflated significance",
                "domain": "single-cell",
            }),
            5,
        )
        candidate = result["candidates"][0]
        self.assertEqual(candidate["candidate_id"], "flow-compatible")
        self.assertIn("pseudoreplication", candidate["matched_terms"])
        risks = candidate["manual_review"]["derived_semantics"]["scientific_review"]["scientific_risks"]
        self.assertTrue(any("pseudoreplication" in item for item in risks))
        self.assertFalse(candidate["execution"]["eligible"])

    def test_public_index_without_private_overlay_has_no_derived_semantics(self) -> None:
        index = KnowledgeIndex(self.index_dir)
        index.load()
        result = retrieve(
            index,
            RetrievalRequest.from_mapping({
                "query": "SCENIC+ joint scRNA scATAC eRegulon",
                "package": "SCENIC+",
            }),
            5,
        )
        self.assertEqual(result["index"]["manual_review_overlay"]["status"], "missing_ignored")
        self.assertEqual(result["candidate_count"], 0)
        self.assertNotIn("derived_semantics", json.dumps(result, ensure_ascii=False))

    def test_tampered_deep_review_receipt_is_fail_closed(self) -> None:
        self.write_deep_review_overlay("flow-compatible", corrupt_hash=True)
        index = KnowledgeIndex(self.index_dir)
        index.load()
        result = retrieve(index, RetrievalRequest.from_mapping({"query": "Giotto spatial domains"}), 5)
        self.assertEqual(result["index"]["manual_review_overlay"]["status"], "invalid_ignored")
        self.assertTrue(any(item.startswith("manual_review_overlay_invalid") for item in result["index"]["warnings"]))
        self.assertTrue(all(item["manual_review"] is None for item in result["candidates"]))

    def test_parse_verified_manual_review_remains_non_executable(self) -> None:
        self.write_review_overlay([self.review_record("flow-exact", maturity="parse-verified")])
        index = KnowledgeIndex(self.index_dir)
        index.load()
        result = retrieve(
            index,
            RetrievalRequest.from_mapping({"query": "Scanpy UMAP", "package": "scanpy"}),
            5,
        )
        candidate = result["candidates"][0]
        self.assertEqual(candidate["candidate_id"], "flow-exact")
        self.assertEqual(candidate["maturity"]["effective"], "fixture-verified")
        self.assertEqual(candidate["manual_review"]["maturity"], "parse-verified")
        self.assertFalse(candidate["manual_review"]["fixture_or_data_verified"])
        self.assertFalse(candidate["execution"]["eligible"])
        self.assertFalse(candidate["recipe_safety"]["eligible"])
        self.assertEqual(
            candidate["execution"]["reason"],
            "manual_review_overlay_has_no_fixture_or_data_verification",
        )
        self.assertIn("manual_review_not_fixture_or_data_verified", candidate["gaps"])

    def test_limit_is_enforced_with_stable_ranks(self) -> None:
        index = KnowledgeIndex(self.index_dir)
        index.load()
        result = retrieve(index, RetrievalRequest.from_mapping({"domain": "single-cell"}), 1)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["candidates"][0]["rank"], 1)

    def test_cli_accepts_request_json_and_writes_machine_json(self) -> None:
        request_path = Path(self.temp.name) / "request.json"
        output_path = Path(self.temp.name) / "result.json"
        request_path.write_text(
            json.dumps({"query": "Scanpy UMAP", "domain": "single-cell", "package": "scanpy"}),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "knowledge_retriever.py"),
                "--request", str(request_path),
                "--index", str(self.index_dir),
                "--limit", "1",
                "--output", str(output_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["candidates"][0]["candidate_id"], "flow-exact")

    def test_orchestrator_request_aliases_are_accepted(self) -> None:
        request = RetrievalRequest.from_mapping({
            "question": "Scanpy cell annotation",
            "analysis_type": "UMAP",
            "modality": "single-cell",
            "preferred_package": "scanpy",
            "requested_outputs": ["UMAP figure", "tables"],
        })
        self.assertEqual(request.query, "Scanpy cell annotation UMAP")
        self.assertEqual(request.domain, "single-cell")
        self.assertEqual(request.package, "scanpy")
        self.assertEqual(request.figure_intent, "UMAP figure tables")


if __name__ == "__main__":
    unittest.main()
