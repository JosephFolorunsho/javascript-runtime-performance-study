from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import random
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

RUNTIMES = ("node", "bun", "deno")
EXPECTED_OUTPUT = b"READY"
SESSION_REPETITIONS = {
    1: range(1, 16),
    2: range(16, 31),
}
MANIFEST_FIELDS = [
    "run_id", "session_id", "sequence", "repetition", "runtime",
    "shuffle_seed", "started_at", "finished_at", "status",
    "startup_time_ns", "startup_time_ms", "observed_output",
    "program_file", "stdout_log", "stderr_log", "error_message",
]


class BenchmarkError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one session of the final cold-start benchmark."
    )
    parser.add_argument("--session", required=True, type=int, choices=(1, 2))
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--cooldown", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=20260804)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--stop-on-failure", action="store_true")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.cooldown < 0:
        parser.error("--cooldown cannot be negative")
    return args


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise BenchmarkError(f"'{name}' could not be found on PATH")
    return executable


def program_path(project_root: Path, runtime: str) -> Path:
    path = project_root / "benchmarks" / "cold_start" / runtime / "startup.js"
    if not path.exists():
        raise BenchmarkError(f"Startup program not found: {path}")
    return path


def runtime_command(project_root: Path, runtime: str) -> list[str]:
    script = program_path(project_root, runtime)
    if runtime == "node":
        return [find_executable("node"), str(script)]
    if runtime == "bun":
        return [find_executable("bun"), str(script)]
    if runtime == "deno":
        return [find_executable("deno"), "run", str(script)]
    raise BenchmarkError(f"Unsupported runtime: {runtime}")


