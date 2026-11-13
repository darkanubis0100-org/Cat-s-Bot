import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cogs._research_context import ResearchWorkspace, assess_research, bound_research, public_brief, select_passages


class ResearchContextTest(unittest.TestCase):
    def test_brief_rejects_private_identifiers_and_is_bounded(self):
        for text in ("Email ana@example.com", "<@123456789012345678>", "ID 123456789012345678", "api_key=credential", "x" * 601):
            with self.subTest(text=text), self.assertRaises(ValueError):
                public_brief({"topic": "Example", "question": text})
        self.assertEqual(public_brief({"topic": "Example", "question": "Latest release?"})["topic"], "Example")
    def test_passages_find_relevant_tail_and_keep_order_within_budget(self):
        text = "\n\n".join(["Navigation and unrelated history " * 120] * 8 + ["## Example release 9\nExample release 9 is current since 2030-01-02."])
        result = select_passages(text, "Example current release", 500)
        self.assertIn("Example release 9 is current", result["text"])
        self.assertTrue(result["text_truncated"])
        self.assertLessEqual(len(result["text"].encode("utf-8")), 500)

    def test_late_table_keeps_headers_rows_units_and_scope_under_utf8_budget(self):
        header = "Modelo\tMemoria integrada\tMemoria ampliable\tCapacidad máxima\tFormato"
        rows = ["Card Pro-X\t128 GB\t256 GB\t384 GB\tTarjeta PCIe",
                "System Max-Y\t256 GB\t2,048 GB\t2,304 GB\tServidor completo"]
        table = "Especificaciones por variante\n" + "\n".join([header, *rows])
        for layout in (table, table.replace("\t", "\n\n").replace("\n", "\n\n")):
            with self.subTest(flattened="\t" not in layout):
                text = "\n\n".join(["Example Graphics brings powerful graphics experiences. " * 30] * 20 + [layout])
                result = select_passages(text, "Dame specs de la tarjeta más potente de Example Graphics", 800)
                selected = result["text"]
                for token in ("Modelo", "Memoria integrada", "Memoria ampliable", "Capacidad máxima", "Formato",
                              "Card Pro-X", "128 GB", "384 GB", "Tarjeta PCIe", "System Max-Y", "2,304 GB", "Servidor completo"):
                    self.assertIn(token, selected)
                self.assertLess(selected.index("Modelo"), selected.index("Card Pro-X"))
                self.assertLess(selected.index("Card Pro-X"), selected.index("System Max-Y"))
                self.assertLessEqual(len(selected.encode("utf-8")), 800)
                self.assertTrue(result["text_truncated"])

    def test_whole_context_bound_reselects_evidence_instead_of_cutting_its_prefix(self):
        table = "Model\tCapacity\tScope\nCard X\t384 GB\tPCIe card\nSystem Y\t2,304 GB\tWhole server"
        text = "\n\n".join(["Example Graphics marketing without specifications. " * 30] * 8 + [table])
        web = {"public_brief": {"topic": "Example Graphics", "question": "Dame specs de Example Graphics"},
               "page_evidence_untrusted": [{"url": "https://docs.example/specs", "text": text}],
               "verified_source_urls": ["https://docs.example/specs"],
               "sources": [{"url": "https://docs.example/specs", "kind": "page_read"}],
               "assessment": {"sufficient": False, "facts": [], "gaps": []}, "complete": False}
        bounded = bound_research(web, 800)
        self.assertIn(table, bounded["page_evidence_untrusted"][0]["text"])
        self.assertLessEqual(len(json.dumps(bounded, ensure_ascii=False).encode("utf-8")), 800)
        self.assertFalse(bounded["assessment"]["sufficient"])
        self.assertEqual(bounded["verified_source_urls"], ["https://docs.example/specs"])
        self.assertEqual(bounded["sources"][0]["url"], "https://docs.example/specs")

    def test_table_and_its_variant_heading_are_never_separated(self):
        heading = "System Max-Y — whole server"
        table = "Metric\tValue\n" + "\n".join(f"Memory bank {i}\t256 GB" for i in range(9))
        text = "Unrelated marketing paragraph. " * 40 + "\n\n" + heading + "\n\n" + table
        for budget in (len(table.encode()) + 10, len((heading + "\n\n" + table).encode()) + 4):
            with self.subTest(budget=budget):
                result = select_passages(text, "Dame specs", budget)
                if "256 GB" in result["text"]:
                    self.assertIn(heading + "\n\n" + table, result["text"])
                else:
                    self.assertLess(budget, len((heading + "\n\n" + table).encode()))
                self.assertLessEqual(len(result["text"].encode()), budget)

    def test_many_short_paragraphs_do_not_repeat_suffix_scans(self):
        paragraphs = ["Short public paragraph without numeric cells."] * 3000
        with patch("cogs._research_context.re.search", wraps=re.search) as search:
            result = select_passages("\n\n".join(paragraphs), "Public specifications", 512)
        self.assertTrue(result["text"])
        self.assertLessEqual(len(result["text"].encode()), 512)
        self.assertLessEqual(search.call_count, 3 * len(paragraphs))

    def test_assessment_rejects_invented_quotes_and_requires_two_sources_for_conflicts(self):
        pages = {"https://official.example/releases": {"text": "Release 9 is current.", "retrieved_at_utc": "2030-01-02"}}
        report = {"summary": "Release status", "public_topic": "Example releases", "sufficient": True, "gaps": [],
                  "facts": [{"claim": "Version 10", "status": "supported", "support": [{"url": next(iter(pages)), "quote": "Release 10"}]},
                            {"claim": "Disagreement", "status": "conflicting", "support": [{"url": next(iter(pages)), "quote": "Release 9 is current."}]}]}
        result = assess_research(report, pages)
        self.assertFalse(result["sufficient"])
        self.assertEqual({fact["status"] for fact in result["facts"]}, {"unknown"})

    def test_assessment_preserves_supported_quote_dates_and_unknowns(self):
        url = "https://official.example/releases"
        pages = {url: {"text": "Release 9 is current.", "retrieved_at_utc": "2030-01-02"}}
        result = assess_research({"summary": "Release status", "public_topic": "Example", "sufficient": True, "gaps": [],
            "facts": [{"claim": "Version 9", "status": "supported", "support": [{"url": url, "quote": "Release 9 is current."}]}]}, pages)
        self.assertTrue(result["sufficient"])
        self.assertEqual(result["facts"][0]["support"][0]["retrieved_at_utc"], "2030-01-02")
        self.assertFalse(assess_research(None, pages)["sufficient"])

    def test_compacted_ledger_preserves_only_previously_validated_url_quote_pairs(self):
        url = "https://official.example/releases"
        original = {"summary": "Release 10 invented summary", "public_topic": "Example", "sufficient": True, "gaps": [],
            "facts": [{"claim": "Version 9", "status": "supported", "support": [{"url": url, "quote": "Release 9 is current."}]}]}
        previous = assess_research(original, {url: {"text": "Release 9 is current.", "retrieved_at_utc": "2030-01-02"}})
        continued = assess_research(original, {}, previous=previous)
        self.assertTrue(continued["sufficient"])
        self.assertEqual(continued["facts"][0]["support"][0]["retrieved_at_utc"], "2030-01-02")
        for source_url, quote in ((url, "Release 10 invented summary"), ("https://other.example/", "Release 9 is current.")):
            original["facts"][0]["support"] = [{"url": source_url, "quote": quote}]
            self.assertFalse(assess_research(original, {}, previous=previous)["sufficient"])

    def test_optional_diagnostics_are_pruned_before_supported_evidence(self):
        url = "https://official.example/releases"
        assessment = assess_research({"sufficient": True, "gaps": [], "facts": [{"claim": "Version 9",
            "status": "supported", "support": [{"url": url, "quote": "Release 9 is current."}]}]},
            {url: {"text": "Release 9 is current.", "retrieved_at_utc": "2030-01-02"}})
        errors = [{"result_untrusted": {"error": "http_error", "url": "https://example.org/" + "x" * 2000,
            "next_action": "Try another relevant public source."}} for _ in range(4)]
        result = bound_research({"assessment": assessment, "diagnostics": {"observations": errors},
            "results_untrusted": errors, "complete": True, "sources": [], "verified_source_urls": [url]}, 8192)
        self.assertTrue(result["assessment"]["sufficient"])
        self.assertEqual(result["assessment"]["facts"], assessment["facts"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), 8192)

    def test_workspace_is_scoped_bounded_atomic_and_expires(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ResearchWorkspace(Path(folder), ttl=30, max_files=2)
            with patch("cogs._research_context.time.time", return_value=100):
                for user in (1, 2, 3):
                    store.save((10, 20, user), {"scope_token": "same", "assessment": {"facts": []}})
                self.assertEqual(len(list(Path(folder).rglob("*.md"))), 2)
                self.assertIsNone(store.load((10, 21, 3), "same"))
                self.assertIsNone(store.load((10, 20, 3), "changed"))
                store.save((10, 20, 3), {"scope_token": "same", "assessment": {"facts": []}})
                self.assertEqual(store.load((10, 20, 3), "same")["assessment"], {"facts": []})
                self.assertFalse(list(Path(folder).rglob("*.tmp")))
            with patch("cogs._research_context.time.time", return_value=131):
                self.assertIsNone(store.load((10, 20, 3), "same"))

    def test_workspace_does_not_read_symlinks_or_oversized_files(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ResearchWorkspace(Path(folder))
            store.save((10, 20, 30), {"scope_token": "token"})
            path = next(Path(folder).rglob("*.md"))
            path.write_text("x" * 100000, encoding="utf-8")
            self.assertIsNone(store.load((10, 20, 30), "token"))

    def test_whole_ledger_including_long_urls_summary_and_gaps_fits_byte_allowance(self):
        url = "https://example.com/" + "a" * 1900
        web = {"sources": [{"url": url, "title": "á" * 200} for _ in range(5)],
               "verified_source_urls": [url], "search_result_urls": [url] * 5, "results_untrusted": [],
               "complete": True, "pages_read": 1, "public_brief": {"topic": "á" * 200, "question": "á" * 600},
               "assessment": {"facts": [], "summary": "á" * 1500, "gaps": ["á" * 300] * 8, "sufficient": True}}
        for limit in (512, 4096):
            with self.subTest(limit=limit):
                bounded = bound_research(web, limit)
                self.assertLessEqual(len(json.dumps(bounded, ensure_ascii=False).encode("utf-8")), limit)
                self.assertFalse(bounded["assessment"]["sufficient"])
                self.assertEqual(bounded["pages_read"], 1)

    def test_optional_workspace_handles_directory_and_cleanup_io_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ResearchWorkspace(Path(folder))
            with patch.object(Path, "mkdir", side_effect=PermissionError("read-only")):
                store.save((10, 20, 30), {"scope_token": "token"})
            with patch("cogs._research_context.os.replace", side_effect=OSError("full")):
                store.save((10, 20, 30), {"scope_token": "token"})
            self.assertIsNone(store.load((10, 20, 30), "token"))
