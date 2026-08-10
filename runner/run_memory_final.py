from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import queue
import random
import shutil
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError as error:
    raise SystemExit(
        "psutil is required for the memory benchmark. Install it with:\n"
        "py -3.13 -m pip install psutil"
    ) from error


RUNTIMES = ("node", "bun", "deno")
WORKLOADS = ("idle", "allocated")
ALLOCATION_BYTES = 100 * 1024 * 1024
EXPECTED_CHECKSUM = {
    "idle": 0,
    "allocated": 2,
}

SESSION_REPETITIONS = {
    1: range(1, 11),
    2: range(11, 21),
}

DEFAULT_SAMPLE_INTERVAL_SECONDS = 0.02
DEFAULT_POST_READY_SECONDS = 2.0
DEFAULT_READY_TIMEOUT_SECONDS = 10.0
DEFAULT_COOLDOWN_SECONDS = 2.0

MANIFEST_FIELDS = [
    "run_id",
    "session_id",
    "sequence",
    "repetition",
    "runtime",
    "workload",
    "shuffle_seed",
    "started_at",
    "ready_at",
    "finished_at",
    "status",
    "allocation_bytes",
    "sample_interval_ms",
    "post_ready_window_ms",
    "pre_ready_samples",
    "post_ready_samples",
    "rss_at_ready_bytes",
    "rss_at_ready_mib",
    "median_post_ready_rss_bytes",
    "median_post_ready_rss_mib",
    "mean_post_ready_rss_bytes",
    "mean_post_ready_rss_mib",
    "peak_post_ready_rss_bytes",
    "peak_post_ready_rss_mib",
    "peak_observed_rss_bytes",
    "peak_observed_rss_mib",
    "ready_payload",
    "sample_file",
    "stdout_log",
    "stderr_log",
    "error_message",
]


class BenchmarkError(RuntimeError):
    """Raised when a memory observation cannot be completed safely."""


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one controlled session of the final JavaScript-runtime "
            "memory benchmark."
        )
    )

    parser.add_argument(
        "--session",
        required=True,
        type=int,
        choices=(1, 2),
        help=(
            "Session 1 collects repetitions 1-10; "
            "session 2 collects repetitions 11-20."
        ),
    )
    parser.add_argument(
        "--sample-interval",
        type=float,
        default=DEFAULT_SAMPLE_INTERVAL_SECONDS,
        help="Memory-sampling interval in seconds. Default: 0.02.",
    )
    parser.add_argument(
        "--post-ready-seconds",
        type=float,
        default=DEFAULT_POST_READY_SECONDS,
        help="Post-READY sampling window in seconds. Default: 2.0.",
    )
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=DEFAULT_READY_TIMEOUT_SECONDS,
        help="Maximum seconds allowed for READY. Default: 10.",
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        default=DEFAULT_COOLDOWN_SECONDS,
        help="Pause between observations in seconds. Default: 2.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260810,
        help="Base seed for reproducible execution-order randomisation.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite existing valid observations. "
            "Do not use during normal final collection."
        ),
    )
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="Stop the current session after the first failed observation.",
    )

    args = parser.parse_args()

    if args.sample_interval <= 0:
        parser.error("--sample-interval must be greater than zero.")

    if args.post_ready_seconds <= 0:
        parser.error("--post-ready-seconds must be greater than zero.")

    if args.ready_timeout <= 0:
        parser.error("--ready-timeout must be greater than zero.")

    if args.cooldown < 0:
        parser.error("--cooldown cannot be negative.")

    return args


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bytes_to_mib(value: float | int) -> float:
    return float(value) / (1024 * 1024)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def find_executable(name: str) -> str:
    executable = shutil.which(name)

    if executable is None:
        raise BenchmarkError(f"'{name}' could not be found on PATH.")

    return executable


