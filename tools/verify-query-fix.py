#!/usr/bin/env python3
"""Verify TaskManagerDataLayer.dll query defects via controlled execution (Goal 1 / J14).

Executes Microsoft's QueryProcessInformation machine code in an isolated child process
with a synthetic NtQuerySystemInformation import slot. Resolves function entry and
vector cleanup via Microsoft public symbols (cdb), and locates the NtQSI import slot
dynamically from PE imports. Does not attach a debugger and never touches running
Task Manager instances.

Usage:
    python tools/verify-query-fix.py [--dll <path>] [--output <dir>] [--symbols <sympath>] [--cdb <cdb_path>]
    python tools/verify-query-fix.py --real-kernel [--churn-seconds 5]
"""
import argparse
import ctypes as C
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import threading
import time

DEFAULT_DLL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/TaskManagerDataLayer.dll"
DEFAULT_SYMBOLS = r"srv*C:\tmp\TaskmgrCpuDiag-20260928-194418\Symbols*https://msdl.microsoft.com/download/symbols"
REFERENCE_JSON = Path(__file__).resolve().parents[1] / ".local/work-inbox/20261021-native-9549/native-controlled.json"


# -----------------------------------------------------------------------------
# PE Parsing
# -----------------------------------------------------------------------------

def pe_bytes(image: bytes, rva: int, size: int) -> bytes:
    pe = struct.unpack_from('<I', image, 0x3c)[0]
    count = struct.unpack_from('<H', image, pe + 6)[0]
    optional = struct.unpack_from('<H', image, pe + 20)[0]
    for n in range(count):
        at = pe + 24 + optional + n * 40
        virtual_size, start, raw_size, raw = struct.unpack_from('<IIII', image, at + 8)
        if start <= rva and rva + size <= start + min(virtual_size, raw_size):
            return image[raw + rva - start:raw + rva - start + size]
    raise ValueError(f"RVA outside backed section: {rva:x}")


def cstring(image: bytes, rva: int) -> str:
    val = bytearray()
    for i in range(512):
        b = pe_bytes(image, rva + i, 1)
        if b == b'\0':
            return val.decode('ascii')
        val.extend(b)
    raise ValueError(f"Unterminated import string at RVA: {rva:x}")


def find_ntqsi_import_slot(image: bytes) -> tuple[int, str, str]:
    """Dynamically locates the IAT slot for ntdll.dll!NtQuerySystemInformation."""
    pe = struct.unpack_from('<I', image, 0x3c)[0]
    start, size = struct.unpack_from('<II', image, pe + 24 + 112 + 8)
    for at in range(start, start + size, 20):
        lookup, _, _, name_rva, iat_rva = struct.unpack('<IIIII', pe_bytes(image, at, 20))
        if not any((lookup, name_rva, iat_rva)):
            break
        if not lookup:
            continue
        dll_name = cstring(image, name_rva)
        if 'ntdll' in dll_name.lower():
            for i in range(4096):
                val = struct.unpack('<Q', pe_bytes(image, lookup + 8 * i, 8))[0]
                if not val:
                    break
                if not (val >> 63):
                    fn_name = cstring(image, val + 2)
                    if fn_name == 'NtQuerySystemInformation':
                        return (iat_rva + 8 * i, dll_name, fn_name)
    raise RuntimeError("Failed to dynamically locate NtQuerySystemInformation in PE imports")


def get_dll_version_and_metadata(dll_path: Path) -> dict:
    """Computes SHA-256 and searches component-store / PE info for version."""
    data = dll_path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    stat = dll_path.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()

    # Search WinSxS for component-store version matching hash
    component_version = None
    winsxs = dll_path.parent.parent / "WinSxS"
    if winsxs.is_dir():
        try:
            for p in winsxs.glob("amd64_microsoft-windows-advancedtaskmanager_*/TaskManagerDataLayer.dll"):
                m = re.search(r'_([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)_', p.parent.name)
                if m:
                    try:
                        if hashlib.sha256(p.read_bytes()).hexdigest() == sha256:
                            component_version = m.group(1)
                            break
                    except Exception:
                        pass
        except Exception:
            pass

    return {
        "path": str(dll_path.resolve()),
        "sha256": sha256,
        "size_bytes": len(data),
        "modified_utc": mtime,
        "component_store_version": component_version,
    }


