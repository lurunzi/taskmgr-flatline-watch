"""Goal 1 and Goal 2 automation for Task Manager CPU Flatline Watch (J16).

Automates verification and tracking across Windows Update cycles:
1. Goal 1: Upon TaskManagerDataLayer.dll SHA-256 change:
   - Copies new DLL & matching PDB into fix-verification/dll-<version>/ with MANIFEST.json.
   - Runs tools/verify-query-fix.py in an isolated child process against the new file.
   - Runs negative control against the archived dll-9549 file.
   - Evaluates criteria (exhaustion, 3rd-call success, baseline propagation, negative control).
   - Writes structured receipt to fix-verification/goal1-<timestamp>/.
   - Explicitly records Criterion 5 real-kernel churn run as unexecuted / deferred to manual run.
   - If failed, generates Feedback Hub summary in the receipt directory.
   - Updates status.json with goal1 block ('waiting_for_update' / 'passed' / 'failed').

2. Goal 2: Once Goal 1 passes, automatically starts tracking:
   - Calendar days (>= 14 required).
   - Effective monitoring hours (>= 150.0 required, derived from daily-monitoring.json).
   - High-load windows (>= 20 windows with >= 50 process starts in 2 seconds).
   - TaskManagerDataLayer.dll 0x8007139F native aggregation errors (must be exactly 0).
   - Confirmed flatlines (must be exactly 0).
   - ProcessDataSource2.cpp:821 0x8007000E errors (tracked and reported only).
   - Persists state to goal2.json and status.json.
   - Sets status to 'goal2_passed' when all criteria are met, prompting the owner to perform Goal 3.
   - Immediately sets 'goal2_failed' if any aggregation error or flatline occurs.

3. Goal 3 boundary constraint:
   - Never automatically unregisters scheduled tasks, never stops the watcher, and never deletes evidence.
"""
from collections import deque
from datetime import datetime, timezone, date
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import threading
import time
import winreg

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SYMBOLS_CACHE = Path(r"C:\tmp\TaskmgrCpuDiag-20260928-194418\Symbols")
DEFAULT_NEGATIVE_CONTROL_DLL = Path(r"D:\TaskmgrFreezeCaptures\fix-verification\dll-9549\TaskManagerDataLayer.dll")


def get_os_build() -> str:
    """Retrieves OS CurrentBuild.UBR from Windows registry, falling back to platform.version()."""
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
        build = winreg.QueryValueEx(k, "CurrentBuild")[0]
        ubr = winreg.QueryValueEx(k, "UBR")[0]
        return f"{build}.{ubr}"
    except Exception:
        return platform.version()