def workload_path(workload: str, project_root: Path) -> Path:
    path = (
        project_root
        / "benchmarks"
        / "memory"
        / f"{workload}.mjs"
    )

    if not path.exists():
        raise BenchmarkError(f"Memory workload file was not found: {path}")

    return path


def shared_path(project_root: Path) -> Path:
    path = project_root / "benchmarks" / "memory" / "shared.mjs"

    if not path.exists():
        raise BenchmarkError(f"Shared memory workload file was not found: {path}")

    return path


def runtime_command(
    runtime: str,
    workload: str,
    project_root: Path,
) -> list[str]:
    program = workload_path(workload, project_root)

    if runtime == "node":
        return [find_executable("node"), str(program)]

    if runtime == "bun":
        return [find_executable("bun"), str(program)]

    if runtime == "deno":
        return [find_executable("deno"), "run", str(program)]

    raise BenchmarkError(f"Unsupported runtime: {runtime}")


def capture_command_output(
    command: list[str],
    project_root: Path,
) -> str:
    completed = subprocess.run(
        command,
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )

    if completed.returncode != 0:
        raise BenchmarkError(
            "Version command failed:\n"
            f"{' '.join(command)}\n"
            f"{completed.stdout}"
        )

    return completed.stdout.strip()


def collect_metadata(
    project_root: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    runtime_information: dict[str, dict[str, str]] = {}

    for runtime in RUNTIMES:
        executable = find_executable(runtime)
        runtime_information[runtime] = {
            "executable": executable,
            "version": capture_command_output(
                [executable, "--version"],
                project_root,
            ),
        }

    files = {
        "shared": shared_path(project_root),
        "idle": workload_path("idle", project_root),
        "allocated": workload_path("allocated", project_root),
    }

    return {
        "protocol": {
            "metric": "operating-system-observed resident memory",
            "primary_summary": "median post-readiness RSS",
            "secondary_summary": "peak post-readiness RSS",
            "workloads": list(WORKLOADS),
            "allocated_payload_bytes": ALLOCATION_BYTES,
            "sample_interval_seconds": args.sample_interval,
            "post_ready_sampling_seconds": args.post_ready_seconds,
            "ready_timeout_seconds": args.ready_timeout,
            "cooldown_seconds": args.cooldown,
            "repetitions_per_runtime_workload": 20,
            "sessions": 2,
            "total_observations": 120,
            "runtime_order": (
                "all runtime-workload combinations randomised "
                "within each repetition"
            ),
            "base_random_seed": args.seed,
            "process_tree_memory_aggregation": True,
        },
        "program_hashes": {
            label: {
                "relative_path": path.relative_to(project_root).as_posix(),
                "sha256": sha256_file(path),
            }
            for label, path in files.items()
        },
        "tools": {
            "python": {
                "version": sys.version,
                "executable": sys.executable,
            },
            "psutil": {
                "version": psutil.__version__,
            },
            "platform": {
                "description": platform.platform(),
                "machine": platform.machine(),
                "processor": platform.processor(),
            },
            "runtimes": runtime_information,
        },
    }


def ensure_frozen_metadata(
    metadata_path: Path,
    current_metadata: dict[str, Any],
) -> None:
    if not metadata_path.exists():
        document = {
            "created_at": utc_now(),
            **current_metadata,
        }

        metadata_path.write_text(
            json.dumps(document, indent=2) + "\n",
            encoding="utf-8",
        )

        print(f"Created frozen memory metadata:\n{metadata_path}")
        return

    existing = json.loads(metadata_path.read_text(encoding="utf-8"))

    for section in ("protocol", "program_hashes", "tools"):
        if existing.get(section) != current_metadata.get(section):
            raise BenchmarkError(
                "The current memory protocol, workload files, or "
                "environment differs from the frozen metadata.\n\n"
                f"Inspect: {metadata_path}\n\n"
                "Do not continue until the difference is understood."
            )

    print("Frozen memory protocol and environment verified.")


def create_execution_plan(
    *,
    session_id: int,
    base_seed: int,
) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    sequence = 0

    combinations = [
        (runtime, workload)
        for runtime in RUNTIMES
        for workload in WORKLOADS
    ]

    for repetition in SESSION_REPETITIONS[session_id]:
        shuffle_seed = base_seed + repetition
        shuffled = combinations.copy()
        random.Random(shuffle_seed).shuffle(shuffled)

        for runtime, workload in shuffled:
            sequence += 1
            plan.append(
                {
                    "session_id": session_id,
                    "sequence": sequence,
                    "repetition": repetition,
                    "runtime": runtime,
                    "workload": workload,
                    "shuffle_seed": shuffle_seed,
                }
            )

    return plan


def save_execution_plan(
    plan_path: Path,
    plan: list[dict[str, Any]],
) -> None:
    with plan_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "session_id",
                "sequence",
                "repetition",
                "runtime",
                "workload",
                "shuffle_seed",
            ],
        )
        writer.writeheader()
        writer.writerows(plan)


