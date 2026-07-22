import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "corpus_distiller.py"
SPEC = importlib.util.spec_from_file_location("corpus_distiller", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class CorpusDistillerTests(unittest.TestCase):
    def test_complete_flow_index_and_materialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source"
            article = source / "001_demo"
            (article / "code").mkdir(parents=True)
            (article / "images").mkdir()
            (article / "metadata.json").write_text(
                json.dumps({"title": "Demo", "code_blocks": 1, "images": 1}, ensure_ascii=False),
                encoding="utf-8",
            )
            (article / "article.md").write_text(
                "# Demo\n\n```r\nlibrary(Seurat)\nx <- 1\n```\n\n![result](images/result.png)\n",
                encoding="utf-8",
            )
            (article / "code" / "s02.py").write_text("import scanpy as sc\ny = 2\n", encoding="utf-8")
            (article / "code" / "s01.R").write_text("library(Seurat)\nx <- 1\n", encoding="utf-8")
            (article / "images" / "result.png").write_bytes(
                b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + (10).to_bytes(4, "big") + (20).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
            )
            records = base / "records.json"
            records.write_text(
                json.dumps([{
                    "来源文件夹": "source",
                    "记录标题": "Demo",
                    "路径": str(article),
                    "一级分类": "单细胞分析",
                    "代码资产完整度": "完整流程",
                    "蒸馏价值": "高",
                    "内容要点": "demo",
                    "数据类型": "scRNA-seq",
                    "_tags": ["QC", "UMAP"],
                    "_r": ["Seurat"],
                    "_py": ["scanpy"],
                    "_plots": ["UMAP"],
                }], ensure_ascii=False),
                encoding="utf-8",
            )
            config = base / "config.json"
            config.write_text(
                json.dumps({
                    "source_roots": [{"id": "source", "name": "source", "path": str(source), "mode": "read_only"}],
                    "preprocessing_records": str(records),
                    "distribution": {},
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            output = base / "index"
            manifest = MODULE.build_index(config, output)
            self.assertEqual(manifest["counts"]["source_flow_bundles"], 1)
            inventory = MODULE.read_jsonl(output / "file-inventory.jsonl")
            self.assertTrue(inventory)
            self.assertTrue(all(item["distribution_class"] == "private_local_only" for item in inventory))
            self.assertTrue(all(item["source_mode"] == "read_only" and item["publish_allowed"] is False for item in inventory))
            bundles = MODULE.read_jsonl(output / "source-flow-bundles.jsonl")
            bundle = bundles[0]
            self.assertTrue(bundle["preprocessing_match"]["record_id"].startswith("prep-"))
            self.assertEqual(bundle["preprocessing_match"]["method"], "exact_path")
            self.assertEqual([item["relative_to_bundle"] for item in bundle["ordered_code_files"]], ["code/s01.R", "code/s02.py"])
            self.assertEqual(bundle["images"][0]["width"], 10)
            self.assertIn("Seurat", bundle["package_index"]["packages"])
            self.assertTrue(bundle["flow_integrity"]["reconstructable_from_sources"])
            registry = MODULE.read_jsonl(output / "preprocessing-records.jsonl")
            crosswalk = MODULE.read_jsonl(output / "preprocessing-crosswalk.jsonl")
            self.assertEqual(len(registry), 1)
            self.assertEqual(crosswalk[0]["preprocess_record_id"], registry[0]["preprocess_record_id"])
            self.assertEqual(crosswalk[0]["status"], "exact_bundle_path")
            self.assertTrue(crosswalk[0]["relation_verified"])
            self.assertEqual(manifest["counts"]["high_value_relation_verified_records"], 1)
            rebuilt = MODULE.rebuild_crosswalk(config, output)
            self.assertTrue(rebuilt["ok"])
            self.assertEqual(rebuilt["high_value"]["records"], 1)
            result = MODULE.validate_index(output, verify_source_hashes=True)
            self.assertTrue(result["ok"], result)
            materialized = base / "materialized"
            receipt = MODULE.materialize_bundle(output, bundle["bundle_id"], materialized)
            self.assertEqual(receipt["bundle_id"], bundle["bundle_id"])
            self.assertTrue((materialized / "code" / "s01.R").exists())
            self.assertTrue((materialized / "article_fenced_blocks" / "block-0001.R").exists())

    def test_parent_project_does_not_duplicate_nested_articles(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "single-cell-projects"
            source.mkdir()
            (source / "top.R").write_text("x <- 1\n", encoding="utf-8")
            collection = source / "articles"
            article = collection / "001_nested"
            (article / "code").mkdir(parents=True)
            (article / "article.md").write_text("# nested\n\n```r\ny <- 2\n```\n", encoding="utf-8")
            (article / "metadata.json").write_text('{"title":"nested","code_blocks":1,"images":0}', encoding="utf-8")
            (article / "code" / "s01.R").write_text("y <- 2\n", encoding="utf-8")
            root = MODULE.SourceRoot("single-cell-projects", "单细胞代码", source, {})
            dirs = MODULE.discover_bundle_dirs(root)
            self.assertIn(source, dirs)
            self.assertIn(article, dirs)
            self.assertNotIn(collection, dirs)

    def test_gold_set_uses_unique_normalized_article_titles(self):
        bundles = []
        for domain in MODULE.DOMAIN_QUOTAS:
            for index in range(20):
                title = f"{domain}-article-{index}"
                for duplicate in range(2):
                    bundles.append(
                        {
                            "bundle_id": f"flow-{len(bundles):04d}",
                            "title": title,
                            "ordered_code_files": [{"relative_to_bundle": "code.R"}],
                            "images": [{"relative_to_bundle": "figure.png"}],
                            "article": {"fenced_code_blocks": []},
                            "preprocessing_match": {
                                "record": {
                                    "一级分类": domain,
                                    "蒸馏价值": "高",
                                    "代码资产完整度": "完整",
                                }
                            },
                        }
                    )
        gold = MODULE.select_gold(bundles)
        titles = [MODULE.clean_title(item["title"]) for item in gold["selected"]]
        self.assertEqual(len(gold["selected"]), 60)
        self.assertEqual(len(titles), len(set(titles)))

    def test_capability_variants_classify_identical_code_as_exact(self):
        base = {
            "article": {"fenced_code_blocks": []},
            "ordered_code_files": [{"sha256": "a" * 64}, {"sha256": "d" * 64}],
            "preprocessing_match": {"record": {"_plots": ["火山图"]}},
        }
        bundles = [
            dict(base, bundle_id="flow-a"),
            dict(base, bundle_id="flow-b"),
            {
                **base,
                "bundle_id": "flow-c",
                "ordered_code_files": [{"sha256": "b" * 64}],
            },
        ]
        module = MODULE.build_capability_modules(bundles)[0]
        classes = {item["source_bundle_id"]: item["equivalence"] for item in module["variants"]}
        self.assertEqual(classes["flow-a"], "exact")
        self.assertEqual(classes["flow-b"], "exact")
        self.assertIn("exact", classes.values())
        self.assertIn("compatible", classes.values())

    def test_preprocessing_record_identity_is_unique_and_path_normalized(self):
        root = MODULE.SourceRoot("single-cell", "单细胞代码", Path(r"D:\Demo"), {})
        base = {
            "来源文件夹": "单细胞代码",
            "清单文件": "all.md",
            "记录标题": "Demo",
            "路径": r"D:\Demo\Article",
        }
        equivalent = {**base, "路径": "d:/demo/article/"}
        relocated_root = MODULE.SourceRoot("single-cell", "单细胞代码", Path(r"E:\Corpus"), {})
        relocated = {**base, "路径": r"E:\Corpus\Article"}
        self.assertEqual(MODULE.preprocessing_record_id(base, root), MODULE.preprocessing_record_id(equivalent, root))
        self.assertEqual(MODULE.preprocessing_record_id(base, root), MODULE.preprocessing_record_id(relocated, relocated_root))
        with self.assertRaises(ValueError):
            MODULE.build_preprocessing_registry([base, equivalent], [root])

    def test_crosswalk_supports_collection_and_project_root_descriptor(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "single-cell"
            collection = source / "articles"
            child = collection / "001_demo"
            child.mkdir(parents=True)
            root = MODULE.SourceRoot("single-cell", "单细胞代码", source, {})
            bundles = [
                {
                    "bundle_id": "flow-root",
                    "source_root_name": "单细胞代码",
                    "private_source_directory": str(source),
                    "article": {},
                    "ordered_code_files": [],
                    "images": [],
                    "preprocessing_match": {},
                },
                {
                    "bundle_id": "flow-child",
                    "source_root_name": "单细胞代码",
                    "private_source_directory": str(child),
                    "article": {},
                    "ordered_code_files": [],
                    "images": [],
                    "preprocessing_match": {},
                },
            ]
            records = [
                {
                    "来源文件夹": "单细胞代码",
                    "清单文件": "all.md",
                    "记录标题": "collection",
                    "路径": str(collection),
                    "蒸馏价值": "高",
                },
                {
                    "来源文件夹": "单细胞代码",
                    "清单文件": "all.md",
                    "记录标题": "root scripts",
                    "路径": str(source) + "\\（顶层散文件）",
                    "蒸馏价值": "高",
                },
            ]
            _, crosswalk, report = MODULE.build_preprocessing_crosswalk(records, bundles, [root], [])
            self.assertEqual(crosswalk[0]["status"], "collection_bundle_set")
            self.assertEqual([item["bundle_id"] for item in crosswalk[0]["relations"]], ["flow-child"])
            self.assertEqual(crosswalk[1]["status"], "project_root_descriptor")
            self.assertTrue(all(item["relation_verified"] for item in crosswalk))
            self.assertEqual(report["high_value"]["relation_verified_records"], 2)

    def test_external_visualization_reuse_is_verified_by_exact_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "plot-code"
            source.mkdir()
            article = source / "001_demo.md"
            article.write_text("# demo\n", encoding="utf-8")
            digest = MODULE.sha256_file(article)
            snapshot = base / "visualization-2026718-v1" / "assets" / "source_archive"
            snapshot.mkdir(parents=True)
            (snapshot / "SHA256SUMS.csv").write_text(
                f"path,bytes,sha256\n001_demo.md,{article.stat().st_size},{digest}\n",
                encoding="utf-8",
            )
            root = MODULE.SourceRoot(
                "plot-code",
                "绘图代码",
                source,
                {
                    "overlap_policy": "link_existing_visualization_archive",
                    "reuse_skill": "visualization-2026718-v1",
                    "reuse_snapshot": "assets/source_archive",
                },
            )
            record = {
                "来源文件夹": "绘图代码",
                "清单文件": "plots.md",
                "记录标题": "demo",
                "路径": str(article),
                "蒸馏价值": "高",
            }
            file_rows = [{"source_root_id": "plot-code", "relative_path": "001_demo.md", "sha256": digest}]
            with mock.patch.object(MODULE, "SKILL_ROOT", base / "biomedical-analysis-agent"):
                _, crosswalk, report = MODULE.build_preprocessing_crosswalk([record], [], [root], file_rows)
            self.assertEqual(crosswalk[0]["status"], "external_reuse_hash_verified")
            self.assertTrue(crosswalk[0]["relation_verified"])
            self.assertEqual(crosswalk[0]["relations"][0]["sha256"], digest)
            self.assertEqual(report["high_value"]["records_without_relations"], 0)

    def test_reviewed_gold_set_is_frozen_and_new_candidate_is_kept_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            review_dir = output / "manual-review"
            review_dir.mkdir()
            (review_dir / "gold-review-batch-a.jsonl").write_text(
                json.dumps({"bundle_id": "flow-reviewed", "domain": "单细胞分析", "title": "Reviewed"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            bundles = [{"bundle_id": "flow-reviewed"}, {"bundle_id": "flow-new"}]
            candidate = {"schema_version": "1.1", "selected": [{"bundle_id": "flow-new"}]}
            frozen, next_gold = MODULE.resolve_gold_set(output, candidate, bundles)
            self.assertTrue(frozen["frozen"])
            self.assertEqual(frozen["selected"][0]["bundle_id"], "flow-reviewed")
            self.assertIs(next_gold, candidate)


if __name__ == "__main__":
    unittest.main()