# -----------------------------------------------------------------------------
# Symbol Resolution (J8 method via cdb)
# -----------------------------------------------------------------------------

def find_cdb() -> Path | None:
    """Finds cdb.exe from environment, WinDbg AppX, or Windows Kits."""
    cdb_env = os.environ.get("CDB_PATH")
    if cdb_env and Path(cdb_env).is_file():
        return Path(cdb_env)
    known = [
        Path(r"C:\Program Files\WindowsApps\Microsoft.WinDbg_1.2606.22001.0_x64__8wekyb3d8bbwe\amd64\cdb.exe"),
        Path(r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe"),
        Path(r"C:\Program Files\Windows Kits\10\Debuggers\x64\cdb.exe"),
    ]
    for p in known:
        if p.is_file():
            return p
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", "(Get-AppxPackage -Name *WinDbg*).InstallLocation"],
            text=True, timeout=5
        ).strip()
        if out:
            p = Path(out) / "amd64" / "cdb.exe"
            if p.is_file():
                return p
    except Exception:
        pass
    which = shutil.which("cdb.exe")
    if which:
        return Path(which)
    return None


def normalize_symbol_path(sym: str) -> str:
    """Ensures local directory portions in srv*... use Windows backslashes."""
    parts = sym.split('*')
    normalized = []
    for p in parts:
        if p.lower().startswith('http://') or p.lower().startswith('https://') or p.lower() == 'srv':
            normalized.append(p)
        else:
            normalized.append(p.replace('/', '\\'))
    return '*'.join(normalized)


def resolve_public_symbols(dll_path: Path, symbol_path: str = DEFAULT_SYMBOLS, cdb_path: Path | None = None) -> dict:
    """Resolves QueryProcessInformation and vector _Tidy from Microsoft public symbols.

    Explicitly fails if symbols cannot be resolved; never falls back to pinned RVAs.
    """
    if cdb_path is None:
        cdb_path = find_cdb()
    if not cdb_path or not cdb_path.is_file():
        raise RuntimeError(f"cdb.exe not found. Set CDB_PATH environment variable or install WinDbg.")

    symbol_path = normalize_symbol_path(symbol_path)
    cmd = (
        "x TaskManagerDataLayer!*QueryProcessInformation*; "
        "x TaskManagerDataLayer!*vector<unsigned char*>::_Tidy*; "
        "q"
    )

    res = subprocess.run(
        [str(cdb_path), "-z", str(dll_path), "-y", symbol_path, "-c", cmd],
        capture_output=True, text=True, timeout=60
    )
    if res.returncode != 0 and not res.stdout:
        raise RuntimeError(f"cdb execution failed with exit code {res.returncode}: {res.stderr}")

    mod_match = re.search(r"ModLoad:\s+([0-9a-fA-F`]+)\s+([0-9a-fA-F`]+)", res.stdout)
    if not mod_match:
        raise RuntimeError(f"Could not find module base in cdb output:\n{res.stdout[-2000:]}")
    base_va = int(mod_match.group(1).replace("`", ""), 16)

    # Match exact ProcessDataGenerator::QueryProcessInformation
    q_match = re.search(
        r"([0-9a-fA-F`]+)\s+(TaskManagerDataLayer!TaskManager::DataSources::ProcessDataGenerator::QueryProcessInformation\b[^\r\n]*)",
        res.stdout
    )
    if not q_match:
        raise RuntimeError(
            "Symbol resolution failed: could not locate public symbol "
            "TaskManager::DataSources::ProcessDataGenerator::QueryProcessInformation. "
            "Will not fall back to pinned RVA."
        )
    q_va = int(q_match.group(1).replace("`", ""), 16)
    q_sym = q_match.group(2).strip()
    q_rva = q_va - base_va

    # Match vector _Tidy
    t_match = re.search(
        r"([0-9a-fA-F`]+)\s+(TaskManagerDataLayer!std::vector<unsigned char[^\r\n]*::_Tidy\b[^\r\n]*)",
        res.stdout
    )
    if not t_match:
        # Fallback to broader vector tidy match
        t_match = re.search(
            r"([0-9a-fA-F`]+)\s+(TaskManagerDataLayer!std::vector<[^\r\n]*::_Tidy\b[^\r\n]*)",
            res.stdout
        )
    if not t_match:
        raise RuntimeError(
            "Symbol resolution failed: could not locate public symbol for std::vector<...>::_Tidy. "
            "Will not fall back to pinned RVA."
        )
    t_va = int(t_match.group(1).replace("`", ""), 16)
    t_sym = t_match.group(2).strip()
    t_rva = t_va - base_va

    return {
        "base_va": hex(base_va),
        "query_rva": hex(q_rva),
        "query_symbol": q_sym,
        "tidy_rva": hex(t_rva),
        "tidy_symbol": t_sym,
        "symbol_path": symbol_path,
        "cdb_path": str(cdb_path),
    }