def total_process_tree_rss(root_process: psutil.Process) -> int:
    total = 0
    processes = [root_process]

    try:
        processes.extend(root_process.children(recursive=True))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    seen: set[int] = set()

    for process in processes:
        if process.pid in seen:
            continue

        seen.add(process.pid)

        try:
            total += process.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return total


def stdout_reader(
    stream: Any,
    line_queue: queue.Queue[str | BaseException],
) -> None:
    try:
        line = stream.readline()
        line_queue.put(line)
    except BaseException as error:
        line_queue.put(error)


def stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return

    process.terminate()

    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def sample_observation(
    *,
    command: list[str],
    project_root: Path,
    workload: str,
    sample_interval: float,
    post_ready_seconds: float,
    ready_timeout: float,
) -> tuple[dict[str, Any], list[dict[str, Any]], str, str]:
    creation_flags = (
        subprocess.CREATE_NO_WINDOW
        if os.name == "nt"
        else 0
    )

    process: subprocess.Popen[str] | None = None
    lines: queue.Queue[str | BaseException] = queue.Queue(maxsize=1)
    samples: list[dict[str, Any]] = []
    stderr_text = ""
    extra_stdout = ""

    launch_monotonic = time.perf_counter()

    try:
        process = subprocess.Popen(
            command,
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creation_flags,
        )

        if process.stdout is None or process.stderr is None:
            raise BenchmarkError("Failed to open runtime output pipes.")

        ps_process = psutil.Process(process.pid)

        reader = threading.Thread(
            target=stdout_reader,
            args=(process.stdout, lines),
            daemon=True,
        )
        reader.start()

        ready_payload: dict[str, Any] | None = None
        ready_monotonic: float | None = None
        ready_timestamp = ""
        next_sample = time.perf_counter()
        deadline = launch_monotonic + ready_timeout

        while ready_payload is None:
            if process.poll() is not None:
                stderr_text = process.stderr.read()
                raise BenchmarkError(
                    "Runtime exited before READY was received.\n"
                    f"stderr:\n{stderr_text}"
                )

            now = time.perf_counter()

            if now >= deadline:
                raise BenchmarkError(
                    f"READY was not received within {ready_timeout} seconds."
                )

            if now >= next_sample:
                rss = total_process_tree_rss(ps_process)
                samples.append(
                    {
                        "elapsed_ms": (now - launch_monotonic) * 1000,
                        "phase": "pre_ready",
                        "rss_bytes": rss,
                        "rss_mib": bytes_to_mib(rss),
                    }
                )
                next_sample = now + sample_interval

            try:
                line_or_error = lines.get_nowait()
            except queue.Empty:
                time.sleep(min(sample_interval / 4, 0.005))
                continue

            if isinstance(line_or_error, BaseException):
                raise BenchmarkError(
                    f"Unable to read runtime stdout: {line_or_error}"
                )

            line = line_or_error.strip()

            try:
                payload = json.loads(line)
            except json.JSONDecodeError as error:
                raise BenchmarkError(
                    f"READY output was not valid JSON: {line!r}"
                ) from error

            expected_allocation = (
                ALLOCATION_BYTES if workload == "allocated" else 0
            )

            if (
                payload.get("event") != "READY"
                or payload.get("mode") != workload
                or payload.get("allocation_bytes") != expected_allocation
                or payload.get("checksum") != EXPECTED_CHECKSUM[workload]
            ):
                raise BenchmarkError(
                    "Runtime returned an unexpected READY payload:\n"
                    f"{payload}"
                )

            ready_payload = payload
            ready_monotonic = time.perf_counter()
            ready_timestamp = utc_now()

        post_ready_deadline = ready_monotonic + post_ready_seconds
        next_sample = ready_monotonic

        while time.perf_counter() < post_ready_deadline:
            if process.poll() is not None:
                raise BenchmarkError(
                    "Runtime exited before the post-READY sampling window ended."
                )

            now = time.perf_counter()

            if now >= next_sample:
                rss = total_process_tree_rss(ps_process)
                samples.append(
                    {
                        "elapsed_ms": (now - launch_monotonic) * 1000,
                        "phase": "post_ready",
                        "rss_bytes": rss,
                        "rss_mib": bytes_to_mib(rss),
                    }
                )
                next_sample = now + sample_interval
            else:
                time.sleep(min(sample_interval / 4, 0.005))

        post_ready_samples = [
            sample["rss_bytes"]
            for sample in samples
            if sample["phase"] == "post_ready"
        ]

        pre_ready_samples = [
            sample["rss_bytes"]
            for sample in samples
            if sample["phase"] == "pre_ready"
        ]

        if len(post_ready_samples) < 10:
            raise BenchmarkError(
                "Too few post-READY RSS samples were collected."
            )

        rss_at_ready = post_ready_samples[0]
        all_rss = [sample["rss_bytes"] for sample in samples]

        summary = {
            "ready_at": ready_timestamp,
            "allocation_bytes": ready_payload["allocation_bytes"],
            "pre_ready_samples": len(pre_ready_samples),
            "post_ready_samples": len(post_ready_samples),
            "rss_at_ready_bytes": rss_at_ready,
            "rss_at_ready_mib": bytes_to_mib(rss_at_ready),
            "median_post_ready_rss_bytes": statistics.median(
                post_ready_samples
            ),
            "median_post_ready_rss_mib": bytes_to_mib(
                statistics.median(post_ready_samples)
            ),
            "mean_post_ready_rss_bytes": statistics.fmean(
                post_ready_samples
            ),
            "mean_post_ready_rss_mib": bytes_to_mib(
                statistics.fmean(post_ready_samples)
            ),
            "peak_post_ready_rss_bytes": max(post_ready_samples),
            "peak_post_ready_rss_mib": bytes_to_mib(
                max(post_ready_samples)
            ),
            "peak_observed_rss_bytes": max(all_rss),
            "peak_observed_rss_mib": bytes_to_mib(max(all_rss)),
            "ready_payload": ready_payload,
        }

        stop_process(process)

        if process.stdout is not None:
            extra_stdout = process.stdout.read()

        if process.stderr is not None:
            stderr_text = process.stderr.read()

        return summary, samples, extra_stdout, stderr_text

    finally:
        stop_process(process)