def extract_pdb_info(dll_bytes: bytes) -> tuple[str | None, str | None]:
    """Extracts CodeView RSDS PDB filename and symbol key (GUID+age) from PE bytes."""
    try:
        pe = struct.unpack_from('<I', dll_bytes, 0x3c)[0]
        dbg_rva, dbg_size = struct.unpack_from('<II', dll_bytes, pe + 24 + 112 + 8 * 6)
        count = struct.unpack_from('<H', dll_bytes, pe + 6)[0]
        optional = struct.unpack_from('<H', dll_bytes, pe + 20)[0]
        dbg_off = None
        for n in range(count):
            at = pe + 24 + optional + n * 40
            vsize, vaddr, rsize, roff = struct.unpack_from('<IIII', dll_bytes, at + 8)
            if vaddr <= dbg_rva < vaddr + min(vsize, rsize):
                dbg_off = roff + (dbg_rva - vaddr)
                break
        if dbg_off is None:
            return None, None
        for i in range(dbg_size // 28):
            rec = dll_bytes[dbg_off + i * 28 : dbg_off + (i + 1) * 28]
            typ, sz, raddr, pdata = struct.unpack_from('<4x4x2x2xIIII', rec)
            if typ == 2:  # IMAGE_DEBUG_TYPE_CODEVIEW
                cv_sig = dll_bytes[pdata : pdata + 4]
                if cv_sig == b'RSDS':
                    d1, d2, d3 = struct.unpack_from('<IHH', dll_bytes, pdata + 4)
                    d4 = dll_bytes[pdata + 12 : pdata + 20]
                    age = struct.unpack_from('<I', dll_bytes, pdata + 20)[0]
                    pdb_name = dll_bytes[pdata + 24 :].split(b'\x00')[0].decode('utf-8', errors='replace')
                    pdb_key = f"{d1:08X}{d2:04X}{d3:04X}{d4.hex().upper()}{age:X}"
                    return pdb_name, pdb_key
    except Exception:
        pass
    return None, None


def generate_feedback_hub_summary(candidate_meta: dict, eval_res: dict, neg_res: dict) -> str:
    """Formats an English text summary of Goal 1 failure suitable for Microsoft Feedback Hub."""
    criteria = eval_res.get("criteria", {})
    c1 = criteria.get("criterion_1_exhaustion_not_success", {})
    c2 = criteria.get("criterion_2_third_call_success_not_oom", {})
    c3 = criteria.get("criterion_3_baseline_and_error_propagation", {})

    lines = [
        "Task Manager CPU Flatline Fix Verification (Goal 1): Microsoft Feedback Hub Submission Summary",
        "",
        f"- Target File: {candidate_meta.get('path', 'TaskManagerDataLayer.dll')}",
        f"- SHA-256: {candidate_meta.get('sha256')}",
        f"- Component Store Version: {candidate_meta.get('component_store_version') or 'N/A'}",
        f"- OS Build: {candidate_meta.get('os_build') or get_os_build()}",
        f"- Verification Timestamp (UTC): {datetime.now(timezone.utc).isoformat()}",
        f"- Harness: tools/verify-query-fix.py (controlled execution of native QueryProcessInformation)",
        "",
        "Controlled Execution Acceptance Criteria Results:",
        f"1. Query Exhaustion (3 consecutive 0xC0000004):",
        f"   - Observed Return: {c1.get('observed_hresult')}",
        f"   - Published Bytes: {c1.get('published_bytes')}",
        f"   - Ghost Record Observed: {c1.get('ghost_record_observed')}",
        f"   - Status: {'PASS' if c1.get('passed') else 'FAIL (Defect Present: returned success/buffer on exhausted retries)'}",
        "",
        f"2. Third-Call Success (0xC0000004, 0xC0000004, then 0x00000000):",
        f"   - Observed Return: {c2.get('observed_hresult')}",
        f"   - Published Bytes: {c2.get('published_bytes')}",
        f"   - Status: {'PASS' if c2.get('passed') else 'FAIL (Defect Present: returned E_OUTOFMEMORY 0x8007000E on 3rd call success)'}",
        "",
        f"3. Baseline & Error Propagation:",
        f"   - Second-call Success Return: {c3.get('second_call_success_hresult')}",
        f"   - Access Denied (0xC0000022) Return: {c3.get('access_denied_propagated_hresult')}",
        f"   - Status: {'PASS' if c3.get('passed') else 'FAIL'}",
        "",
        f"4. Negative Control (dll-9549 reference binary):",
        f"   - Verdict: {neg_res.get('verdict')}",
        f"   - Negative Control Valid: {neg_res.get('verdict') == 'DEFECTIVE_9549_CONFIRMED'}",
        "",
        "Verdict: DEFECTS REMAIN PRESENT IN THIS BINARY.",
        "The updated TaskManagerDataLayer.dll has not resolved the known QueryProcessInformation loop defects.",
    ]
    return "\n".join(lines) + "\n"


class Goal1Runner:
    """Executes Goal 1 verification when TaskManagerDataLayer.dll changes."""

    def __init__(self, output_dir: Path, symbols_cache: Path | None = None,
                 negative_control_path: Path | None = None,
                 verify_script: Path | None = None):
        self.output_dir = Path(output_dir)
        self.symbols_cache = Path(symbols_cache) if symbols_cache else DEFAULT_SYMBOLS_CACHE
        self.negative_control_path = Path(negative_control_path) if negative_control_path else DEFAULT_NEGATIVE_CONTROL_DLL
        self.verify_script = Path(verify_script) if verify_script else (ROOT / "tools" / "verify-query-fix.py")

    def run_goal1(self, candidate_dll_path: Path, candidate_meta: dict | None = None,
                  mock_candidate_result: dict | None = None,
                  mock_negative_result: dict | None = None) -> dict:
        """Runs Goal 1 verification end-to-end on a new candidate DLL."""
        candidate_dll_path = Path(candidate_dll_path).resolve()
        if not candidate_dll_path.is_file():
            raise FileNotFoundError(f"Candidate DLL not found: {candidate_dll_path}")

        raw_bytes = candidate_dll_path.read_bytes()
        sha256 = hashlib.sha256(raw_bytes).hexdigest()
        os_build = get_os_build()

        meta = dict(candidate_meta or {})
        meta["path"] = str(candidate_dll_path)
        meta["sha256"] = sha256
        meta["size_bytes"] = len(raw_bytes)
        meta["os_build"] = os_build

        # Determine version tag for folder name
        version_str = meta.get("component_store_version") or meta.get("file_version") or sha256[:8]
        if "." in version_str:
            version_tag = version_str.split(".")[-1]
        else:
            version_tag = version_str

        # 1. Archive new DLL and matching PDB into fix-verification/dll-<version>/
        fix_dir = self.output_dir / "fix-verification"
        fix_dir.mkdir(parents=True, exist_ok=True)
        archive_dir = fix_dir / f"dll-{version_tag}"
        archive_dir.mkdir(parents=True, exist_ok=True)

        dest_dll = archive_dir / "TaskManagerDataLayer.dll"
        if dest_dll.resolve() != candidate_dll_path:
            shutil.copy2(candidate_dll_path, dest_dll)

        # Locate PDB
        pdb_name, pdb_key = extract_pdb_info(raw_bytes)
        pdb_present = False
        if pdb_name and pdb_key:
            # Check local symbol cache
            cached_pdb = self.symbols_cache / pdb_name / pdb_key / pdb_name
            if cached_pdb.is_file():
                dest_sym_dir = archive_dir / "symbols" / pdb_name / pdb_key
                dest_sym_dir.mkdir(parents=True, exist_ok=True)
                dest_pdb = dest_sym_dir / pdb_name
                shutil.copy2(cached_pdb, dest_pdb)
                # Also place direct copy in symbols/
                shutil.copy2(cached_pdb, archive_dir / "symbols" / pdb_name)
                pdb_present = True

        manifest = {
            "file": "TaskManagerDataLayer.dll",
            "source": str(candidate_dll_path),
            "sha256": sha256,
            "component_store_version": meta.get("component_store_version") or version_str,
            "os_build": os_build,
            "copied_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "pdb": pdb_name or "TaskManagerDataLayer.pdb",
            "pdb_key": pdb_key,
            "pdb_present": pdb_present,
            "purpose": "Post-fix candidate copy for goal 1 verification and goal 2 monitoring."
        }
        manifest_path = archive_dir / "MANIFEST.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        # 2. Create receipt directory: fix-verification/goal1-<timestamp>/
        timestamp_slug = datetime.now().strftime("%Y%m%d-%H%M%S")
        receipt_dir = fix_dir / f"goal1-{timestamp_slug}"
        receipt_dir.mkdir(parents=True, exist_ok=True)

        candidate_out = receipt_dir / "candidate-receipt.json"
        neg_out = receipt_dir / "negative-control-receipt.json"

        # 3. Execute candidate verification in isolated subprocess
        if mock_candidate_result is not None:
            candidate_receipt = mock_candidate_result
            candidate_out.write_text(json.dumps(candidate_receipt, indent=2), encoding="utf-8")
        else:
            cmd_cand = [
                sys.executable, str(self.verify_script),
                "--dll", str(dest_dll),
                "--output", str(candidate_out)
            ]
            proc_cand = subprocess.run(cmd_cand, capture_output=True, text=True, timeout=180)
            if candidate_out.is_file():
                candidate_receipt = json.loads(candidate_out.read_text(encoding="utf-8"))
            else:
                candidate_receipt = {
                    "evaluation": {"verdict": "FAIL", "verdict_summary": f"Process exited {proc_cand.returncode}: {proc_cand.stderr}"}
                }

        # 4. Execute negative control against dll-9549 in isolated subprocess
        if mock_negative_result is not None:
            neg_receipt = mock_negative_result
            neg_out.write_text(json.dumps(neg_receipt, indent=2), encoding="utf-8")
        else:
            if not self.negative_control_path.is_file():
                neg_receipt = {
                    "evaluation": {"verdict": "ERROR", "verdict_summary": f"Negative control DLL not found: {self.negative_control_path}"}
                }
            else:
                cmd_neg = [
                    sys.executable, str(self.verify_script),
                    "--dll", str(self.negative_control_path),
                    "--output", str(neg_out)
                ]
                proc_neg = subprocess.run(cmd_neg, capture_output=True, text=True, timeout=180)
                if neg_out.is_file():
                    neg_receipt = json.loads(neg_out.read_text(encoding="utf-8"))
                else:
                    neg_receipt = {
                        "evaluation": {"verdict": "FAIL", "verdict_summary": f"Process exited {proc_neg.returncode}: {proc_neg.stderr}"}
                    }

        # 5. Evaluate Goal 1 overall verdict
        cand_eval = candidate_receipt.get("evaluation", {})
        cand_verdict = cand_eval.get("verdict")
        neg_eval = neg_receipt.get("evaluation", {})
        neg_verdict = neg_eval.get("verdict")

        cand_criteria = cand_eval.get("criteria", {})
        c1_passed = cand_criteria.get("criterion_1_exhaustion_not_success", {}).get("passed", False)
        c2_passed = cand_criteria.get("criterion_2_third_call_success_not_oom", {}).get("passed", False)
        c3_passed = cand_criteria.get("criterion_3_baseline_and_error_propagation", {}).get("passed", False)
        neg_passed = (neg_verdict == "DEFECTIVE_9549_CONFIRMED")

        if cand_verdict == "PASS" and neg_passed:
            goal1_status = "passed"
            goal1_verdict = "PASS"
            summary_desc = "Goal 1 verification passed: both defects resolved; negative control confirmed."
        else:
            goal1_status = "failed"
            goal1_verdict = "FAIL"
            summary_desc = f"Goal 1 verification failed (candidate: {cand_verdict}, negative control: {neg_verdict})."

        # 6. Top-level Goal 1 Receipt
        goal1_receipt = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "goal": "Goal 1: binary fix confirmed by controlled execution",
            "verdict": goal1_verdict,
            "status": goal1_status,
            "verdict_summary": summary_desc,
            "candidate_meta": meta,
            "archive_dir": str(archive_dir),
            "manifest_file": str(manifest_path),
            "criteria_summary": {
                "criterion_1_exhaustion": c1_passed,
                "criterion_2_third_call_success": c2_passed,
                "criterion_3_baseline_propagation": c3_passed,
                "criterion_4_negative_control": neg_passed,
                "criterion_5_real_kernel": {
                    "executed": False,
                    "note": "Real-kernel churn run deferred to separate manual execution per J16 specification."
                }
            },
            "candidate_evaluation": cand_eval,
            "negative_control_evaluation": neg_eval,
            "candidate_receipt_file": str(candidate_out),
            "negative_control_receipt_file": str(neg_out)
        }

        receipt_file = receipt_dir / "receipt.json"
        receipt_file.write_text(json.dumps(goal1_receipt, indent=2), encoding="utf-8")

        summary_md = f"""# Goal 1 Fix Verification Receipt

- **Timestamp (UTC)**: `{goal1_receipt['timestamp_utc']}`
- **Target DLL**: `{meta.get('path')}`
- **SHA-256**: `{meta.get('sha256')}`
- **Component Store / Version**: `{meta.get('component_store_version') or version_str}`
- **Verdict**: **{goal1_verdict}** ({goal1_status})
- **Summary**: {summary_desc}

## Criteria Summary
| Criterion | Status | Detail |
|---|---|---|
| **1. Exhaustion** | {'PASS' if c1_passed else 'FAIL'} | Error HRESULT on 3x 0xC0000004 |
| **2. Third-call success** | {'PASS' if c2_passed else 'FAIL'} | S_OK with snapshot, not 0x8007000E |
| **3. Baseline & propagation** | {'PASS' if c3_passed else 'FAIL'} | Second call S_OK, 0xC0000022 -> 0x80070005 |
| **4. Negative control (9549)** | {'PASS' if neg_passed else 'FAIL'} | Confirmed defects reproduced on 9549 |
| **5. Real-kernel churn** | DEFERRED | Explicitly deferred to separate manual execution per J16 specification |

## Artifacts
- **Candidate Archive**: `{archive_dir}`
- **Candidate Receipt**: `{candidate_out}`
- **Negative Control Receipt**: `{neg_out}`
"""
        summary_md_path = receipt_dir / "summary.md"
        summary_md_path.write_text(summary_md, encoding="utf-8")

        # 7. Generate Feedback Hub summary on failure
        feedback_hub_file = None
        if goal1_status == "failed":
            feedback_hub_text = generate_feedback_hub_summary(meta, cand_eval, neg_eval)
            feedback_hub_path = receipt_dir / "feedback-hub-summary.txt"
            feedback_hub_path.write_text(feedback_hub_text, encoding="utf-8")
            feedback_hub_file = str(feedback_hub_path)

        return {
            "status": goal1_status,
            "verdict": goal1_verdict,
            "receipt_dir": str(receipt_dir),
            "receipt_file": str(receipt_file),
            "summary_md_file": str(summary_md_path),
            "feedback_hub_file": feedback_hub_file,
            "archive_dir": str(archive_dir),
            "sha256": sha256,
            "file_version": version_str
        }


