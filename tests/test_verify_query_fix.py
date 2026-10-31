"""Unit tests for tools/verify-query-fix.py (Goal 1 / J14).

Verifies receipt evaluation logic, reference comparison, PE parsing,
and symbol resolution guards without loading the real TaskManagerDataLayer.dll
or touching real running Task Manager instances.
"""
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

spec = importlib.util.spec_from_file_location(
    "verify_query_fix",
    Path(__file__).resolve().parents[1] / "tools" / "verify-query-fix.py"
)
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)

from isolated_test_case import IsolatedTestCase


def make_synthetic_pe(include_ntqsi: bool = True) -> bytes:
    """Creates a minimal in-memory PE32+ image containing an import directory."""
    buf = bytearray(0x2000)
    struct.pack_into("<I", buf, 0x3C, 0x80)
    pe = 0x80
    buf[pe:pe + 4] = b"PE\0\0"
    struct.pack_into("<H", buf, pe + 6, 1)  # 1 section
    optional_size = 240
    struct.pack_into("<H", buf, pe + 20, optional_size)
    # Data Directory 1 (Import table) at pe + 24 + 112 + 8
    struct.pack_into("<II", buf, pe + 24 + 112 + 8, 0x1000, 40)
    # Section header at pe + 24 + optional_size (VirtualSize, VA, RawSize, RawOffset)
    sec = pe + 24 + optional_size
    struct.pack_into("<IIII", buf, sec + 8, 0x1000, 0x1000, 0x1000, 0x400)

    def write_at_rva(rva: int, data: bytes):
        off = 0x400 + (rva - 0x1000)
        buf[off:off + len(data)] = data

    if include_ntqsi:
        # Import descriptor at 0x1000: lookup=0x1100, timestamp=0, fwd=0, name=0x1200, iat=0x1300
        write_at_rva(0x1000, struct.pack("<IIIII", 0x1100, 0, 0, 0x1200, 0x1300))
        write_at_rva(0x1014, b"\0" * 20)  # null descriptor
        write_at_rva(0x1200, b"ntdll.dll\0")
        write_at_rva(0x1100, struct.pack("<QQ", 0x1250, 0))
        write_at_rva(0x1250, struct.pack("<H", 0) + b"NtQuerySystemInformation\0")
        write_at_rva(0x1300, struct.pack("<QQ", 0x1250, 0))
    else:
        # Import descriptor pointing to other function
        write_at_rva(0x1000, struct.pack("<IIIII", 0x1100, 0, 0, 0x1200, 0x1300))
        write_at_rva(0x1014, b"\0" * 20)
        write_at_rva(0x1200, b"kernel32.dll\0")
        write_at_rva(0x1100, struct.pack("<QQ", 0x1250, 0))
        write_at_rva(0x1250, struct.pack("<H", 0) + b"ExitProcess\0")

    return bytes(buf)


class TestPEParsing(IsolatedTestCase):
    def test_find_ntqsi_import_slot_success(self):
        pe_data = make_synthetic_pe(include_ntqsi=True)
        slot_rva, dll, fn = v.find_ntqsi_import_slot(pe_data)
        self.assertEqual(slot_rva, 0x1300)
        self.assertEqual(dll, "ntdll.dll")
        self.assertEqual(fn, "NtQuerySystemInformation")

    def test_find_ntqsi_import_slot_missing_raises_runtime_error(self):
        pe_data = make_synthetic_pe(include_ntqsi=False)
        with self.assertRaises(RuntimeError) as ctx:
            v.find_ntqsi_import_slot(pe_data)
        self.assertIn("Failed to dynamically locate NtQuerySystemInformation", str(ctx.exception))

    def test_pe_bytes_outside_section_raises_value_error(self):
        pe_data = make_synthetic_pe(include_ntqsi=True)
        with self.assertRaises(ValueError) as ctx:
            v.pe_bytes(pe_data, 0x9999, 4)
        self.assertIn("RVA outside backed section", str(ctx.exception))

    def test_cstring_unterminated_raises_value_error(self):
        buf = bytearray(make_synthetic_pe(include_ntqsi=True))
        # Corrupt string by removing null terminator
        buf[0x400 + 0x200:0x400 + 0x200 + 10] = b"A" * 10
        # Read from offset that never terminates
        infinite_str_buf = bytearray(0x2000)
        struct.pack_into("<I", infinite_str_buf, 0x3C, 0x80)
        infinite_str_buf[0x80:0x84] = b"PE\0\0"
        struct.pack_into("<H", infinite_str_buf, 0x86, 1)
        struct.pack_into("<H", infinite_str_buf, 0x80 + 20, 240)
        sec = 0x80 + 24 + 240
        struct.pack_into("<IIII", infinite_str_buf, sec + 8, 0x1000, 0x1000, 0x1000, 0x400)
        infinite_str_buf[0x400:0x1400] = b"X" * 0x1000
        with self.assertRaises(ValueError) as ctx:
            v.cstring(bytes(infinite_str_buf), 0x1000)
        self.assertIn("Unterminated import string", str(ctx.exception))