# -----------------------------------------------------------------------------
# Record Parsing
# -----------------------------------------------------------------------------

def walk_records(data: bytes) -> tuple[list[dict], str]:
    """Safely traverses SYSTEM_PROCESS_INFORMATION records without reading strings."""
    off = 0
    rows = []
    while off + 0x100 <= len(data) and len(rows) < 100000:
        nxt = int.from_bytes(data[off:off + 4], "little")
        pid = int.from_bytes(data[off + 0x50:off + 0x58], "little")
        ct = int.from_bytes(data[off + 0x20:off + 0x28], "little")
        rows.append({"offset": off, "next": nxt, "pid": pid, "ct": ct})
        if nxt == 0:
            return rows, "end"
        if nxt < 0x100:
            return rows, "invalid_offset"
        off += nxt
    return rows, "bounded_stop"


# -----------------------------------------------------------------------------
# Child Process Execution Worker (Controlled Mode)
# -----------------------------------------------------------------------------

def run_controlled_worker(dll_path: Path, query_rva: int, tidy_rva: int, slot_rva: int) -> dict:
    """Executes the 4 controlled test cases inside the current process."""
    module = C.WinDLL(str(dll_path))
    base = module._handle

    class Vector(C.Structure):
        _fields_ = [("begin", C.c_void_p), ("end", C.c_void_p), ("capacity", C.c_void_p)]

    query = C.WINFUNCTYPE(C.c_long, C.c_void_p, C.POINTER(Vector))(base + query_rva)
    tidy = C.WINFUNCTYPE(None, C.POINTER(Vector))(base + tidy_rva)

    def call_query():
        vec = Vector()
        try:
            hr = query(None, C.byref(vec)) & 0xFFFFFFFF
            size = (vec.end or 0) - (vec.begin or 0)
            data = C.string_at(vec.begin, size) if (size and 0 <= size <= 64 * 1024 * 1024) else b""
            return hr, data
        finally:
            tidy(C.byref(vec))

    k = C.WinDLL("kernel32", use_last_error=True)
    vp = k.VirtualProtect
    vp.argtypes = [C.c_void_p, C.c_size_t, C.c_ulong, C.POINTER(C.c_ulong)]
    vp.restype = C.c_int

    slot = C.c_void_p.from_address(base + slot_rva)
    original = slot.value
    native = C.cast(C.WinDLL("ntdll").NtQuerySystemInformation, C.c_void_p).value
    assert original == native, f"Unexpected NtQSI import value: {hex(original)} vs native {hex(native)}"

    cb_type = C.WINFUNCTYPE(C.c_long, C.c_ulong, C.c_void_p, C.c_ulong, C.POINTER(C.c_ulong))
    rows = []
    protections = []
    cases = [
        ("success_on_second", [0xC0000004, 0], 0),
        ("success_on_third", [0xC0000004, 0xC0000004, 0], 0x8007000E),
        ("three_length_mismatches", [0xC0000004] * 3, 0),
        ("access_denied_second", [0xC0000004, 0xC0000022], 0x80070005),
    ]

    for name, statuses, expected_on_9549 in cases:
        calls = []

        def mock(info, buf, length, retlen):
            i = len(calls)
            if i >= len(statuses):
                return C.c_long(0xC0000001).value
            status = statuses[i]
            required = 0x2000 + i * 0x1000 if status else 0x2000
            retlen[0] = required
            if buf and length >= 0x100:
                C.memset(buf, 0, length)
                C.c_uint32.from_address(buf).value = 0x100 if status == 0xC0000004 else 0
                C.c_uint64.from_address(buf + 0x20).value = 123
                C.c_uint64.from_address(buf + 0x50).value = 4
            calls.append({"status": f"0x{status & 0xFFFFFFFF:08x}", "provided": length, "required": required})
            return C.c_long(status).value

        callback = cb_type(mock)
        old = C.c_ulong()
        assert vp(C.addressof(slot), C.sizeof(slot), 0x04, C.byref(old)), C.get_last_error()
        try:
            slot.value = C.cast(callback, C.c_void_p).value
            hr, data = call_query()
        finally:
            slot.value = original
            assert slot.value == original
            discarded = C.c_ulong()
            assert vp(C.addressof(slot), C.sizeof(slot), old.value, C.byref(discarded)), C.get_last_error()

        protections.append({"original": old.value, "while_mocked": discarded.value, "restore_succeeded": True})
        records, termination = walk_records(data) if data else ([], "empty")
        rows.append({
            "case": name,
            "calls": calls,
            "native_hresult": f"0x{hr:08x}",
            "output_bytes": len(data),
            "record_count": len(records),
            "last": records[-1:] if records else [],
            "termination": termination,
        })

    return {
        "mode": "native_function_with_synthetic_NtQSI",
        "child_pid": os.getpid(),
        "import_restored": slot.value == original,
        "page_protection_restorations": protections,
        "cases": rows,
    }