class Goal2Tracker:
    """Tracks and evaluates Goal 2 metrics (14 days, 150h, 20 high-load windows, 0 errors, 0 flatlines)."""

    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.goal2_file = self.output_dir / "goal2.json"
        self.lock = threading.Lock()
        self.state = {
            "status": "not_started",
            "start_utc": None,
            "last_updated_utc": None,
            "target_dll_sha256": None,
            "target_dll_version": None,
            "calendar_days": 0,
            "effective_monitoring_seconds": 0.0,
            "effective_monitoring_hours": 0.0,
            "high_load_windows_count": 0,
            "aggregation_errors_0x8007139F_count": 0,
            "confirmed_flatlines_count": 0,
            "process_datasource2_821_count": 0,
            "criteria": {
                "calendar_days_ge_14": False,
                "effective_hours_ge_150": False,
                "high_load_windows_ge_20": False,
                "zero_aggregation_errors": True,
                "zero_flatlines": True
            },
            "ready_for_goal3": False,
            "summary_text_zh": "等待目标一完成",
            "summary_text_en": "Waiting for Goal 1",
            "high_load_windows": []
        }
        self.load()

    def load(self) -> None:
        """Loads persisted Goal 2 state if available."""
        with self.lock:
            if self.goal2_file.is_file():
                try:
                    data = json.loads(self.goal2_file.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        # Gating constraint: if start_utc is None/absent, Goal 1 has not passed yet,
                        # so status must be not_started with 0 counts.
                        if not data.get("start_utc"):
                            data["status"] = "not_started"
                            data["aggregation_errors_0x8007139F_count"] = 0
                            data["confirmed_flatlines_count"] = 0
                            data["process_datasource2_821_count"] = 0
                            if "criteria" in data:
                                data["criteria"]["zero_aggregation_errors"] = True
                                data["criteria"]["zero_flatlines"] = True
                        self.state.update(data)
                except Exception:
                    pass

    def save(self) -> None:
        """Atomically saves Goal 2 state to goal2.json."""
        with self.lock:
            self.state["last_updated_utc"] = datetime.now(timezone.utc).isoformat()
            data = dict(self.state)
        text = json.dumps(data, indent=2) + "\n"
        temp = self.goal2_file.with_suffix(".tmp")
        try:
            temp.write_text(text, encoding="utf-8")
            temp.replace(self.goal2_file)
        except Exception:
            pass

    def start_counting(self, target_dll_info: dict) -> None:
        """Starts Goal 2 accumulation upon Goal 1 pass."""
        with self.lock:
            now_utc = datetime.now(timezone.utc).isoformat()
            self.state.update({
                "status": "accumulating",
                "start_utc": now_utc,
                "last_updated_utc": now_utc,
                "target_dll_sha256": target_dll_info.get("sha256"),
                "target_dll_version": target_dll_info.get("file_version") or target_dll_info.get("component_store_version"),
                "calendar_days": 0,
                "effective_monitoring_seconds": 0.0,
                "effective_monitoring_hours": 0.0,
                "high_load_windows_count": 0,
                "aggregation_errors_0x8007139F_count": 0,
                "confirmed_flatlines_count": 0,
                "process_datasource2_821_count": 0,
                "criteria": {
                    "calendar_days_ge_14": False,
                    "effective_hours_ge_150": False,
                    "high_load_windows_ge_20": False,
                    "zero_aggregation_errors": True,
                    "zero_flatlines": True
                },
                "ready_for_goal3": False,
                "high_load_windows": []
            })
        self.evaluate_criteria()
        self.save()

    def accumulate_effective_seconds(self, delta_sec: float) -> None:
        """Accumulates effective monitoring seconds from J5 daily monitoring."""
        with self.lock:
            if self.state.get("status") != "accumulating":
                return
            self.state["effective_monitoring_seconds"] = round(
                self.state.get("effective_monitoring_seconds", 0.0) + delta_sec, 2
            )
            self.state["effective_monitoring_hours"] = round(
                self.state["effective_monitoring_seconds"] / 3600.0, 2
            )
        self.evaluate_criteria()

    def record_high_load_window(self, mono_ts: float | None = None, process_count: int = 50) -> None:
        """Records a 2-second high-load window (>= 50 process starts)."""
        with self.lock:
            if self.state.get("status") != "accumulating":
                return
            self.state["high_load_windows_count"] = self.state.get("high_load_windows_count", 0) + 1
            entry = {
                "utc": datetime.now(timezone.utc).isoformat(),
                "process_count": process_count
            }
            windows = self.state.setdefault("high_load_windows", [])
            windows.append(entry)
            if len(windows) > 200:
                self.state["high_load_windows"] = windows[-200:]
        self.evaluate_criteria()
        self.save()

    def record_aggregation_error(self, event_info: dict | None = None) -> None:
        """Records a 0x8007139F native aggregation error (immediately fails Goal 2)."""
        with self.lock:
            if self.state.get("status") != "accumulating":
                return
            self.state["aggregation_errors_0x8007139F_count"] = (
                self.state.get("aggregation_errors_0x8007139F_count", 0) + 1
            )
            self.state["status"] = "goal2_failed"
            self.state["criteria"]["zero_aggregation_errors"] = False
        self.evaluate_criteria()
        self.save()

    def record_confirmed_flatline(self, incident_folder: str | Path | None = None) -> None:
        """Records a confirmed flatline recovery (immediately fails Goal 2)."""
        with self.lock:
            if self.state.get("status") != "accumulating":
                return
            self.state["confirmed_flatlines_count"] = (
                self.state.get("confirmed_flatlines_count", 0) + 1
            )
            self.state["status"] = "goal2_failed"
            self.state["criteria"]["zero_flatlines"] = False
        self.evaluate_criteria()
        self.save()

    def record_process_datasource2_821(self) -> None:
        """Records a 0x8007000E error at ProcessDataSource2.cpp:821 (reported only)."""
        with self.lock:
            if self.state.get("status") != "accumulating":
                return
            self.state["process_datasource2_821_count"] = (
                self.state.get("process_datasource2_821_count", 0) + 1
            )
        self.save()

    def evaluate_criteria(self) -> dict:
        """Evaluates whether Goal 2 criteria are met."""
        with self.lock:
            status = self.state.get("status", "not_started")
            start_utc = self.state.get("start_utc")
            if not start_utc or status in ("not_started", "waiting_for_goal1"):
                self.state["status"] = "not_started"
                return dict(self.state)

            # Calculate calendar days
            try:
                start_dt = datetime.fromisoformat(start_utc).date()
                cur_dt = datetime.now(timezone.utc).date()
                cal_days = max(0, (cur_dt - start_dt).days)
            except Exception:
                cal_days = 0
            self.state["calendar_days"] = cal_days

            eff_hours = self.state.get("effective_monitoring_hours", 0.0)
            hl_windows = self.state.get("high_load_windows_count", 0)
            agg_errors = self.state.get("aggregation_errors_0x8007139F_count", 0)
            flatlines = self.state.get("confirmed_flatlines_count", 0)

            c_days = cal_days >= 14
            c_hours = eff_hours >= 150.0
            c_windows = hl_windows >= 20
            c_zero_errors = (agg_errors == 0)
            c_zero_flatlines = (flatlines == 0)

            self.state["criteria"] = {
                "calendar_days_ge_14": c_days,
                "effective_hours_ge_150": c_hours,
                "high_load_windows_ge_20": c_windows,
                "zero_aggregation_errors": c_zero_errors,
                "zero_flatlines": c_zero_flatlines
            }

            if not c_zero_errors or not c_zero_flatlines:
                self.state["status"] = "goal2_failed"
                self.state["ready_for_goal3"] = False
                self.state["summary_text_zh"] = (
                    f"目标二未通过：检测到异常（聚合错误: {agg_errors} 次，平线: {flatlines} 次）"
                )
                self.state["summary_text_en"] = (
                    f"Goal 2 failed: defects detected ({agg_errors} aggregation errors, {flatlines} flatlines)"
                )
            elif c_days and c_hours and c_windows and c_zero_errors and c_zero_flatlines:
                self.state["status"] = "goal2_passed"
                self.state["ready_for_goal3"] = True
                self.state["summary_text_zh"] = (
                    f"目标二已达成（{cal_days}天 · {eff_hours:.1f}h · {hl_windows}窗口 · 0异常）：负责人可手动执行目标三退役监测器"
                )
                self.state["summary_text_en"] = (
                    f"Goal 2 passed ({cal_days}d · {eff_hours:.1f}h · {hl_windows} windows · 0 errors): owner may manually perform Goal 3 retirement"
                )
            else:
                self.state["status"] = "accumulating"
                self.state["ready_for_goal3"] = False
                self.state["summary_text_zh"] = (
                    f"目标二统计中（{cal_days}/14天 · {eff_hours:.1f}/150h · {hl_windows}/20窗口 · 0异常）"
                )
                self.state["summary_text_en"] = (
                    f"Goal 2 accumulating ({cal_days}/14d · {eff_hours:.1f}/150h · {hl_windows}/20 windows · 0 errors)"
                )

            return dict(self.state)

    def get_status_block(self) -> dict:
        """Returns Goal 2 state block for status.json."""
        with self.lock:
            return {
                "status": self.state.get("status"),
                "start_utc": self.state.get("start_utc"),
                "last_updated_utc": self.state.get("last_updated_utc"),
                "target_dll_sha256": self.state.get("target_dll_sha256"),
                "target_dll_version": self.state.get("target_dll_version"),
                "calendar_days": self.state.get("calendar_days", 0),
                "effective_monitoring_hours": self.state.get("effective_monitoring_hours", 0.0),
                "high_load_windows_count": self.state.get("high_load_windows_count", 0),
                "aggregation_errors_0x8007139F_count": self.state.get("aggregation_errors_0x8007139F_count", 0),
                "confirmed_flatlines_count": self.state.get("confirmed_flatlines_count", 0),
                "process_datasource2_821_count": self.state.get("process_datasource2_821_count", 0),
                "criteria": dict(self.state.get("criteria", {})),
                "ready_for_goal3": self.state.get("ready_for_goal3", False),
                "summary_text_zh": self.state.get("summary_text_zh", ""),
                "summary_text_en": self.state.get("summary_text_en", "")
            }


class GoalAutomationManager:
    """Coordinates Goal 1 execution and Goal 2 tracking within the Watcher daemon."""

    def __init__(self, output_dir: Path, monitor=None, symbols_cache: Path | None = None,
                 negative_control_path: Path | None = None):
        self.output_dir = Path(output_dir)
        self.monitor = monitor
        self.runner = Goal1Runner(self.output_dir, symbols_cache=symbols_cache,
                                  negative_control_path=negative_control_path)
        self.tracker = Goal2Tracker(self.output_dir)

        self.goal1_state = {
            "status": "waiting_for_update",
            "receipt_path": None,
            "receipt_file": None,
            "last_checked_utc": datetime.now(timezone.utc).isoformat(),
            "dll_sha256": None,
            "file_version": None,
            "verdict": None
        }

        # Real-time high-load sliding window
        self.process_start_timestamps = deque()
        self.last_high_load_window_mono = 0.0
        self.lock = threading.Lock()

    def initialize(self, target_dll_info: dict) -> None:
        """Initializes Goal 1 and Goal 2 states on startup."""
        self.goal1_state["dll_sha256"] = target_dll_info.get("sha256")
        self.goal1_state["file_version"] = (
            target_dll_info.get("file_version") or target_dll_info.get("component_store_version")
        )
        self.goal1_state["last_checked_utc"] = datetime.now(timezone.utc).isoformat()

        # Check if an existing Goal 1 receipt exists for this DLL
        self._check_existing_receipt(target_dll_info.get("sha256"))

        # If Goal 1 passed and Goal 2 has not started, start Goal 2 accumulation
        if self.goal1_state.get("status") == "passed" and self.tracker.state.get("status") == "not_started":
            self.tracker.start_counting(target_dll_info)

        # If DLL changed on startup, trigger Goal 1 check
        if target_dll_info.get("dll_changed"):
            self.check_dll_update(target_dll_info)

    def _check_existing_receipt(self, sha256: str | None) -> None:
        """Looks for an existing goal1 receipt in fix-verification matching sha256."""
        if not sha256:
            return
        fix_dir = self.output_dir / "fix-verification"
        if not fix_dir.is_dir():
            return
        receipts = sorted(fix_dir.glob("goal1-*/receipt.json"), reverse=True)
        for r_file in receipts:
            try:
                data = json.loads(r_file.read_text(encoding="utf-8"))
                c_meta = data.get("candidate_meta", {})
                if c_meta.get("sha256") == sha256:
                    self.goal1_state["status"] = data.get("status", "passed")
                    self.goal1_state["verdict"] = data.get("verdict", "PASS")
                    self.goal1_state["receipt_file"] = str(r_file)
                    self.goal1_state["receipt_path"] = str(r_file.parent)
                    break
            except Exception:
                pass

    def check_dll_update(self, target_dll_info: dict, dll_path: Path | None = None,
                         mock_candidate_result: dict | None = None,
                         mock_negative_result: dict | None = None) -> dict | None:
        """Called upon DLL hash change or hourly check."""
        sha = target_dll_info.get("sha256")
        self.goal1_state["last_checked_utc"] = datetime.now(timezone.utc).isoformat()
        self.goal1_state["dll_sha256"] = sha
        self.goal1_state["file_version"] = (
            target_dll_info.get("file_version") or target_dll_info.get("component_store_version")
        )

        if not target_dll_info.get("dll_changed") and mock_candidate_result is None:
            return None

        # Check if already verified for this exact hash
        if (self.goal1_state.get("status") in ("passed", "failed") and
                self.goal1_state.get("dll_sha256") == sha and mock_candidate_result is None):
            return None

        # Execute Goal 1 runner
        cand_path = Path(dll_path or target_dll_info.get("path") or
                         Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "TaskManagerDataLayer.dll")
        res = self.runner.run_goal1(
            cand_path, candidate_meta=target_dll_info,
            mock_candidate_result=mock_candidate_result,
            mock_negative_result=mock_negative_result
        )

        self.goal1_state["status"] = res["status"]
        self.goal1_state["verdict"] = res["verdict"]
        self.goal1_state["receipt_file"] = res["receipt_file"]
        self.goal1_state["receipt_path"] = res["receipt_dir"]

        # If Goal 1 passed, automatically start Goal 2 counting!
        if res["status"] == "passed":
            self.tracker.start_counting(target_dll_info)

        return res

    def on_process_start(self, event_mono: float) -> None:
        """Callback from RealtimeTrace for Kernel-Process ProcessStart events."""
        with self.lock:
            self.process_start_timestamps.append(event_mono)
            cutoff = event_mono - 2.0
            while self.process_start_timestamps and self.process_start_timestamps[0] < cutoff:
                self.process_start_timestamps.popleft()

            count = len(self.process_start_timestamps)
            if count >= 50:
                is_effective = self._is_monitor_effective()
                if is_effective and (event_mono - self.last_high_load_window_mono >= 2.0):
                    self.last_high_load_window_mono = event_mono
                    self.tracker.record_high_load_window(event_mono, count)

    def _is_monitor_effective(self) -> bool:
        """Determines if the watcher is currently in effective monitoring time."""
        if not self.monitor:
            return True
        st = getattr(self.monitor, "state", {})
        cur_state = st.get("state")
        has_taskmgr = bool(st.get("taskmgr_pid"))
        is_cpu_page = has_taskmgr and cur_state not in ("blind", "waiting")
        detector_valid = (cur_state != "invalid")
        native_ok = (getattr(self.monitor, "current_provider_gap", None) is None)
        return bool(is_cpu_page and detector_valid and native_ok)

    def on_native_error_0x8007139F(self, event_info: dict | None = None) -> None:
        """Callback from RealtimeTrace for 0x8007139F native freeze errors."""
        self.tracker.record_aggregation_error(event_info)

    def on_datasource2_821_error(self) -> None:
        """Callback from RealtimeTrace for ProcessDataSource2.cpp:821 0x8007000E errors."""
        self.tracker.record_process_datasource2_821()

    def record_confirmed_flatline(self, folder: str | Path | None = None) -> None:
        """Callback from Monitor upon confirming and recovering a flatline."""
        self.tracker.record_confirmed_flatline(folder)

    def accumulate_effective_seconds(self, delta_sec: float) -> None:
        """Accumulates effective monitoring seconds from Monitor.update_daily_monitoring."""
        self.tracker.accumulate_effective_seconds(delta_sec)

    def get_goal1_status(self) -> dict:
        """Returns goal1 status block for status.json."""
        with self.lock:
            return dict(self.goal1_state)

    def get_goal2_status(self) -> dict:
        """Returns goal2 status block for status.json."""
        return self.tracker.get_status_block()