class TestCriteriaEvaluation(IsolatedTestCase):
    def test_evaluate_criteria_defective_9549(self):
        mock_results = {
            "cases": [
                {
                    "case": "three_length_mismatches",
                    "native_hresult": "0x00000000",
                    "output_bytes": 16384,
                    "last": [{"offset": 0, "next": 0, "pid": 0, "ct": 0}],
                },
                {
                    "case": "success_on_third",
                    "native_hresult": "0x8007000e",
                    "output_bytes": 0,
                    "last": [],
                },
                {
                    "case": "success_on_second",
                    "native_hresult": "0x00000000",
                    "output_bytes": 16384,
                    "last": [{"offset": 0, "next": 0, "pid": 4, "ct": 123}],
                },
                {
                    "case": "access_denied_second",
                    "native_hresult": "0x80070005",
                    "output_bytes": 0,
                    "last": [],
                },
            ]
        }
        res = v.evaluate_criteria(mock_results)
        self.assertEqual(res["verdict"], "DEFECTIVE_9549_CONFIRMED")
        self.assertIn("Negative control confirmed", res["verdict_summary"])
        crit = res["criteria"]
        self.assertFalse(crit["criterion_1_exhaustion_not_success"]["passed"])
        self.assertTrue(crit["criterion_1_exhaustion_not_success"]["defect_present"])
        self.assertFalse(crit["criterion_2_third_call_success_not_oom"]["passed"])
        self.assertTrue(crit["criterion_2_third_call_success_not_oom"]["defect_present"])
        self.assertTrue(crit["criterion_3_baseline_and_error_propagation"]["passed"])

    def test_evaluate_criteria_fixed_binary_pass(self):
        mock_results = {
            "cases": [
                {
                    "case": "three_length_mismatches",
                    "native_hresult": "0x8007000e",
                    "output_bytes": 0,
                    "last": [],
                },
                {
                    "case": "success_on_third",
                    "native_hresult": "0x00000000",
                    "output_bytes": 24576,
                    "last": [{"offset": 0, "next": 0, "pid": 4, "ct": 123}],
                },
                {
                    "case": "success_on_second",
                    "native_hresult": "0x00000000",
                    "output_bytes": 16384,
                    "last": [{"offset": 0, "next": 0, "pid": 4, "ct": 123}],
                },
                {
                    "case": "access_denied_second",
                    "native_hresult": "0x80070005",
                    "output_bytes": 0,
                    "last": [],
                },
            ]
        }
        res = v.evaluate_criteria(mock_results)
        self.assertEqual(res["verdict"], "PASS")
        self.assertIn("FIXED", res["verdict_summary"])
        crit = res["criteria"]
        self.assertTrue(crit["criterion_1_exhaustion_not_success"]["passed"])
        self.assertTrue(crit["criterion_2_third_call_success_not_oom"]["passed"])
        self.assertTrue(crit["criterion_3_baseline_and_error_propagation"]["passed"])

    def test_evaluate_criteria_unexpected_failure(self):
        mock_results = {
            "cases": [
                {
                    "case": "three_length_mismatches",
                    "native_hresult": "0x8007000e",
                    "output_bytes": 0,
                    "last": [],
                },
                {
                    "case": "success_on_third",
                    "native_hresult": "0x00000000",
                    "output_bytes": 24576,
                    "last": [],
                },
                {
                    "case": "success_on_second",
                    "native_hresult": "0x80004005",  # unexpected failure on baseline
                    "output_bytes": 0,
                    "last": [],
                },
                {
                    "case": "access_denied_second",
                    "native_hresult": "0x80070005",
                    "output_bytes": 0,
                    "last": [],
                },
            ]
        }
        res = v.evaluate_criteria(mock_results)
        self.assertEqual(res["verdict"], "FAIL")
        self.assertIn("Unexpected failure", res["verdict_summary"])


