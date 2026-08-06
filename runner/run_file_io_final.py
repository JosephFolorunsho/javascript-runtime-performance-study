from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import random
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUNTIMES = ("node", "bun", "deno")
OPERATIONS = ("read", "write")
EXPECTED_BYTES = 100 * 1024 * 1024
FIXTURE_RELATIVE_PATH = Path(
    "data/fixtures/file_io/read_fixture_100mib.bin"
)

SESSION_REPETITIONS = {
    1: range(1, 6),
    2: range(6, 11),
}

MANIFEST_FIELDS = [
    "run_id",
    "session_id",
    "sequence",
    "repetition",
    "runtime",
    "operation",
    "shuffle_seed",
    "started_at",
    "finished_at",
    "status",
    "bytes",
    "duration_ms",
    "mib_per_second",
    "command_wall_time_ms",
    "exit_code",
    "output_file_validated",
    "output_file_removed",
    "result_file",
    "benchmark_program",
    "temporary_output_file",
    "stdout_log",
    "stderr_log",
    "error_message",
]


class BenchmarkError(RuntimeError):
    """Raised when a file I/O observation cannot be completed."""


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one controlled session of the final JavaScript-runtime "
            "file I/O benchmark."
        )
    )

    parser.add_argument(
        "--session",
        required=True,
        type=int,
        choices=(1, 2),
        help=(
            "Session 1 collects repetitions 1-5; "
            "session 2 collects repetitions 6-10."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Maximum seconds allowed for one benchmark command.",
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        default=5.0,
        help="Pause between observations in seconds. Default: 5.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260806,
        help="Base seed for reproducible execution-order randomisation.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite existing valid result files. "
            "Do not use during normal final data collection."
        ),
    )
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="Stop the current session after the first failed observation.",
    )

    args = parser.parse_args()

    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero.")

    if args.cooldown < 0:
        parser.error("--cooldown cannot be negative.")

    return args


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def get_benchmark_path(runtime: str, project_root: Path) -> Path:
    path = (
        project_root
        / "benchmarks"
        / "file_io"
        / runtime
        / "benchmark.mjs"
    )

    if not path.exists():
        raise BenchmarkError(f"Benchmark program was not found: {path}")

    return path


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


def validate_binary_pattern(path: Path, expected_bytes: int) -> None:
    if not path.exists():
        raise BenchmarkError(f"Binary file was not found: {path}")

    actual_size = path.stat().st_size

    if actual_size != expected_bytes:
        raise BenchmarkError(
            f"Unexpected file size for {path}: "
            f"expected {expected_bytes}, got {actual_size}."
        )

    offsets = sorted(
        {
            0,
            1,
            255,
            256,
            4096,
            expected_bytes // 2,
            expected_bytes - 2,
            expected_bytes - 1,
        }
    )

    with path.open("rb") as file_handle:
        for offset in offsets:
            file_handle.seek(offset)
            observed = file_handle.read(1)
            expected = bytes((offset % 256,))

            if observed != expected:
                raise BenchmarkError(
                    "Binary-content validation failed for "
                    f"{path} at offset {offset}: "
                    f"expected {expected!r}, got {observed!r}."
                )