# -----------------------------------------------------------------------------
# Optional Real-Kernel Observed Mode
# -----------------------------------------------------------------------------

def run_real_kernel_worker(dll_path: Path, query_rva: int, tidy_rva: int, slot_rva: int,
                           seconds: int = 5, churn_threads: int = 4) -> dict:
    """Executes live queries under real process churn while monitoring NtQSI import calls."""
    module = C.WinDLL(str(dll_path))
    base = module._handle

    class Vector(C.Structure):
        _fields_ = [("begin", C.c_void_p), ("end", C.c_void_p), ("capacity", C.c_void_p)]

    query = C.WINFUNCTYPE(C.c_long, C.c_void_p, C.POINTER(Vector))(base + query_rva)
    tidy = C.WINFUNCTYPE(None, C.POINTER(Vector))(base + tidy_rva)

    def call_query():
        vec = Vector()
        try:
            hr = query(None, C.byref(vec)) & 0xFFFFFFFF
            size = (vec.end or 0) - (vec.begin or 0)
            data = C.string_at(vec.begin, size) if (size and 0 <= size <= 64 * 1024 * 1024) else b""
            return hr, data
        finally:
            tidy(C.byref(vec))

    k = C.WinDLL("kernel32", use_last_error=True)
    vp = k.VirtualProtect
    vp.argtypes = [C.c_void_p, C.c_size_t, C.c_ulong, C.POINTER(C.c_ulong)]
    vp.restype = C.c_int

    cb_type = C.WINFUNCTYPE(C.c_long, C.c_ulong, C.c_void_p, C.c_ulong, C.POINTER(C.c_ulong))
    native = cb_type(("NtQuerySystemInformation", C.WinDLL("ntdll")))
    slot = C.c_void_p.from_address(base + slot_rva)
    original = slot.value
    assert original == C.cast(native, C.c_void_p).value

    calls = []
    def observe(info, buf, length, retlen):
        status = native(info, buf, length, retlen)
        req = retlen[0] if retlen else 0
        calls.append({"status": f"0x{status & 0xFFFFFFFF:08x}", "provided": length, "required": req})
        return status

    callback = cb_type(observe)
    old = C.c_ulong()
    assert vp(C.addressof(slot), C.sizeof(slot), 0x04, C.byref(old))

    counts = {}
    stop_churn = threading.Event()
    def churn_worker():
        while not stop_churn.is_set():
            subprocess.run(["cmd", "/d", "/c", "exit"], creationflags=0x08000000, timeout=5)

    workers = [threading.Thread(target=churn_worker) for _ in range(churn_threads)]
    for w in workers:
        w.start()

    end_time = time.monotonic() + seconds
    queries_run = 0
    try:
        slot.value = C.cast(callback, C.c_void_p).value
        while time.monotonic() < end_time:
            calls.clear()
            hr, data = call_query()
            queries_run += 1
            rows, termination = walk_records(data) if hr == 0 else ([], "not_walked")
            ghost = [r for r in rows if r["pid"] == 0 and r["ct"] == 0]
            exhausted = len(calls) == 3 and all(c["status"] == "0xc0000004" for c in calls)
            third_success = len(calls) == 3 and calls[-1]["status"] == "0x00000000"
            key = f"hr=0x{hr:08x};calls={len(calls)};exhausted={exhausted};third_success={third_success};ghost={bool(ghost)}"
            counts[key] = counts.get(key, 0) + 1
            time.sleep(0.005)
    finally:
        stop_churn.set()
        for w in workers:
            w.join(timeout=6)
        slot.value = original
        discarded = C.c_ulong()
        assert vp(C.addressof(slot), C.sizeof(slot), old.value, C.byref(discarded))

    return {
        "mode": "native_function_real_kernel_observed",
        "seconds": seconds,
        "queries_run": queries_run,
        "counts": counts,
        "churn_threads": churn_threads,
    }