class TestReferenceComparison(IsolatedTestCase):
    def test_compare_with_reference_missing_file(self):
        res = v.compare_with_reference({}, Path("non_existent_file_xyz123.json"))
        self.assertFalse(res["compared"])
        self.assertIn("not found", res["reason"])

    def test_compare_with_reference_matching(self):
        ref_data = {
            "dll_sha256": "abc123hash",
            "cases": [
                {"case": "success_on_second", "native_hresult": "0x00000000", "output_bytes": 16384},
                {"case": "three_length_mismatches", "native_hresult": "0x00000000", "output_bytes": 16384},
            ]
        }
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".json", encoding="utf-8") as f:
            json.dump(ref_data, f)
            temp_path = Path(f.name)
        try:
            receipt = {
                "controlled_results": {
                    "cases": [
                        {"case": "success_on_second", "native_hresult": "0x00000000", "output_bytes": 16384},
                        {"case": "three_length_mismatches", "native_hresult": "0x00000000", "output_bytes": 16384},
                    ]
                }
            }
            res = v.compare_with_reference(receipt, temp_path)
            self.assertTrue(res["compared"])
            self.assertTrue(res["matches_reference_100_percent"])
            self.assertEqual(res["mismatches"], [])
        finally:
            temp_path.unlink(missing_ok=True)

    def test_compare_with_reference_mismatch(self):
        ref_data = {
            "dll_sha256": "abc123hash",
            "cases": [
                {"case": "success_on_second", "native_hresult": "0x00000000", "output_bytes": 16384},
            ]
        }
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".json", encoding="utf-8") as f:
            json.dump(ref_data, f)
            temp_path = Path(f.name)
        try:
            receipt = {
                "controlled_results": {
                    "cases": [
                        {"case": "success_on_second", "native_hresult": "0x80004005", "output_bytes": 0},
                    ]
                }
            }
            res = v.compare_with_reference(receipt, temp_path)
            self.assertTrue(res["compared"])
            self.assertFalse(res["matches_reference_100_percent"])
            self.assertTrue(len(res["mismatches"]) > 0)
        finally:
            temp_path.unlink(missing_ok=True)


class TestReceiptMarkdown(IsolatedTestCase):
    def test_generate_receipt_markdown_structure(self):
        receipt = {
            "timestamp_utc": "2026-10-31T22:00:00Z",
            "target_dll": {
                "path": "C:\\Windows\\System32\\TaskManagerDataLayer.dll",
                "sha256": "deadbeef1234",
                "component_store_version": "10.0.26100.9549",
            },
            "symbol_resolution": {
                "symbol_path": "srv*C:\\Symbols*https://msdl.microsoft.com/download/symbols",
                "query_symbol": "TaskManager::DataSources::ProcessDataGenerator::QueryProcessInformation",
                "query_rva": "0x4bca8",
                "tidy_symbol": "std::vector<unsigned char>::_Tidy",
                "tidy_rva": "0x1a3fc",
            },
            "import_resolution": {
                "slot_rva": "0x7a9c8",
                "dll": "ntdll.dll",
            },
            "evaluation": {
                "verdict": "DEFECTIVE_9549_CONFIRMED",
                "verdict_summary": "Negative control confirmed",
                "criteria": {
                    "criterion_1_exhaustion_not_success": {
                        "passed": False,
                        "observed_hresult": "0x00000000",
                        "published_bytes": 16384,
                        "ghost_record_observed": True,
                    },
                    "criterion_2_third_call_success_not_oom": {
                        "passed": False,
                        "observed_hresult": "0x8007000e",
                        "published_bytes": 0,
                    },
                    "criterion_3_baseline_and_error_propagation": {
                        "passed": True,
                        "second_call_success_hresult": "0x00000000",
                        "access_denied_propagated_hresult": "0x80070005",
                    },
                },
            },
            "reference_comparison": {
                "compared": True,
                "matches_reference_100_percent": True,
                "reference_file": "ref.json",
                "reference_sha256": "deadbeef1234",
                "mismatches": [],
            },
        }
        md = v.generate_receipt_markdown(receipt)
        self.assertIn("# TaskManagerDataLayer.dll Query Fix Verification Receipt", md)
        self.assertIn("DEFECTIVE_9549_CONFIRMED", md)
        self.assertIn("0x4bca8", md)
        self.assertIn("0x7a9c8", md)
        self.assertIn("Criterion", md)
        self.assertIn("100% Match", md)


