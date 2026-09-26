#!/usr/bin/env python3
"""Run actual framed NMT worker requests over hand-authored synthetic cases.

Records complete actual predictions for semantic review. Never point this tool
at private IME history. It does not attach to Fcitx or read personal input.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import statistics
import struct
import subprocess
import threading
import time

from check_eval_cases import load_cases


def proc_tree(pid):
    pending, seen = [pid], set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        try:
            pending.extend(int(x) for x in Path(f"/proc/{current}/task/{current}/children").read_text().split())
        except (OSError, ValueError):
            pass
    return seen


def sample_tree(pid):
    rss = pss = cpu = 0.0
    observed = []
    ticks = os.sysconf("SC_CLK_TCK")
    for current in proc_tree(pid):
        try:
            status = dict(line.split(":", 1) for line in Path(f"/proc/{current}/status").read_text().splitlines())
            stat = Path(f"/proc/{current}/stat").read_text().rsplit(")", 1)[1].split()
            cpu += (int(stat[11]) + int(stat[12])) / ticks
            rss += int(status.get("VmRSS", "0 kB").split()[0])
            try:
                rollup = dict(line.split(":", 1) for line in Path(f"/proc/{current}/smaps_rollup").read_text().splitlines() if ":" in line)
                pss += int(rollup["Pss"].split()[0])
            except (OSError, KeyError):
                pss = float("nan")
            observed.append(current)
        except (OSError, KeyError, ValueError):
            continue
    return {"rss_kib": rss, "pss_kib": pss if math.isfinite(pss) else None,
            "cpu_seconds": cpu, "pids": observed}


class PeakMonitor:
    def __init__(self, pid):
        self.pid, self.peak_rss, self.peak_pss = pid, 0, 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            s = sample_tree(self.pid)
            self.peak_rss = max(self.peak_rss, s["rss_kib"])
            self.peak_pss = max(self.peak_pss, s["pss_kib"] or 0)
            self.stop.wait(0.02)

    def finish(self):
        self.stop.set()
        self.thread.join()
        return {"sampled_peak_rss_kib": self.peak_rss,
                "sampled_peak_pss_kib": self.peak_pss, "sample_interval_ms": 20}


class Worker:
    def __init__(self, command):
        # Measure the worker's real thread/idle defaults, without benchmark-only
        # OpenMP tuning that would hide a desktop CPU regression.
        env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=env)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        os.set_blocking(self.proc.stdout.fileno(), False)
        self.sequence = 0

    def read_exact(self, size, deadline):
        data = bytearray()
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError("worker_response_timeout")
            part = os.read(self.proc.stdout.fileno(), size - len(data))
            if not part:
                raise EOFError("worker_stdout_closed")
            data.extend(part)
        return bytes(data)

    def request(self, case, context):
        self.sequence += 1
        data = json.dumps({"id": self.sequence, "source": case["source"],
                           "history": case["history"], "context": context}, ensure_ascii=False).encode()
        if len(data) > 65536:
            raise ValueError("request_frame_too_large")
        started = time.monotonic()
        self.proc.stdin.write(struct.pack("!I", len(data)) + data)
        self.proc.stdin.flush()
        deadline = started + 30
        size, = struct.unpack("!I", self.read_exact(4, deadline))
        if not 0 < size <= 65536:
            raise ValueError("response_frame_too_large")
        response = json.loads(self.read_exact(size, deadline))
        if response.get("id") != self.sequence:
            raise ValueError("response_identity_mismatch")
        elapsed = (time.monotonic() - started) * 1000
        return response, elapsed

    def close(self):
        self.selector.close()
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc.stdout.close()


def percentile95(values):
    return sorted(values)[max(0, math.ceil(len(values) * 0.95) - 1)] if values else None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--python", required=True)
    p.add_argument("--worker", type=Path, required=True)
    p.add_argument("--model", type=Path, required=True)
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--threads", type=int, choices=(1, 2), default=2)
    p.add_argument("--warm-requests", type=int, default=100)
    p.add_argument("--idle-seconds", type=float, default=60)
    p.add_argument("--record-synthetic", action="store_true", required=True,
                   help="Explicitly confirm these are synthetic cases whose predictions may be saved")
    a = p.parse_args()
    cases, errors = load_cases(a.cases)
    if errors:
        p.error("invalid synthetic case file: " + "; ".join(errors))
    if a.warm_requests < 1 or a.idle_seconds < 0:
        p.error("invalid measurement count")
    a.output_dir.mkdir(parents=True, exist_ok=True)
    if any(a.output_dir.iterdir()):
        p.error("output directory must be empty to preserve earlier measurements")
    command = [a.python, "-I", "-B", "-u", str(a.worker.resolve()), "--model-dir",
               str(a.model.resolve()), "--threads", str(a.threads)]
    summary = {"status": "running", "command": command, "case_count": len(cases),
               "planned_predictions": 2 * len(cases), "completed_predictions": 0,
               "semantic_status": "needs_independent_review",
               "cases_sha256": hashlib.sha256(a.cases.read_bytes()).hexdigest(),
               "worker_sha256": hashlib.sha256(a.worker.read_bytes()).hexdigest(),
               "cold_cache_note": "New process; OS page cache not dropped. Includes process startup and model load.",
               "scope": "Actual worker IPC; excludes Fcitx debounce and UI. Peak sampled, not exact hardware peak.",
               "errors": [], "threads": a.threads}
    metrics, predictions = [], {"baseline": [], "context": []}
    worker = monitor = None
    summary_path = a.output_dir / "summary.json"
    def checkpoint():
        summary["completion_ratio"] = summary["completed_predictions"] / summary["planned_predictions"]
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    checkpoint()
    try:
        cold_start = time.monotonic()
        worker = Worker(command)
        monitor = PeakMonitor(worker.proc.pid)
        response, _ = worker.request(cases[0], False)
        summary["cold_ms"] = (time.monotonic() - cold_start) * 1000
        summary["cold_response_ok"] = not response.get("error") and bool(response.get("translation"))
        summary["load_peak"] = monitor.finish()
        monitor = PeakMonitor(worker.proc.pid)
        print("Model loaded; running baseline and context cases", flush=True)
        for variant in predictions:
            for case in cases:
                response, elapsed = worker.request(case, variant == "context")
                valid = not response.get("error") and isinstance(response.get("translation"), str) and bool(response["translation"].strip())
                predictions[variant].append({"id": case["id"], "translation": response.get("translation", ""),
                                             "provenance": "actual_local_nmt_" + variant,
                                             "error": response.get("error"), "diagnostics": response.get("diagnostics", {})})
                metrics.append({"id": case["id"], "variant": variant, "source_chars": len(case["source"]),
                                "elapsed_ms": elapsed, "status": "ok" if valid else "failed"})
                summary["completed_predictions"] += int(valid)
                if not valid:
                    summary["errors"].append({"id": case["id"], "variant": variant, "error": response.get("error", "invalid_translation")})
                # Persist completed cases even if a later request is interrupted.
                with (a.output_dir / (variant + ".jsonl")).open("a", encoding="utf-8") as f:
                    f.write(json.dumps(predictions[variant][-1], ensure_ascii=False) + "\n")
            checkpoint()
            print(f"{variant}: {len(predictions[variant])}/{len(cases)} returned", flush=True)
        warm, failures = [], 0
        for i in range(a.warm_requests):
            response, elapsed = worker.request(cases[i % len(cases)], True)
            valid = not response.get("error") and bool(response.get("translation"))
            failures += int(not valid)
            if valid:
                warm.append(elapsed)
            metrics.append({"id": str(i), "variant": "warm_context", "source_chars": len(cases[i % len(cases)]["source"]),
                            "elapsed_ms": elapsed, "status": "ok" if valid else "failed"})
        summary["warm"] = {"requests": a.warm_requests, "valid": len(warm), "failed": failures,
                           "median_ms": statistics.median(warm) if warm else None, "p95_ms": percentile95(warm),
                           "unique_source_count": len({c["source"] for c in cases[:a.warm_requests]}),
                           "max_source_chars": max(len(c["source"]) for c in cases),
                           "min_source_chars": min(len(c["source"]) for c in cases)}
        summary["inference_peak"] = monitor.finish()
        monitor = None
        summary["hot_tree"] = sample_tree(worker.proc.pid)
        print(f"Warm requests complete; measuring {a.idle_seconds:g}s idle CPU", flush=True)
        checkpoint()
        before, started = sample_tree(worker.proc.pid), time.monotonic()
        time.sleep(a.idle_seconds)
        after, elapsed = sample_tree(worker.proc.pid), time.monotonic() - started
        summary["idle"] = {"wall_seconds": elapsed, "cpu_seconds": after["cpu_seconds"] - before["cpu_seconds"],
                           "one_core_cpu_percent": 100 * (after["cpu_seconds"] - before["cpu_seconds"]) / elapsed,
                           "worker_alive": worker.proc.poll() is None, "tree_after": after}
        summary["status"] = "measurement_complete" if not summary["errors"] and not failures else "measurement_has_failures"
    except (Exception, KeyboardInterrupt) as exc:
        summary["status"] = "interrupted_or_failed"
        summary["errors"].append({"type": type(exc).__name__})
    finally:
        if monitor:
            summary["last_peak"] = monitor.finish()
        if worker:
            worker.close()
        fields = ["id", "variant", "source_chars", "elapsed_ms", "status"]
        with (a.output_dir / "metrics.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(metrics)
        checkpoint()
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["status"] == "measurement_complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