# -----------------------------------------------------------------------------
# Acceptance Criteria Evaluation (Goal 1)
# -----------------------------------------------------------------------------

def evaluate_criteria(controlled_results: dict) -> dict:
    """Evaluates Goal 1 criteria against the controlled execution results."""
    cases = {c["case"]: c for c in controlled_results.get("cases", [])}

    # Criterion 1: Exhaustion is no longer success
    case_c1 = cases.get("three_length_mismatches", {})
    c1_hr = case_c1.get("native_hresult", "").lower()
    c1_bytes = case_c1.get("output_bytes", 0)
    c1_last = case_c1.get("last", [])
    has_ghost = any(r.get("pid") == 0 and r.get("ct") == 0 for r in c1_last)
    # Fixed binary MUST return error (not 0x00000000) and publish no ghost buffer
    c1_passed = (c1_hr != "0x00000000") and (c1_bytes == 0 or not has_ghost)
    c1_defect_present = (c1_hr == "0x00000000")

    # Criterion 2: Third-call success is no longer out-of-memory
    case_c2 = cases.get("success_on_third", {})
    c2_hr = case_c2.get("native_hresult", "").lower()
    c2_bytes = case_c2.get("output_bytes", 0)
    # Fixed binary MUST return 0x00000000 with valid buffer > 0, NOT 0x8007000e
    c2_passed = (c2_hr == "0x00000000" and c2_bytes > 0)
    c2_defect_present = (c2_hr == "0x8007000e")

    # Criterion 3: Baseline and error propagation
    case_s2 = cases.get("success_on_second", {})
    case_ad = cases.get("access_denied_second", {})
    s2_hr = case_s2.get("native_hresult", "").lower()
    ad_hr = case_ad.get("native_hresult", "").lower()
    c3_passed = (s2_hr == "0x00000000" and case_s2.get("output_bytes", 0) > 0 and ad_hr == "0x80070005")

    # Overall verdict
    if c1_passed and c2_passed and c3_passed:
        verdict = "PASS"
        verdict_summary = "Both known QueryProcessInformation defects are FIXED in this binary."
    elif c1_defect_present and c2_defect_present and c3_passed:
        verdict = "DEFECTIVE_9549_CONFIRMED"
        verdict_summary = "Negative control confirmed: both defects reproduced as expected on 9549."
    else:
        verdict = "FAIL"
        verdict_summary = f"Unexpected failure: C1 passed={c1_passed}, C2 passed={c2_passed}, C3 passed={c3_passed}"

    return {
        "verdict": verdict,
        "verdict_summary": verdict_summary,
        "criteria": {
            "criterion_1_exhaustion_not_success": {
                "passed": c1_passed,
                "defect_present": c1_defect_present,
                "observed_hresult": c1_hr,
                "published_bytes": c1_bytes,
                "ghost_record_observed": has_ghost,
                "description": "With three consecutive 0xC0000004 returns, returns error HRESULT and publishes no buffer.",
            },
            "criterion_2_third_call_success_not_oom": {
                "passed": c2_passed,
                "defect_present": c2_defect_present,
                "observed_hresult": c2_hr,
                "published_bytes": c2_bytes,
                "description": "With length mismatches on calls 1-2 and success on call 3, returns S_OK (not E_OUTOFMEMORY 0x8007000E).",
            },
            "criterion_3_baseline_and_error_propagation": {
                "passed": c3_passed,
                "second_call_success_hresult": s2_hr,
                "access_denied_propagated_hresult": ad_hr,
                "description": "Second call success returns S_OK; access denied 0xC0000022 propagates as 0x80070005.",
            },
        },
    }