class TestRecordWalking(IsolatedTestCase):
    def test_walk_records_valid_chain(self):
        # 2 records of 0x100 bytes each, last has next == 0
        buf = bytearray(0x200)
        # Record 0: nxt = 0x100, pid = 4, ct = 100
        struct.pack_into("<I", buf, 0x0, 0x100)
        struct.pack_into("<Q", buf, 0x20, 100)
        struct.pack_into("<Q", buf, 0x50, 4)
        # Record 1: nxt = 0, pid = 1000, ct = 200
        struct.pack_into("<I", buf, 0x100, 0)
        struct.pack_into("<Q", buf, 0x120, 200)
        struct.pack_into("<Q", buf, 0x150, 1000)

        rows, term = v.walk_records(bytes(buf))
        self.assertEqual(term, "end")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["pid"], 4)
        self.assertEqual(rows[1]["pid"], 1000)

    def test_walk_records_invalid_offset(self):
        buf = bytearray(0x200)
        # Record 0: nxt = 0x50 (< 0x100 invalid)
        struct.pack_into("<I", buf, 0x0, 0x50)
        rows, term = v.walk_records(bytes(buf))
        self.assertEqual(term, "invalid_offset")
        self.assertEqual(len(rows), 1)

    def test_walk_records_empty(self):
        rows, term = v.walk_records(b"")
        self.assertEqual(term, "bounded_stop")
        self.assertEqual(rows, [])


class TestSymbolResolutionGuards(IsolatedTestCase):
    def test_normalize_symbol_path(self):
        path = "srv*C:/tmp/Symbols*https://msdl.microsoft.com/download/symbols"
        normalized = v.normalize_symbol_path(path)
        self.assertEqual(normalized, r"srv*C:\tmp\Symbols*https://msdl.microsoft.com/download/symbols")

    def test_resolve_public_symbols_missing_cdb_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            v.resolve_public_symbols(Path("dummy.dll"), cdb_path=Path("non_existent_cdb_path.exe"))
        self.assertIn("cdb.exe not found", str(ctx.exception))

    def test_resolve_public_symbols_missing_symbol_raises_without_pinned_fallback(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = """
ModLoad: 00000001`80000000 00000001`80100000   TaskManagerDataLayer.dll
SomeOtherSymbol
quit:
"""
        with patch.object(subprocess, "run", return_value=mock_proc):
            with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".exe") as fake_cdb:
                fake_cdb_path = Path(fake_cdb.name)
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    v.resolve_public_symbols(Path("dummy.dll"), cdb_path=fake_cdb_path)
                self.assertIn("Symbol resolution failed", str(ctx.exception))
                self.assertIn("Will not fall back to pinned RVA", str(ctx.exception))
            finally:
                fake_cdb_path.unlink(missing_ok=True)

    def test_resolve_public_symbols_success_parses_rvas(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = """
ModLoad: 00000001`80000000 00000001`80100000   TaskManagerDataLayer.dll
00000001`8004bca8 TaskManagerDataLayer!TaskManager::DataSources::ProcessDataGenerator::QueryProcessInformation (void)
00000001`8001a3fc TaskManagerDataLayer!std::vector<unsigned char,std::allocator<unsigned char> >::_Tidy (void)
quit:
"""
        with patch.object(subprocess, "run", return_value=mock_proc):
            with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".exe") as fake_cdb:
                fake_cdb_path = Path(fake_cdb.name)
            try:
                res = v.resolve_public_symbols(Path("dummy.dll"), cdb_path=fake_cdb_path)
                self.assertEqual(res["base_va"], "0x180000000")
                self.assertEqual(res["query_rva"], "0x4bca8")
                self.assertEqual(res["tidy_rva"], "0x1a3fc")
                self.assertIn("QueryProcessInformation", res["query_symbol"])
                self.assertIn("_Tidy", res["tidy_symbol"])
            finally:
                fake_cdb_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