def collect_metadata(
    project_root: Path,
    fixture_path: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    runtime_information: dict[str, dict[str, str]] = {}
    program_hashes: dict[str, dict[str, str]] = {}

    for runtime in RUNTIMES:
        executable = find_executable(runtime)
        benchmark_path = get_benchmark_path(runtime, project_root)

        runtime_information[runtime] = {
            "executable": executable,
            "version": capture_command_output(
                [executable, "--version"],
                project_root,
            ),
        }

        program_hashes[runtime] = {
            "relative_path": benchmark_path.relative_to(
                project_root
            ).as_posix(),
            "sha256": sha256_file(benchmark_path),
        }

    return {
        "protocol": {
            "operations": list(OPERATIONS),
            "runtimes": list(RUNTIMES),
            "fixture_format": "deterministic raw binary byte pattern",
            "fixture_size_bytes": EXPECTED_BYTES,
            "fixture_size_mib": EXPECTED_BYTES / (1024 * 1024),
            "read_condition": (
                "Buffered warm-cache whole-file read. Each benchmark "
                "program performs one unrecorded read before the measured read."
            ),
            "write_condition": (
                "Buffered whole-file write. The payload is loaded before "
                "the measured interval; no explicit hardware flush is forced."
            ),
            "timing_source": "JavaScript performance.now()",
            "repetitions_per_runtime_operation": 10,
            "sessions": 2,
            "repetitions_per_session": 5,
            "observations_per_session": 30,
            "total_observations": 60,
            "timeout_seconds": args.timeout,
            "cooldown_seconds": args.cooldown,
            "base_random_seed": args.seed,
            "execution_order": (
                "All runtime-operation combinations randomised "
                "within each repetition."
            ),
        },
        "fixture": {
            "relative_path": fixture_path.relative_to(
                project_root
            ).as_posix(),
            "size_bytes": fixture_path.stat().st_size,
            "sha256": sha256_file(fixture_path),
        },
        "benchmark_program_hashes": program_hashes,
        "tools": {
            "python": {
                "version": sys.version,
                "executable": sys.executable,
                "role": (
                    "Experiment automation, validation, randomisation, "
                    "manifest generation and raw-result storage."
                ),
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

        print(f"Created frozen file I/O metadata:\n{metadata_path}")
        return

    existing = json.loads(metadata_path.read_text(encoding="utf-8"))

    for section in (
        "protocol",
        "fixture",
        "benchmark_program_hashes",
        "tools",
    ):
        if existing.get(section) != current_metadata.get(section):
            raise BenchmarkError(
                "The current file I/O protocol, fixture, benchmark "
                "implementation, or environment differs from the frozen "
                "metadata.\n\n"
                f"Inspect: {metadata_path}\n\n"
                "Do not continue until the difference is understood."
            )

    print("Frozen file I/O protocol and environment verified.")


def get_runtime_command(
    runtime: str,
    operation: str,
    project_root: Path,
    fixture_path: Path,
    output_path: Path,
) -> list[str]:
    benchmark_path = get_benchmark_path(runtime, project_root)
    common_arguments = [
        operation,
        str(fixture_path),
        str(output_path),
        str(EXPECTED_BYTES),
    ]

    if runtime == "node":
        return [
            find_executable("node"),
            str(benchmark_path),
            *common_arguments,
        ]

    if runtime == "bun":
        return [
            find_executable("bun"),
            str(benchmark_path),
            *common_arguments,
        ]

    if runtime == "deno":
        permissions = ["--allow-read"]

        if operation == "write":
            permissions.append("--allow-write")

        return [
            find_executable("deno"),
            "run",
            *permissions,
            str(benchmark_path),
            *common_arguments,
        ]

    raise BenchmarkError(f"Unsupported runtime: {runtime}")


def run_benchmark_command(
    command: list[str],
    project_root: Path,
    timeout_seconds: float,
) -> tuple[str, str, int, float]:
    creation_flags = (
        subprocess.CREATE_NO_WINDOW
        if os.name == "nt"
        else 0
    )

    start_ns = time.perf_counter_ns()

    completed = subprocess.run(
        command,
        cwd=project_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
        creationflags=creation_flags,
    )

    finish_ns = time.perf_counter_ns()
    wall_time_ms = (finish_ns - start_ns) / 1_000_000

    return (
        completed.stdout,
        completed.stderr,
        completed.returncode,
        wall_time_ms,
    )


def validate_benchmark_result(
    result: dict[str, Any],
    *,
    expected_operation: str,
) -> None:
    if result.get("status") != "success":
        raise BenchmarkError(
            f"Benchmark returned non-success status: {result.get('status')!r}"
        )

    if result.get("operation") != expected_operation:
        raise BenchmarkError(
            "Benchmark returned an unexpected operation value."
        )

    if result.get("bytes") != EXPECTED_BYTES:
        raise BenchmarkError(
            "Benchmark returned an unexpected byte count."
        )

    duration_ms = result.get("duration_ms")
    mib_per_second = result.get("mib_per_second")

    if (
        not isinstance(duration_ms, (int, float))
        or not math.isfinite(duration_ms)
        or duration_ms <= 0
    ):
        raise BenchmarkError(
            "Benchmark returned an invalid duration_ms value."
        )

    if (
        not isinstance(mib_per_second, (int, float))
        or not math.isfinite(mib_per_second)
        or mib_per_second <= 0
    ):
        raise BenchmarkError(
            "Benchmark returned an invalid mib_per_second value."
        )

    recomputed = (
        (EXPECTED_BYTES / (1024 * 1024))
        / (duration_ms / 1000)
    )

    if not math.isclose(
        mib_per_second,
        recomputed,
        rel_tol=1e-9,
        abs_tol=1e-6,
    ):
        raise BenchmarkError(
            "Benchmark throughput does not match the returned duration."
        )


def create_execution_plan(
    *,
    session_id: int,
    base_seed: int,
) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    sequence = 0

    combinations = [
        (runtime, operation)
        for runtime in RUNTIMES
        for operation in OPERATIONS
    ]

    for repetition in SESSION_REPETITIONS[session_id]:
        shuffle_seed = base_seed + repetition
        repetition_combinations = combinations.copy()
        random.Random(shuffle_seed).shuffle(repetition_combinations)

        for runtime, operation in repetition_combinations:
            sequence += 1
            plan.append(
                {
                    "session_id": session_id,
                    "sequence": sequence,
                    "repetition": repetition,
                    "runtime": runtime,
                    "operation": operation,
                    "shuffle_seed": shuffle_seed,
                }
            )

    return plan


def save_execution_plan(
    plan_path: Path,
    plan: list[dict[str, Any]],
) -> None:
    with plan_path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as plan_file:
        writer = csv.DictWriter(
            plan_file,
            fieldnames=[
                "session_id",
                "sequence",
                "repetition",
                "runtime",
                "operation",
                "shuffle_seed",
            ],
        )
        writer.writeheader()
        writer.writerows(plan)


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
        ) as manifest_file:
            existing = list(csv.DictReader(manifest_file))

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

    temporary_path = manifest_path.with_suffix(".tmp")

    with temporary_path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as manifest_file:
        writer = csv.DictWriter(
            manifest_file,
            fieldnames=MANIFEST_FIELDS,
        )
        writer.writeheader()
        writer.writerows(existing)

    temporary_path.replace(manifest_path)


def existing_result_is_valid(
    result_path: Path,
    *,
    runtime: str,
    operation: str,
) -> bool:
    try:
        document = json.loads(result_path.read_text(encoding="utf-8"))
        benchmark_result = document["benchmark_result"]

        return (
            document.get("status") == "success"
            and document.get("runtime") == runtime
            and document.get("operation") == operation
            and benchmark_result.get("status") == "success"
            and benchmark_result.get("operation") == operation
            and benchmark_result.get("bytes") == EXPECTED_BYTES
            and benchmark_result.get("duration_ms", 0) > 0
            and benchmark_result.get("mib_per_second", 0) > 0
        )
    except (
        OSError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
    ):
        return False


def build_manifest_record(
    *,
    run_id: str,
    session_id: int,
    sequence: int,
    repetition: int,
    runtime: str,
    operation: str,
    shuffle_seed: int,
    started_at: str,
    finished_at: str,
    status: str,
    benchmark_result: dict[str, Any] | None,
    command_wall_time_ms: float | str,
    exit_code: int | str,
    output_file_validated: bool,
    output_file_removed: bool,
    result_path: Path,
    benchmark_path: Path,
    temporary_output_path: Path,
    stdout_log_path: Path,
    stderr_log_path: Path,
    project_root: Path,
    error_message: str = "",
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "session_id": session_id,
        "sequence": sequence,
        "repetition": repetition,
        "runtime": runtime,
        "operation": operation,
        "shuffle_seed": shuffle_seed,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "bytes": (
            benchmark_result.get("bytes", "")
            if benchmark_result
            else ""
        ),
        "duration_ms": (
            benchmark_result.get("duration_ms", "")
            if benchmark_result
            else ""
        ),
        "mib_per_second": (
            benchmark_result.get("mib_per_second", "")
            if benchmark_result
            else ""
        ),
        "command_wall_time_ms": command_wall_time_ms,
        "exit_code": exit_code,
        "output_file_validated": output_file_validated,
        "output_file_removed": output_file_removed,
        "result_file": result_path.relative_to(
            project_root
        ).as_posix(),
        "benchmark_program": benchmark_path.relative_to(
            project_root
        ).as_posix(),
        "temporary_output_file": temporary_output_path.relative_to(
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


def run_observation(
    *,
    project_root: Path,
    fixture_path: Path,
    output_directory: Path,
    temporary_output_directory: Path,
    log_directory: Path,
    manifest_path: Path,
    plan_item: dict[str, Any],
    args: argparse.Namespace,
) -> str:
    session_id = plan_item["session_id"]
    sequence = plan_item["sequence"]
    repetition = plan_item["repetition"]
    runtime = plan_item["runtime"]
    operation = plan_item["operation"]
    shuffle_seed = plan_item["shuffle_seed"]

    run_id = (
        f"file_io_s{session_id:02d}"
        f"_r{repetition:02d}"
        f"_{runtime}"
        f"_{operation}"
    )

    result_path = output_directory / f"{run_id}.json"
    temporary_output_path = (
        temporary_output_directory / f"{run_id}.bin"
    )
    stdout_log_path = log_directory / f"{run_id}-stdout.log"
    stderr_log_path = log_directory / f"{run_id}-stderr.log"
    benchmark_path = get_benchmark_path(runtime, project_root)

    if result_path.exists() and not args.force:
        if not existing_result_is_valid(
            result_path,
            runtime=runtime,
            operation=operation,
        ):
            raise BenchmarkError(
                "An existing result is invalid and will not be "
                f"overwritten automatically:\n{result_path}"
            )

        document = json.loads(result_path.read_text(encoding="utf-8"))
        benchmark_result = document["benchmark_result"]

        record = build_manifest_record(
            run_id=run_id,
            session_id=session_id,
            sequence=sequence,
            repetition=repetition,
            runtime=runtime,
            operation=operation,
            shuffle_seed=shuffle_seed,
            started_at=document.get("started_at", ""),
            finished_at=document.get("finished_at", ""),
            status="success",
            benchmark_result=benchmark_result,
            command_wall_time_ms=document.get(
                "command_wall_time_ms",
                "",
            ),
            exit_code=document.get("exit_code", ""),
            output_file_validated=document.get(
                "output_file_validated",
                False,
            ),
            output_file_removed=document.get(
                "output_file_removed",
                False,
            ),
            result_path=result_path,
            benchmark_path=benchmark_path,
            temporary_output_path=temporary_output_path,
            stdout_log_path=stdout_log_path,
            stderr_log_path=stderr_log_path,
            project_root=project_root,
        )

        upsert_manifest(manifest_path, record)
        print(f"Skipping existing valid observation: {run_id}")
        return "skipped"

    if temporary_output_path.exists():
        temporary_output_path.unlink()

    command = get_runtime_command(
        runtime,
        operation,
        project_root,
        fixture_path,
        temporary_output_path,
    )

    started_at = utc_now()
    finished_at = ""
    status = "failed"
    benchmark_result: dict[str, Any] | None = None
    command_wall_time_ms: float | str = ""
    exit_code: int | str = ""
    output_file_validated = False
    output_file_removed = False
    error_message = ""
    stdout_text = ""
    stderr_text = ""

    try:
        print(
            f"\n[{sequence}/30] {run_id}\n"
            f"Runtime: {runtime} | Operation: {operation}"
        )

        (
            stdout_text,
            stderr_text,
            measured_exit_code,
            measured_wall_time_ms,
        ) = run_benchmark_command(
            command,
            project_root,
            args.timeout,
        )

        exit_code = measured_exit_code
        command_wall_time_ms = measured_wall_time_ms

        if measured_exit_code != 0:
            raise BenchmarkError(
                f"Benchmark process exited with code {measured_exit_code}.\n"
                f"{stderr_text}"
            )

        json_text = stdout_text.strip()

        if not json_text:
            raise BenchmarkError("Benchmark returned empty stdout.")

        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError as error:
            raise BenchmarkError(
                "Benchmark returned invalid JSON."
            ) from error

        if not isinstance(parsed, dict):
            raise BenchmarkError(
                "Benchmark output must be a JSON object."
            )

        benchmark_result = parsed
        validate_benchmark_result(
            benchmark_result,
            expected_operation=operation,
        )

        if operation == "write":
            validate_binary_pattern(
                temporary_output_path,
                EXPECTED_BYTES,
            )
            output_file_validated = True
            temporary_output_path.unlink()
            output_file_removed = True
        else:
            if temporary_output_path.exists():
                raise BenchmarkError(
                    "The read workload unexpectedly created an output file."
                )
            output_file_validated = True
            output_file_removed = True

        status = "success"
        finished_at = utc_now()

        result_document = {
            "run_id": run_id,
            "session_id": session_id,
            "sequence": sequence,
            "repetition": repetition,
            "runtime": runtime,
            "operation": operation,
            "shuffle_seed": shuffle_seed,
            "started_at": started_at,
            "finished_at": finished_at,
            "status": status,
            "benchmark_result": benchmark_result,
            "command_wall_time_ms": command_wall_time_ms,
            "exit_code": exit_code,
            "output_file_validated": output_file_validated,
            "output_file_removed": output_file_removed,
            "benchmark_program": benchmark_path.relative_to(
                project_root
            ).as_posix(),
            "fixture_file": fixture_path.relative_to(
                project_root
            ).as_posix(),
        }

        result_path.write_text(
            json.dumps(result_document, indent=2) + "\n",
            encoding="utf-8",
        )

        print(
            f"Completed: {run_id}\n"
            f"Duration: {benchmark_result['duration_ms']:.3f} ms\n"
            f"Throughput: "
            f"{benchmark_result['mib_per_second']:.3f} MiB/s"
        )

    except (
        BenchmarkError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        error_message = str(error)
        status = "failed"
        finished_at = utc_now()

        print(
            f"\nFailed: {run_id}\n{error_message}",
            file=sys.stderr,
        )

    finally:
        stdout_log_path.write_text(
            stdout_text,
            encoding="utf-8",
        )
        stderr_log_path.write_text(
            stderr_text,
            encoding="utf-8",
        )

        record = build_manifest_record(
            run_id=run_id,
            session_id=session_id,
            sequence=sequence,
            repetition=repetition,
            runtime=runtime,
            operation=operation,
            shuffle_seed=shuffle_seed,
            started_at=started_at,
            finished_at=finished_at or utc_now(),
            status=status,
            benchmark_result=benchmark_result,
            command_wall_time_ms=command_wall_time_ms,
            exit_code=exit_code,
            output_file_validated=output_file_validated,
            output_file_removed=output_file_removed,
            result_path=result_path,
            benchmark_path=benchmark_path,
            temporary_output_path=temporary_output_path,
            stdout_log_path=stdout_log_path,
            stderr_log_path=stderr_log_path,
            project_root=project_root,
            error_message=error_message,
        )

        upsert_manifest(manifest_path, record)

    return status


def main() -> int:
    args = parse_arguments()
    project_root = Path(__file__).resolve().parents[1]
    fixture_path = project_root / FIXTURE_RELATIVE_PATH

    validate_binary_pattern(fixture_path, EXPECTED_BYTES)

    raw_directory = project_root / "data" / "raw" / "file_io"
    session_directory = raw_directory / f"session_{args.session:02d}"
    temporary_output_directory = raw_directory / "temporary_outputs"
    log_directory = (
        project_root
        / "results"
        / "logs"
        / "file_io_final"
        / f"session_{args.session:02d}"
    )

    raw_directory.mkdir(parents=True, exist_ok=True)
    session_directory.mkdir(parents=True, exist_ok=True)
    temporary_output_directory.mkdir(parents=True, exist_ok=True)
    log_directory.mkdir(parents=True, exist_ok=True)

    manifest_path = raw_directory / "file_io_final_manifest.csv"
    metadata_path = raw_directory / "file_io_final_metadata.json"
    plan_path = session_directory / "execution_order.csv"

    metadata = collect_metadata(
        project_root,
        fixture_path,
        args,
    )
    ensure_frozen_metadata(metadata_path, metadata)

    plan = create_execution_plan(
        session_id=args.session,
        base_seed=args.seed,
    )
    save_execution_plan(plan_path, plan)

    print("\nFinal file I/O benchmark")
    print("------------------------")
    print(f"Session:              {args.session}")
    print(
        "Repetitions:          "
        f"{list(SESSION_REPETITIONS[args.session])}"
    )
    print(f"Observations:          {len(plan)}")
    print(f"Fixture size:          {EXPECTED_BYTES} bytes (100 MiB)")
    print(f"Operations:            {', '.join(OPERATIONS)}")
    print(f"Cool-down:             {args.cooldown} seconds")
    print(f"Execution plan:        {plan_path}")
    print(f"Manifest:              {manifest_path}")

    outcomes: list[str] = []

    for index, plan_item in enumerate(plan, start=1):
        try:
            outcome = run_observation(
                project_root=project_root,
                fixture_path=fixture_path,
                output_directory=session_directory,
                temporary_output_directory=temporary_output_directory,
                log_directory=log_directory,
                manifest_path=manifest_path,
                plan_item=plan_item,
                args=args,
            )
        except BenchmarkError as error:
            print(
                f"\nFatal observation error:\n{error}",
                file=sys.stderr,
            )
            outcome = "failed"

        outcomes.append(outcome)

        if outcome == "failed" and args.stop_on_failure:
            print(
                "\nSession stopped because --stop-on-failure was supplied.",
                file=sys.stderr,
            )
            break

        if index < len(plan) and outcome != "skipped":
            print(f"Cool-down: {args.cooldown} seconds...")
            time.sleep(args.cooldown)

    counts = Counter(outcomes)

    print("\nSession summary")
    print("---------------")
    print(f"Successful: {counts['success']}")
    print(f"Skipped:    {counts['skipped']}")
    print(f"Failed:     {counts['failed']}")

    if counts["failed"] > 0:
        print(
            "\nInvestigate the failure and rerun the same session command. "
            "Existing valid observations will be skipped.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# py -3.13 runner\run_file_io_final.py --session 1 --stop-on-failure
# py -3.13 runner\run_file_io_final.py --session 2 --stop-on-failure 