def compare_with_reference(receipt: dict, reference_path: Path) -> dict:
    """Compares controlled results with the 2026-10-21 reference JSON."""
    if not reference_path.is_file():
        return {"compared": False, "reason": f"Reference file not found: {reference_path}"}
    try:
        ref = json.loads(reference_path.read_text(encoding="utf-8"))
        ref_cases = {c["case"]: c for c in ref.get("cases", [])}
        cur_cases = {c["case"]: c for c in receipt.get("controlled_results", {}).get("cases", [])}

        mismatches = []
        for name, ref_c in ref_cases.items():
            if name not in cur_cases:
                mismatches.append(f"Missing case {name}")
                continue
            cur_c = cur_cases[name]
            if cur_c["native_hresult"].lower() != ref_c["native_hresult"].lower():
                mismatches.append(f"{name} native_hresult: {cur_c['native_hresult']} vs ref {ref_c['native_hresult']}")
            if cur_c["output_bytes"] != ref_c["output_bytes"]:
                mismatches.append(f"{name} output_bytes: {cur_c['output_bytes']} vs ref {ref_c['output_bytes']}")

        return {
            "compared": True,
            "reference_file": str(reference_path.resolve()),
            "reference_sha256": ref.get("dll_sha256"),
            "matches_reference_100_percent": (len(mismatches) == 0),
            "mismatches": mismatches,
        }
    except Exception as exc:
        return {"compared": False, "error": str(exc)}


# -----------------------------------------------------------------------------
# Main Runner Orchestrator
# -----------------------------------------------------------------------------