def command_output(command: list[str], project_root: Path) -> str:
    result = subprocess.run(
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
    if result.returncode != 0:
        raise BenchmarkError(f"Version command failed: {' '.join(command)}")
    return result.stdout.strip()


def collect_metadata(project_root: Path, args: argparse.Namespace) -> dict:
    runtimes = {}
    programs = {}
    for runtime in RUNTIMES:
        executable = find_executable(runtime)
        path = program_path(project_root, runtime)
        runtimes[runtime] = {
            "executable": executable,
            "version": command_output([executable, "--version"], project_root),
        }
        programs[runtime] = path.relative_to(project_root).as_posix()

    return {
        "protocol": {
            "metric": "elapsed time from immediately before process creation until READY is received from stdout",
            "expected_output": "READY",
            "repetitions_per_runtime": 30,
            "sessions": 2,
            "repetitions_per_session": 15,
            "total_observations": 90,
            "timeout_seconds": args.timeout,
            "cooldown_seconds": args.cooldown,
            "base_seed": args.seed,
            "runtime_order": "randomised within each repetition",
        },
        "runtime_versions": runtimes,
        "program_files": programs,
    }


def freeze_metadata(path: Path, current: dict) -> None:
    if not path.exists():
        path.write_text(
            json.dumps({"created_at": utc_now(), **current}, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Created frozen metadata: {path}")
        return

    existing = json.loads(path.read_text(encoding="utf-8"))
    for section in ("protocol", "runtime_versions", "program_files"):
        if existing.get(section) != current.get(section):
            raise BenchmarkError(
                "The cold-start protocol or environment differs from the frozen metadata. "
                f"Inspect {path}."
            )
    print("Frozen protocol and environment verified.")


def read_exact(stream, size: int, output_queue: queue.Queue) -> None:
    try:
        data = bytearray()
        while len(data) < size:
            chunk = stream.read(size - len(data))
            if not chunk:
                break
            data.extend(chunk)
        output_queue.put(bytes(data))
    except BaseException as error:
        output_queue.put(error)


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def measure_startup(command: list[str], project_root: Path, timeout: float):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = None
    output_queue = queue.Queue(maxsize=1)
    start_ns = time.perf_counter_ns()
    try:
        process = subprocess.Popen(
            command,
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=flags,
        )
        if process.stdout is None or process.stderr is None:
            raise BenchmarkError("Unable to open process pipes")

        thread = threading.Thread(
            target=read_exact,
            args=(process.stdout, len(EXPECTED_OUTPUT), output_queue),
            daemon=True,
        )
        thread.start()

        try:
            value = output_queue.get(timeout=timeout)
        except queue.Empty as error:
            raise BenchmarkError(f"READY was not received within {timeout} seconds") from error

        end_ns = time.perf_counter_ns()
        if isinstance(value, BaseException):
            raise BenchmarkError(f"Unable to read stdout: {value}")

        stderr = process.stderr.read()
        return end_ns - start_ns, value, stderr
    finally:
        stop_process(process)


def execution_plan(session: int, seed: int) -> list[dict]:
    plan = []
    sequence = 0
    for repetition in SESSION_REPETITIONS[session]:
        shuffle_seed = seed + repetition
        order = list(RUNTIMES)
        random.Random(shuffle_seed).shuffle(order)
        for runtime in order:
            sequence += 1
            plan.append({
                "session_id": session,
                "sequence": sequence,
                "repetition": repetition,
                "runtime": runtime,
                "shuffle_seed": shuffle_seed,
            })
    return plan


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def upsert_manifest(path: Path, record: dict) -> None:
    rows = []
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    rows = [row for row in rows if row.get("run_id") != record["run_id"]]
    rows.append({field: str(record.get(field, "")) for field in MANIFEST_FIELDS})
    rows.sort(key=lambda row: (
        int(row["session_id"]), int(row["repetition"]), int(row["sequence"])
    ))
    temporary = path.with_suffix(".tmp")
    write_csv(temporary, rows, MANIFEST_FIELDS)
    temporary.replace(path)


def valid_existing_result(path: Path) -> bool:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return (
            result.get("status") == "success"
            and result.get("observed_output") == "READY"
            and float(result.get("startup_time_ms", 0)) > 0
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def run_observation(project_root: Path, session_dir: Path, log_dir: Path,
                    manifest_path: Path, item: dict, args: argparse.Namespace) -> str:
    session = item["session_id"]
    sequence = item["sequence"]
    repetition = item["repetition"]
    runtime = item["runtime"]
    shuffle_seed = item["shuffle_seed"]
    run_id = f"cold_start_s{session:02d}_r{repetition:02d}_{runtime}"

    result_path = session_dir / f"{run_id}.json"
    stdout_log = log_dir / f"{run_id}-stdout.log"
    stderr_log = log_dir / f"{run_id}-stderr.log"
    relative_program = program_path(project_root, runtime).relative_to(project_root).as_posix()

    if result_path.exists() and not args.force:
        if not valid_existing_result(result_path):
            raise BenchmarkError(f"Existing result is invalid: {result_path}")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        record = {
            **item,
            "run_id": run_id,
            "started_at": result.get("started_at", ""),
            "finished_at": result.get("finished_at", ""),
            "status": "success",
            "startup_time_ns": result["startup_time_ns"],
            "startup_time_ms": result["startup_time_ms"],
            "observed_output": "READY",
            "program_file": relative_program,
            "stdout_log": stdout_log.relative_to(project_root).as_posix(),
            "stderr_log": stderr_log.relative_to(project_root).as_posix(),
            "error_message": "",
        }
        upsert_manifest(manifest_path, record)
        print(f"Skipping existing valid result: {run_id}")
        return "skipped"

    started_at = utc_now()
    status = "failed"
    elapsed_ns = ""
    elapsed_ms = ""
    observed = ""
    error_message = ""
    stdout_data = b""
    stderr_data = b""

    try:
        print(f"\n[{sequence}/45] {run_id}")
        elapsed_ns, stdout_data, stderr_data = measure_startup(
            runtime_command(project_root, runtime), project_root, args.timeout
        )
        observed = stdout_data.decode("utf-8", errors="replace")
        if stdout_data != EXPECTED_OUTPUT:
            raise BenchmarkError(
                f"Unexpected output. Expected {EXPECTED_OUTPUT!r}, received {stdout_data!r}"
            )
        elapsed_ms = elapsed_ns / 1_000_000
        status = "success"
        finished_at = utc_now()
        result_path.write_text(json.dumps({
            "run_id": run_id,
            **item,
            "started_at": started_at,
            "finished_at": finished_at,
            "status": status,
            "startup_time_ns": elapsed_ns,
            "startup_time_ms": elapsed_ms,
            "observed_output": observed,
            "program_file": relative_program,
        }, indent=2) + "\n", encoding="utf-8")
        print(f"Startup time: {elapsed_ms:.3f} ms")
    except (BenchmarkError, OSError, subprocess.SubprocessError) as error:
        error_message = str(error)
        finished_at = utc_now()
        print(f"Failed: {run_id}\n{error_message}", file=sys.stderr)

    stdout_log.write_bytes(stdout_data)
    stderr_log.write_bytes(stderr_data)
    record = {
        **item,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "startup_time_ns": elapsed_ns,
        "startup_time_ms": elapsed_ms,
        "observed_output": observed,
        "program_file": relative_program,
        "stdout_log": stdout_log.relative_to(project_root).as_posix(),
        "stderr_log": stderr_log.relative_to(project_root).as_posix(),
        "error_message": error_message,
    }
    upsert_manifest(manifest_path, record)
    return status


def main() -> int:
    args = parse_args()
    project_root = Path(__file__).resolve().parents[1]
    raw_dir = project_root / "data" / "raw" / "cold_start"
    session_dir = raw_dir / f"session_{args.session:02d}"
    log_dir = project_root / "results" / "logs" / "cold_start_final" / f"session_{args.session:02d}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    session_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = raw_dir / "cold_start_final_manifest.csv"
    metadata_path = raw_dir / "cold_start_final_metadata.json"
    plan_path = session_dir / "execution_order.csv"

    freeze_metadata(metadata_path, collect_metadata(project_root, args))
    plan = execution_plan(args.session, args.seed)
    write_csv(plan_path, plan, [
        "session_id", "sequence", "repetition", "runtime", "shuffle_seed"
    ])

    print("\nFinal cold-start benchmark")
    print("--------------------------")
    print(f"Session:      {args.session}")
    print(f"Repetitions:  {list(SESSION_REPETITIONS[args.session])}")
    print(f"Observations: {len(plan)}")

    outcomes = []
    for index, item in enumerate(plan, start=1):
        try:
            outcome = run_observation(
                project_root, session_dir, log_dir, manifest_path, item, args
            )
        except BenchmarkError as error:
            print(f"Fatal observation error: {error}", file=sys.stderr)
            outcome = "failed"
        outcomes.append(outcome)

        if outcome == "failed" and args.stop_on_failure:
            break
        if index < len(plan) and outcome != "skipped":
            time.sleep(args.cooldown)

    counts = Counter(outcomes)
    print("\nSession summary")
    print("---------------")
    print(f"Successful: {counts['success']}")
    print(f"Skipped:    {counts['skipped']}")
    print(f"Failed:     {counts['failed']}")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

# py -3.13 runner\run_cold_start_final.py --session 1 --stop-on-failure
# py -3.13 runner\run_cold_start_final.py --session 2 --stop-on-failure