def write_sample_csv(
    path: Path,
    samples: list[dict[str, Any]],
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "elapsed_ms",
                "phase",
                "rss_bytes",
                "rss_mib",
            ],
        )
        writer.writeheader()
        writer.writerows(samples)


def upsert_manifest(
    manifest_path: Path,
    record: dict[str, Any],
) -> None:
    existing: list[dict[str, str]] = []

    if manifest_path.exists():
        with manifest_path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as handle:
            existing = list(csv.DictReader(handle))

    existing = [
        row
        for row in existing
        if row.get("run_id") != record["run_id"]
    ]

    existing.append(
        {
            field: str(record.get(field, ""))
            for field in MANIFEST_FIELDS
        }
    )

    existing.sort(
        key=lambda row: (
            int(row["session_id"]),
            int(row["repetition"]),
            int(row["sequence"]),
        )
    )

    temporary = manifest_path.with_suffix(".tmp")

    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=MANIFEST_FIELDS,
        )
        writer.writeheader()
        writer.writerows(existing)

    temporary.replace(manifest_path)


def existing_result_is_valid(path: Path) -> bool:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return (
            result.get("status") == "success"
            and float(result.get("median_post_ready_rss_bytes", 0)) > 0
            and int(result.get("post_ready_samples", 0)) >= 10
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False


def run_observation(
    *,
    project_root: Path,
    output_directory: Path,
    sample_directory: Path,
    log_directory: Path,
    manifest_path: Path,
    plan_item: dict[str, Any],
    args: argparse.Namespace,
) -> str:
    session_id = plan_item["session_id"]
    sequence = plan_item["sequence"]
    repetition = plan_item["repetition"]
    runtime = plan_item["runtime"]
    workload = plan_item["workload"]
    shuffle_seed = plan_item["shuffle_seed"]

    run_id = (
        f"memory_s{session_id:02d}"
        f"_r{repetition:02d}"
        f"_{runtime}"
        f"_{workload}"
    )

    result_path = output_directory / f"{run_id}.json"
    sample_path = sample_directory / f"{run_id}_samples.csv"
    stdout_log_path = log_directory / f"{run_id}-stdout.log"
    stderr_log_path = log_directory / f"{run_id}-stderr.log"

    if result_path.exists() and not args.force:
        if not existing_result_is_valid(result_path):
            raise BenchmarkError(
                "An existing memory result is invalid and will not be "
                f"overwritten automatically:\n{result_path}"
            )

        existing = json.loads(result_path.read_text(encoding="utf-8"))
        record = {
            field: existing.get(field, "")
            for field in MANIFEST_FIELDS
        }
        record.update(
            {
                "run_id": run_id,
                "session_id": session_id,
                "sequence": sequence,
                "repetition": repetition,
                "runtime": runtime,
                "workload": workload,
                "shuffle_seed": shuffle_seed,
                "sample_file": sample_path.relative_to(
                    project_root
                ).as_posix(),
                "stdout_log": stdout_log_path.relative_to(
                    project_root
                ).as_posix(),
                "stderr_log": stderr_log_path.relative_to(
                    project_root
                ).as_posix(),
            }
        )
        upsert_manifest(manifest_path, record)
        print(f"Skipping existing valid result: {run_id}")
        return "skipped"

    command = runtime_command(runtime, workload, project_root)
    started_at = utc_now()
    finished_at = ""
    status = "failed"
    error_message = ""
    summary: dict[str, Any] = {}
    samples: list[dict[str, Any]] = []
    stdout_text = ""
    stderr_text = ""

    try:
        print(
            f"\n[{sequence}/60] {run_id}\n"
            f"Runtime: {runtime} | Workload: {workload}"
        )

        summary, samples, stdout_text, stderr_text = sample_observation(
            command=command,
            project_root=project_root,
            workload=workload,
            sample_interval=args.sample_interval,
            post_ready_seconds=args.post_ready_seconds,
            ready_timeout=args.ready_timeout,
        )

        status = "success"
        finished_at = utc_now()

        write_sample_csv(sample_path, samples)
        stdout_log_path.write_text(stdout_text, encoding="utf-8")
        stderr_log_path.write_text(stderr_text, encoding="utf-8")

        result_document = {
            "run_id": run_id,
            "session_id": session_id,
            "sequence": sequence,
            "repetition": repetition,
            "runtime": runtime,
            "workload": workload,
            "shuffle_seed": shuffle_seed,
            "started_at": started_at,
            "ready_at": summary["ready_at"],
            "finished_at": finished_at,
            "status": status,
            "allocation_bytes": summary["allocation_bytes"],
            "sample_interval_ms": args.sample_interval * 1000,
            "post_ready_window_ms": args.post_ready_seconds * 1000,
            "pre_ready_samples": summary["pre_ready_samples"],
            "post_ready_samples": summary["post_ready_samples"],
            "rss_at_ready_bytes": summary["rss_at_ready_bytes"],
            "rss_at_ready_mib": summary["rss_at_ready_mib"],
            "median_post_ready_rss_bytes": summary[
                "median_post_ready_rss_bytes"
            ],
            "median_post_ready_rss_mib": summary[
                "median_post_ready_rss_mib"
            ],
            "mean_post_ready_rss_bytes": summary[
                "mean_post_ready_rss_bytes"
            ],
            "mean_post_ready_rss_mib": summary[
                "mean_post_ready_rss_mib"
            ],
            "peak_post_ready_rss_bytes": summary[
                "peak_post_ready_rss_bytes"
            ],
            "peak_post_ready_rss_mib": summary[
                "peak_post_ready_rss_mib"
            ],
            "peak_observed_rss_bytes": summary[
                "peak_observed_rss_bytes"
            ],
            "peak_observed_rss_mib": summary[
                "peak_observed_rss_mib"
            ],
            "ready_payload": json.dumps(
                summary["ready_payload"],
                separators=(",", ":"),
            ),
            "sample_file": sample_path.relative_to(
                project_root
            ).as_posix(),
            "stdout_log": stdout_log_path.relative_to(
                project_root
            ).as_posix(),
            "stderr_log": stderr_log_path.relative_to(
                project_root
            ).as_posix(),
            "error_message": "",
        }

        result_path.write_text(
            json.dumps(result_document, indent=2) + "\n",
            encoding="utf-8",
        )

        print(
            f"Completed: {run_id}\n"
            f"Median post-READY RSS: "
            f"{summary['median_post_ready_rss_mib']:.2f} MiB\n"
            f"Peak post-READY RSS: "
            f"{summary['peak_post_ready_rss_mib']:.2f} MiB"
        )

    except (
        BenchmarkError,
        OSError,
        subprocess.SubprocessError,
        psutil.Error,
    ) as error:
        error_message = str(error)
        finished_at = utc_now()
        status = "failed"

        stdout_log_path.write_text(stdout_text, encoding="utf-8")
        stderr_log_path.write_text(stderr_text, encoding="utf-8")

        print(
            f"\nFailed: {run_id}\n{error_message}",
            file=sys.stderr,
        )

    record = {
        "run_id": run_id,
        "session_id": session_id,
        "sequence": sequence,
        "repetition": repetition,
        "runtime": runtime,
        "workload": workload,
        "shuffle_seed": shuffle_seed,
        "started_at": started_at,
        "ready_at": summary.get("ready_at", ""),
        "finished_at": finished_at,
        "status": status,
        "allocation_bytes": summary.get("allocation_bytes", ""),
        "sample_interval_ms": args.sample_interval * 1000,
        "post_ready_window_ms": args.post_ready_seconds * 1000,
        "pre_ready_samples": summary.get("pre_ready_samples", ""),
        "post_ready_samples": summary.get("post_ready_samples", ""),
        "rss_at_ready_bytes": summary.get("rss_at_ready_bytes", ""),
        "rss_at_ready_mib": summary.get("rss_at_ready_mib", ""),
        "median_post_ready_rss_bytes": summary.get(
            "median_post_ready_rss_bytes", ""
        ),
        "median_post_ready_rss_mib": summary.get(
            "median_post_ready_rss_mib", ""
        ),
        "mean_post_ready_rss_bytes": summary.get(
            "mean_post_ready_rss_bytes", ""
        ),
        "mean_post_ready_rss_mib": summary.get(
            "mean_post_ready_rss_mib", ""
        ),
        "peak_post_ready_rss_bytes": summary.get(
            "peak_post_ready_rss_bytes", ""
        ),
        "peak_post_ready_rss_mib": summary.get(
            "peak_post_ready_rss_mib", ""
        ),
        "peak_observed_rss_bytes": summary.get(
            "peak_observed_rss_bytes", ""
        ),
        "peak_observed_rss_mib": summary.get(
            "peak_observed_rss_mib", ""
        ),
        "ready_payload": (
            json.dumps(summary.get("ready_payload", {}), separators=(",", ":"))
            if summary
            else ""
        ),
        "sample_file": sample_path.relative_to(
            project_root
        ).as_posix(),
        "stdout_log": stdout_log_path.relative_to(
            project_root
        ).as_posix(),
        "stderr_log": stderr_log_path.relative_to(
            project_root
        ).as_posix(),
        "error_message": error_message,
    }

    upsert_manifest(manifest_path, record)
    return status


def main() -> int:
    args = parse_arguments()
    project_root = Path(__file__).resolve().parents[1]

    raw_directory = project_root / "data" / "raw" / "memory"
    session_directory = raw_directory / f"session_{args.session:02d}"
    sample_directory = session_directory / "samples"
    log_directory = (
        project_root
        / "results"
        / "logs"
        / "memory_final"
        / f"session_{args.session:02d}"
    )

    raw_directory.mkdir(parents=True, exist_ok=True)
    session_directory.mkdir(parents=True, exist_ok=True)
    sample_directory.mkdir(parents=True, exist_ok=True)
    log_directory.mkdir(parents=True, exist_ok=True)

    manifest_path = raw_directory / "memory_final_manifest.csv"
    metadata_path = raw_directory / "memory_final_metadata.json"
    plan_path = session_directory / "execution_order.csv"

    metadata = collect_metadata(project_root, args)
    ensure_frozen_metadata(metadata_path, metadata)

    plan = create_execution_plan(
        session_id=args.session,
        base_seed=args.seed,
    )
    save_execution_plan(plan_path, plan)

    print("\nFinal memory benchmark")
    print("----------------------")
    print(f"Session:               {args.session}")
    print(
        "Repetitions:           "
        f"{list(SESSION_REPETITIONS[args.session])}"
    )
    print(f"Observations:           {len(plan)}")
    print(
        f"Sample interval:        {args.sample_interval * 1000:.0f} ms"
    )
    print(
        f"Post-READY window:      {args.post_ready_seconds:.1f} seconds"
    )
    print(f"Allocated workload:     {ALLOCATION_BYTES} bytes")
    print(f"Execution plan:         {plan_path}")
    print(f"Manifest:               {manifest_path}")

    outcomes: list[str] = []

    for index, plan_item in enumerate(plan, start=1):
        try:
            outcome = run_observation(
                project_root=project_root,
                output_directory=session_directory,
                sample_directory=sample_directory,
                log_directory=log_directory,
                manifest_path=manifest_path,
                plan_item=plan_item,
                args=args,
            )
        except BenchmarkError as error:
            print(f"\nFatal observation error:\n{error}", file=sys.stderr)
            outcome = "failed"

        outcomes.append(outcome)

        if outcome == "failed" and args.stop_on_failure:
            print(
                "\nSession stopped because --stop-on-failure was supplied.",
                file=sys.stderr,
            )
            break

        if index < len(plan) and outcome != "skipped":
            time.sleep(args.cooldown)

    counts = Counter(outcomes)

    print("\nSession summary")
    print("---------------")
    print(f"Successful: {counts['success']}")
    print(f"Skipped:    {counts['skipped']}")
    print(f"Failed:     {counts['failed']}")

    if counts["failed"] > 0:
        print(
            "\nInvestigate the failure and rerun the same session. "
            "Existing valid observations will be skipped.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# py -3.13 runner\run_memory_final.py --session 1 --stop-on-failure
# py -3.13 runner\run_memory_final.py --session 2 --stop-on-failure