def generate_receipt_markdown(receipt: dict) -> str:
    eval_res = receipt.get("evaluation", {})
    criteria = eval_res.get("criteria", {})
    c1 = criteria.get("criterion_1_exhaustion_not_success", {})
    c2 = criteria.get("criterion_2_third_call_success_not_oom", {})
    c3 = criteria.get("criterion_3_baseline_and_error_propagation", {})
    ref = receipt.get("reference_comparison", {})
    meta = receipt.get("target_dll", {})
    sym = receipt.get("symbol_resolution", {})

    md = f"""# TaskManagerDataLayer.dll Query Fix Verification Receipt

- **Date (UTC)**: {receipt.get('timestamp_utc')}
- **Target DLL**: `{meta.get('path')}`
- **SHA-256**: `{meta.get('sha256')}`
- **Component Store Version**: `{meta.get('component_store_version') or 'N/A'}`
- **Verdict**: **{eval_res.get('verdict')}** ({eval_res.get('verdict_summary')})

## Symbol & Import Resolution
- **Symbol Source**: `{sym.get('symbol_path')}`
- **QueryProcessInformation**: `{sym.get('query_symbol')}` (RVA: `{sym.get('query_rva')}`)
- **_Tidy**: `{sym.get('tidy_symbol')}` (RVA: `{sym.get('tidy_rva')}`)
- **NtQuerySystemInformation Import Slot**: `{receipt.get('import_resolution', {}).get('slot_rva')}` (`{receipt.get('import_resolution', {}).get('dll')}`)

## Acceptance Criteria Evaluation (Goal 1)
| Criterion | Goal Expectation | Observed Behavior | Status |
|---|---|---|---|
| **1. Exhaustion** | Error HRESULT, no buffer published | `{c1.get('observed_hresult')}` ({c1.get('published_bytes')} bytes, ghost={c1.get('ghost_record_observed')}) | {'PASS' if c1.get('passed') else 'FAIL (Defect Present)'} |
| **2. Third-call success** | `S_OK` (0x00000000) with snapshot | `{c2.get('observed_hresult')}` ({c2.get('published_bytes')} bytes) | {'PASS' if c2.get('passed') else 'FAIL (Defect Present)'} |
| **3. Baseline & propagation** | Call 2 success -> `S_OK`; 0xC0000022 -> 0x80070005 | Success: `{c3.get('second_call_success_hresult')}`, Denied: `{c3.get('access_denied_propagated_hresult')}` | {'PASS' if c3.get('passed') else 'FAIL'} |

## Reference Comparison (10-21 native-controlled.json)
- **Reference File**: `{ref.get('reference_file')}`
- **100% Match**: **{ref.get('matches_reference_100_percent')}**
"""
    return md


def main():
    parser = argparse.ArgumentParser(description="Verify TaskManagerDataLayer.dll query defects via controlled execution")
    parser.add_argument("--dll", type=Path, default=DEFAULT_DLL, help="Path to TaskManagerDataLayer.dll")
    parser.add_argument("--symbols", type=str, default=DEFAULT_SYMBOLS, help="Symbol search path for cdb")
    parser.add_argument("--cdb", type=Path, default=None, help="Explicit path to cdb.exe")
    parser.add_argument("--output", type=Path, default=None, help="Directory or JSON file path to write receipt")
    parser.add_argument("--reference", type=Path, default=REFERENCE_JSON, help="Path to 10-21 reference JSON")
    parser.add_argument("--real-kernel", action="store_true", help="Also run real-kernel query test under process churn")
    parser.add_argument("--churn-seconds", type=int, default=5, help="Duration for real-kernel test in seconds")
    # Internal worker flags
    parser.add_argument("--worker-controlled", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-real-kernel", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--query-rva", type=str, help=argparse.SUPPRESS)
    parser.add_argument("--tidy-rva", type=str, help=argparse.SUPPRESS)
    parser.add_argument("--slot-rva", type=str, help=argparse.SUPPRESS)

    args = parser.parse_args()

    # Worker dispatch
    if args.worker_controlled:
        res = run_controlled_worker(
            args.dll, int(args.query_rva, 16), int(args.tidy_rva, 16), int(args.slot_rva, 16)
        )
        print(json.dumps(res))
        sys.exit(0)

    if args.worker_real_kernel:
        res = run_real_kernel_worker(
            args.dll, int(args.query_rva, 16), int(args.tidy_rva, 16), int(args.slot_rva, 16),
            seconds=args.churn_seconds
        )
        print(json.dumps(res))
        sys.exit(0)

    # Parent runner orchestrator
    dll_path = args.dll.resolve()
    if not dll_path.is_file():
        sys.exit(f"Target DLL not found: {dll_path}")

    print(f"[*] Analyzing target DLL: {dll_path}")
    meta = get_dll_version_and_metadata(dll_path)
    print(f"    SHA-256: {meta['sha256']}")
    if meta.get("component_store_version"):
        print(f"    Component store version: {meta['component_store_version']}")

    # 1. Resolve symbols via public symbols (fails if unresolved)
    print("[*] Resolving public symbols via cdb...")
    sym_info = resolve_public_symbols(dll_path, symbol_path=args.symbols, cdb_path=args.cdb)
    print(f"    QueryProcessInformation: {sym_info['query_symbol']} -> RVA {sym_info['query_rva']}")
    print(f"    _Tidy:                   {sym_info['tidy_symbol']} -> RVA {sym_info['tidy_rva']}")

    # 2. Dynamically resolve NtQSI IAT slot from PE imports
    print("[*] Resolving NtQuerySystemInformation import slot dynamically from PE imports...")
    slot_rva, imp_dll, imp_fn = find_ntqsi_import_slot(dll_path.read_bytes())
    slot_rva_hex = hex(slot_rva)
    print(f"    NtQSI IAT slot: {slot_rva_hex} ({imp_dll}!{imp_fn})")

    # 3. Spawn isolated child process for controlled execution
    print("[*] Spawning isolated child process for controlled execution...")
    child_cmd = [
        sys.executable, str(Path(__file__).resolve()),
        "--worker-controlled",
        "--dll", str(dll_path),
        "--query-rva", sym_info['query_rva'],
        "--tidy-rva", sym_info['tidy_rva'],
        "--slot-rva", slot_rva_hex,
    ]
    proc = subprocess.run(child_cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        sys.exit(f"Controlled child worker failed ({proc.returncode}):\n{proc.stderr}")

    try:
        controlled_results = json.loads(proc.stdout)
    except Exception as exc:
        sys.exit(f"Failed to parse child worker output as JSON: {exc}\nOutput: {proc.stdout}")

    # 4. Optional real-kernel run
    real_kernel_results = None
    if args.real_kernel:
        print(f"[*] Spawning child process for real-kernel churn run ({args.churn_seconds}s)...")
        churn_cmd = [
            sys.executable, str(Path(__file__).resolve()),
            "--worker-real-kernel",
            "--dll", str(dll_path),
            "--query-rva", sym_info['query_rva'],
            "--tidy-rva", sym_info['tidy_rva'],
            "--slot-rva", slot_rva_hex,
            "--churn-seconds", str(args.churn_seconds),
        ]
        proc_churn = subprocess.run(churn_cmd, capture_output=True, text=True, timeout=args.churn_seconds + 30)
        if proc_churn.returncode == 0:
            try:
                real_kernel_results = json.loads(proc_churn.stdout)
            except Exception:
                pass

    # 5. Evaluate Goal 1 criteria
    print("[*] Evaluating Goal 1 acceptance criteria...")
    evaluation = evaluate_criteria(controlled_results)
    print(f"    Verdict: {evaluation['verdict']}")
    print(f"    Summary: {evaluation['verdict_summary']}")

    # 6. Compare with reference
    ref_comp = compare_with_reference({"controlled_results": controlled_results}, args.reference)
    if ref_comp.get("compared"):
        print(f"[*] Comparison with 10-21 reference JSON: {'100% MATCH' if ref_comp['matches_reference_100_percent'] else 'MISMATCH'}")

    # 7. Assemble Receipt
    receipt = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "tool": "tools/verify-query-fix.py",
        "target_dll": meta,
        "symbol_resolution": sym_info,
        "import_resolution": {
            "slot_rva": slot_rva_hex,
            "dll": imp_dll,
            "function": imp_fn,
        },
        "controlled_results": controlled_results,
        "real_kernel_results": real_kernel_results,
        "evaluation": evaluation,
        "reference_comparison": ref_comp,
    }

    # 8. Output handling
    out_dir = args.output
    if out_dir is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = Path(r"D:\TaskmgrFreezeCaptures\fix-verification") / f"negative-control-9549-{stamp}"

    if out_dir.suffix.lower() == ".json":
        out_json = out_dir
        out_md = out_dir.with_suffix(".md")
        out_json.parent.mkdir(parents=True, exist_ok=True)
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_json = out_dir / "receipt.json"
        out_md = out_dir / "summary.md"

    out_json.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    out_md.write_text(generate_receipt_markdown(receipt), encoding="utf-8")
    print(f"[*] Receipt written to: {out_json}")
    print(f"[*] Summary written to: {out_md}")

    # If verifying a candidate and failed, exit code indicates verdict
    if evaluation["verdict"] == "PASS":
        sys.exit(0)
    elif evaluation["verdict"] == "DEFECTIVE_9549_CONFIRMED":
        # Negative control successfully confirmed the defects!
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
