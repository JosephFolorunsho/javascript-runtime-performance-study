from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import random
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


URL = "http://127.0.0.1:3000/json"
HOST = "127.0.0.1"
PORT = 3000

EXPECTED_BODY = (
    '{"message":"JavaScript runtime benchmark","status":"success"}'
)

RUNTIMES = ("node", "bun", "deno")
CONNECTION_LEVELS = (10, 50, 100)

# The ten repetitions are divided into two balanced sessions.
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
    "connections",
    "pipelining",
    "warmup_seconds",
    "measurement_seconds",
    "timeout_seconds",
    "shuffle_seed",
    "started_at",
    "finished_at",
    "status",
    "requests_per_second",
    "mean_latency_ms",
    "median_latency_ms",
    "p99_latency_ms",
    "total_requests",
    "errors",
    "timeouts",
    "mismatches",
    "non_2xx",
    "result_file",
    "server_output_log",
    "server_error_log",
    "autocannon_error_log",
    "error_message",
]


class BenchmarkError(RuntimeError):
    """Raised when a benchmark observation cannot be completed."""


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one controlled session of the final HTTP benchmark."
        )
    )

    parser.add_argument(
        "--session",
        required=True,
        type=int,
        choices=(1, 2),
        help=(
            "Collection session: 1 runs repetitions 1-5; "
            "2 runs repetitions 6-10."
        ),
    )

    parser.add_argument(
        "--warmup",
        type=int,
        default=10,
        help="Warm-up duration in seconds. Default: 10.",
    )

    parser.add_argument(
        "--duration",
        type=int,
        default=30,
        help="Measured duration in seconds. Default: 30.",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Autocannon timeout in seconds. Default: 10.",
    )

    parser.add_argument(
        "--pipelining",
        type=int,
        default=1,
        help="Requests pipelined per connection. Default: 1.",
    )

    parser.add_argument(
        "--cooldown",
        type=int,
        default=10,
        help="Cool-down between observations. Default: 10.",
    )

    parser.add_argument(
        "--seed",
        type=int,
        default=20260803,
        help="Base seed for reproducible randomisation.",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Explicitly overwrite existing valid observations. "
            "Do not use this during normal final collection."
        ),
    )

    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="Stop the session immediately after a failed observation.",
    )

    args = parser.parse_args()

    for name in (
        "warmup",
        "duration",
        "timeout",
        "pipelining",
        "cooldown",
    ):
        if getattr(args, name) <= 0:
            parser.error(f"--{name} must be greater than zero.")

    return args


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_executable(name: str) -> str:
    executable = shutil.which(name)

    if executable is None:
        raise BenchmarkError(
            f"'{name}' could not be found on PATH."
        )

    return executable


def get_runtime_command(
    runtime: str,
    project_root: Path,
) -> list[str]:
    server_path = (
        project_root
        / "benchmarks"
        / "http"
        / runtime
        / "server.js"
    )

    if not server_path.exists():
        raise BenchmarkError(
            f"Server file was not found: {server_path}"
        )

    if runtime == "node":
        return [
            find_executable("node"),
            str(server_path),
        ]

    if runtime == "bun":
        return [
            find_executable("bun"),
            str(server_path),
        ]

    if runtime == "deno":
        return [
            find_executable("deno"),
            "run",
            "--allow-net=127.0.0.1:3000",
            str(server_path),
        ]

    raise BenchmarkError(f"Unsupported runtime: {runtime}")


def get_autocannon_path(project_root: Path) -> Path:
    filename = (
        "autocannon.cmd"
        if os.name == "nt"
        else "autocannon"
    )

    path = (
        project_root
        / "node_modules"
        / ".bin"
        / filename
    )

    if not path.exists():
        raise BenchmarkError(
            "The local Autocannon executable was not found:\n"
            f"{path}\n\n"
            "Run 'npm install' from the project root."
        )

    return path


def get_autocannon_command(
    autocannon_path: Path,
    arguments: list[str],
) -> list[str]:
    if os.name == "nt":
        command_line = subprocess.list2cmdline(
            [str(autocannon_path), *arguments]
        )

        return [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/s",
            "/c",
            command_line,
        ]

    return [str(autocannon_path), *arguments]


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


def collect_environment_metadata(
    project_root: Path,
    autocannon_path: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    autocannon_version_command = get_autocannon_command(
        autocannon_path,
        ["--version"],
    )

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

    return {
        "protocol": {
            "url": URL,
            "endpoint": "GET /json",
            "connections": list(CONNECTION_LEVELS),
            "pipelining": args.pipelining,
            "warmup_seconds": args.warmup,
            "measurement_seconds": args.duration,
            "timeout_seconds": args.timeout,
            "cooldown_seconds": args.cooldown,
            "repetitions_per_configuration": 10,
            "total_observations": 90,
            "base_random_seed": args.seed,
        },
        "tools": {
            "python": {
                "version": sys.version,
                "executable": sys.executable,
            },
            "platform": {
                "description": platform.platform(),
                "machine": platform.machine(),
                "processor": platform.processor(),
            },
            "runtimes": runtime_information,
            "autocannon": {
                "executable": str(autocannon_path),
                "version": capture_command_output(
                    autocannon_version_command,
                    project_root,
                ),
            },
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

        print(f"Created frozen metadata:\n{metadata_path}")
        return

    existing = json.loads(
        metadata_path.read_text(encoding="utf-8")
    )

    for section in ("protocol", "tools"):
        if existing.get(section) != current_metadata.get(section):
            raise BenchmarkError(
                "The current environment or protocol differs from "
                "the frozen final-test metadata.\n\n"
                f"Inspect: {metadata_path}\n\n"
                "Do not continue until the difference is explained."
            )

    print("Frozen environment and protocol verified.")


def ensure_port_is_free() -> None:
    with socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM,
    ) as test_socket:
        try:
            test_socket.bind((HOST, PORT))
        except OSError as error:
            raise BenchmarkError(
                f"Port {PORT} is already in use. "
                "Stop any manually running benchmark server."
            ) from error


def validate_endpoint() -> None:
    request = urllib.request.Request(
        URL,
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=2,
        ) as response:
            body = response.read().decode("utf-8")
            content_type = response.headers.get(
                "Content-Type",
                "",
            )

            if response.status != 200:
                raise BenchmarkError(
                    f"Endpoint returned HTTP {response.status}."
                )

            if body != EXPECTED_BODY:
                raise BenchmarkError(
                    "Endpoint returned an unexpected response body."
                )

            if not content_type.startswith("application/json"):
                raise BenchmarkError(
                    "Endpoint returned an unexpected content type."
                )

    except urllib.error.URLError as error:
        raise BenchmarkError(
            f"Endpoint validation failed: {error}"
        ) from error


def wait_for_server(
    process: subprocess.Popen[str],
    maximum_wait_seconds: int = 15,
) -> None:
    deadline = time.monotonic() + maximum_wait_seconds

    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BenchmarkError(
                "Server process exited before becoming ready."
            )

        try:
            validate_endpoint()
            return
        except BenchmarkError:
            time.sleep(0.25)

    raise BenchmarkError(
        "Server did not become ready within "
        f"{maximum_wait_seconds} seconds."
    )


def stop_process(
    process: subprocess.Popen[str] | None,
) -> None:
    if process is None or process.poll() is not None:
        return

    process.terminate()

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_autocannon(
    project_root: Path,
    autocannon_path: Path,
    *,
    connections: int,
    duration: int,
    timeout: int,
    pipelining: int,
    json_output: bool,
    stderr_path: Path,
) -> str:
    arguments = [
        "--connections",
        str(connections),
        "--duration",
        str(duration),
        "--pipelining",
        str(pipelining),
        "--timeout",
        str(timeout),
        "--no-progress",
    ]

    if json_output:
        arguments.append("--json")

    arguments.append(URL)

    command = get_autocannon_command(
        autocannon_path,
        arguments,
    )

    with stderr_path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as error_file:
        completed = subprocess.run(
            command,
            cwd=project_root,
            stdout=(
                subprocess.PIPE
                if json_output
                else subprocess.DEVNULL
            ),
            stderr=error_file,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=duration + timeout + 30,
            check=False,
        )

    if completed.returncode != 0:
        error_text = stderr_path.read_text(
            encoding="utf-8",
            errors="replace",
        )

        raise BenchmarkError(
            f"Autocannon exited with code "
            f"{completed.returncode}.\n\n"
            f"{error_text}"
        )

    return completed.stdout or ""


def validate_result(
    result: dict[str, Any],
    *,
    connections: int,
    pipelining: int,
) -> None:
    for section in ("latency", "requests", "throughput"):
        if section not in result:
            raise BenchmarkError(
                f"Result is missing the '{section}' section."
            )

    if result.get("url") != URL:
        raise BenchmarkError(
            "Result contains an unexpected URL."
        )

    if result.get("connections") != connections:
        raise BenchmarkError(
            "Result contains an unexpected connection count."
        )

    if result.get("pipelining") != pipelining:
        raise BenchmarkError(
            "Result contains an unexpected pipelining value."
        )

    if result["requests"].get("total", 0) <= 0:
        raise BenchmarkError(
            "Result contains no completed requests."
        )


def result_status(result: dict[str, Any]) -> str:
    diagnostic_total = sum(
        int(result.get(field, 0))
        for field in (
            "errors",
            "timeouts",
            "mismatches",
            "non2xx",
        )
    )

    return (
        "success"
        if diagnostic_total == 0
        else "quality_warning"
    )


def build_manifest_record(
    *,
    run_id: str,
    session_id: int,
    sequence: int,
    repetition: int,
    runtime: str,
    connections: int,
    args: argparse.Namespace,
    shuffle_seed: int,
    started_at: str,
    finished_at: str,
    status: str,
    result: dict[str, Any] | None,
    result_path: Path,
    server_output_path: Path,
    server_error_path: Path,
    autocannon_error_path: Path,
    project_root: Path,
    error_message: str = "",
) -> dict[str, Any]:
    requests = result.get("requests", {}) if result else {}
    latency = result.get("latency", {}) if result else {}

    return {
        "run_id": run_id,
        "session_id": session_id,
        "sequence": sequence,
        "repetition": repetition,
        "runtime": runtime,
        "connections": connections,
        "pipelining": args.pipelining,
        "warmup_seconds": args.warmup,
        "measurement_seconds": args.duration,
        "timeout_seconds": args.timeout,
        "shuffle_seed": shuffle_seed,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "requests_per_second": requests.get("average", ""),
        "mean_latency_ms": latency.get("mean", ""),
        "median_latency_ms": latency.get("p50", ""),
        "p99_latency_ms": latency.get("p99", ""),
        "total_requests": requests.get("total", ""),
        "errors": result.get("errors", "") if result else "",
        "timeouts": result.get("timeouts", "") if result else "",
        "mismatches": result.get("mismatches", "") if result else "",
        "non_2xx": result.get("non2xx", "") if result else "",
        "result_file": result_path.relative_to(
            project_root
        ).as_posix(),
        "server_output_log": server_output_path.relative_to(
            project_root
        ).as_posix(),
        "server_error_log": server_error_path.relative_to(
            project_root
        ).as_posix(),
        "autocannon_error_log": autocannon_error_path.relative_to(
            project_root
        ).as_posix(),
        "error_message": error_message,
    }


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


def create_execution_plan(
    *,
    session_id: int,
    base_seed: int,
) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    sequence = 0

    combinations = [
        (runtime, connections)
        for runtime in RUNTIMES
        for connections in CONNECTION_LEVELS
    ]

    for repetition in SESSION_REPETITIONS[session_id]:
        shuffle_seed = base_seed + repetition
        rng = random.Random(shuffle_seed)

        repetition_combinations = combinations.copy()
        rng.shuffle(repetition_combinations)

        for runtime, connections in repetition_combinations:
            sequence += 1

            plan.append(
                {
                    "session_id": session_id,
                    "sequence": sequence,
                    "repetition": repetition,
                    "runtime": runtime,
                    "connections": connections,
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
                "connections",
                "shuffle_seed",
            ],
        )

        writer.writeheader()
        writer.writerows(plan)


def load_existing_result(
    result_path: Path,
    *,
    connections: int,
    pipelining: int,
) -> dict[str, Any]:
    try:
        result = json.loads(
            result_path.read_text(encoding="utf-8")
        )

        validate_result(
            result,
            connections=connections,
            pipelining=pipelining,
        )

        return result

    except (
        OSError,
        json.JSONDecodeError,
        BenchmarkError,
    ) as error:
        raise BenchmarkError(
            f"An existing result file is invalid:\n"
            f"{result_path}\n\n"
            "It will not be overwritten automatically."
        ) from error


def run_observation(
    *,
    project_root: Path,
    autocannon_path: Path,
    output_directory: Path,
    log_directory: Path,
    manifest_path: Path,
    plan_item: dict[str, Any],
    args: argparse.Namespace,
) -> str:
    session_id = plan_item["session_id"]
    sequence = plan_item["sequence"]
    repetition = plan_item["repetition"]
    runtime = plan_item["runtime"]
    connections = plan_item["connections"]
    shuffle_seed = plan_item["shuffle_seed"]

    run_id = (
        f"http_s{session_id:02d}"
        f"_r{repetition:02d}"
        f"_{runtime}"
        f"_c{connections:03d}"
    )

    result_path = output_directory / f"{run_id}.json"

    server_output_path = (
        log_directory / f"{run_id}-server-output.log"
    )

    server_error_path = (
        log_directory / f"{run_id}-server-error.log"
    )

    warmup_error_path = (
        log_directory / f"{run_id}-warmup-error.log"
    )

    autocannon_error_path = (
        log_directory / f"{run_id}-autocannon-error.log"
    )

    if result_path.exists() and not args.force:
        existing_result = load_existing_result(
            result_path,
            connections=connections,
            pipelining=args.pipelining,
        )

        status = result_status(existing_result)

        record = build_manifest_record(
            run_id=run_id,
            session_id=session_id,
            sequence=sequence,
            repetition=repetition,
            runtime=runtime,
            connections=connections,
            args=args,
            shuffle_seed=shuffle_seed,
            started_at=existing_result.get("start", ""),
            finished_at=existing_result.get("finish", ""),
            status=status,
            result=existing_result,
            result_path=result_path,
            server_output_path=server_output_path,
            server_error_path=server_error_path,
            autocannon_error_path=autocannon_error_path,
            project_root=project_root,
        )

        upsert_manifest(manifest_path, record)

        print(f"Skipping existing valid result: {run_id}")
        return "skipped"

    ensure_port_is_free()

    runtime_command = get_runtime_command(
        runtime,
        project_root,
    )

    process: subprocess.Popen[str] | None = None
    result: dict[str, Any] | None = None
    started_at = utc_now()
    finished_at = ""
    status = "failed"
    error_message = ""

    creation_flags = (
        subprocess.CREATE_NO_WINDOW
        if os.name == "nt"
        else 0
    )

    try:
        print(
            f"\n[{sequence}/45] {run_id}\n"
            f"Starting {runtime} at {connections} connections..."
        )

        with (
            server_output_path.open(
                "w",
                encoding="utf-8",
                newline="",
            ) as server_output_file,
            server_error_path.open(
                "w",
                encoding="utf-8",
                newline="",
            ) as server_error_file,
        ):
            process = subprocess.Popen(
                runtime_command,
                cwd=project_root,
                stdout=server_output_file,
                stderr=server_error_file,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creation_flags,
            )

            wait_for_server(process)
            print("Server ready.")

            print(
                f"Warm-up: {args.warmup} seconds..."
            )

            run_autocannon(
                project_root,
                autocannon_path,
                connections=connections,
                duration=args.warmup,
                timeout=args.timeout,
                pipelining=args.pipelining,
                json_output=False,
                stderr_path=warmup_error_path,
            )

            if process.poll() is not None:
                raise BenchmarkError(
                    "Server exited during warm-up."
                )

            # Confirm that the endpoint still behaves correctly.
            validate_endpoint()

            print(
                f"Measured run: {args.duration} seconds..."
            )

            json_text = run_autocannon(
                project_root,
                autocannon_path,
                connections=connections,
                duration=args.duration,
                timeout=args.timeout,
                pipelining=args.pipelining,
                json_output=True,
                stderr_path=autocannon_error_path,
            ).strip()

            if not json_text:
                raise BenchmarkError(
                    "Autocannon returned an empty result."
                )

            try:
                result = json.loads(json_text)
            except json.JSONDecodeError as error:
                raise BenchmarkError(
                    "Autocannon returned invalid JSON."
                ) from error

            validate_result(
                result,
                connections=connections,
                pipelining=args.pipelining,
            )

            # Confirm that the endpoint remains valid after load.
            validate_endpoint()

            status = result_status(result)

            result_path.write_text(
                json.dumps(result, indent=2) + "\n",
                encoding="utf-8",
            )

            print(
                f"Completed: {run_id}\n"
                f"Requests/sec: "
                f"{result['requests']['average']}\n"
                f"Mean latency: "
                f"{result['latency']['mean']} ms\n"
                f"P99 latency: "
                f"{result['latency']['p99']} ms\n"
                f"Status: {status}"
            )

    except (
        BenchmarkError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        error_message = str(error)
        status = "failed"

        print(
            f"\nFailed: {run_id}\n{error_message}",
            file=sys.stderr,
        )

    finally:
        stop_process(process)
        finished_at = utc_now()

        record = build_manifest_record(
            run_id=run_id,
            session_id=session_id,
            sequence=sequence,
            repetition=repetition,
            runtime=runtime,
            connections=connections,
            args=args,
            shuffle_seed=shuffle_seed,
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            result=result,
            result_path=result_path,
            server_output_path=server_output_path,
            server_error_path=server_error_path,
            autocannon_error_path=autocannon_error_path,
            project_root=project_root,
            error_message=error_message,
        )

        upsert_manifest(manifest_path, record)

    return status


def main() -> int:
    args = parse_arguments()

    project_root = Path(__file__).resolve().parents[1]
    autocannon_path = get_autocannon_path(project_root)

    raw_directory = (
        project_root
        / "data"
        / "raw"
        / "http"
    )

    session_directory = (
        raw_directory
        / f"session_{args.session:02d}"
    )

    log_directory = (
        project_root
        / "results"
        / "logs"
        / "http_final"
        / f"session_{args.session:02d}"
    )

    raw_directory.mkdir(parents=True, exist_ok=True)
    session_directory.mkdir(parents=True, exist_ok=True)
    log_directory.mkdir(parents=True, exist_ok=True)

    manifest_path = (
        raw_directory
        / "http_final_manifest.csv"
    )

    metadata_path = (
        raw_directory
        / "http_final_metadata.json"
    )

    plan_path = (
        session_directory
        / "execution_order.csv"
    )

    current_metadata = collect_environment_metadata(
        project_root,
        autocannon_path,
        args,
    )

    ensure_frozen_metadata(
        metadata_path,
        current_metadata,
    )

    plan = create_execution_plan(
        session_id=args.session,
        base_seed=args.seed,
    )

    save_execution_plan(
        plan_path,
        plan,
    )

    print("\nFinal HTTP benchmark")
    print("--------------------")
    print(f"Session:              {args.session}")
    print(
        "Repetitions:          "
        f"{list(SESSION_REPETITIONS[args.session])}"
    )
    print(f"Observations:          {len(plan)}")
    print(f"Warm-up:              {args.warmup} seconds")
    print(f"Measured duration:    {args.duration} seconds")
    print(f"Cool-down:            {args.cooldown} seconds")
    print(f"Execution plan:       {plan_path}")
    print(f"Manifest:             {manifest_path}")

    outcomes: list[str] = []

    for index, plan_item in enumerate(plan, start=1):
        outcome = run_observation(
            project_root=project_root,
            autocannon_path=autocannon_path,
            output_directory=session_directory,
            log_directory=log_directory,
            manifest_path=manifest_path,
            plan_item=plan_item,
            args=args,
        )

        outcomes.append(outcome)

        if outcome == "failed" and args.stop_on_failure:
            print(
                "\nSession stopped because "
                "--stop-on-failure was supplied.",
                file=sys.stderr,
            )
            break

        if index < len(plan) and outcome != "skipped":
            print(
                f"Cool-down: {args.cooldown} seconds..."
            )
            time.sleep(args.cooldown)

    counts = Counter(outcomes)

    print("\nSession summary")
    print("---------------")
    print(f"Successful:       {counts['success']}")
    print(f"Quality warnings: {counts['quality_warning']}")
    print(f"Skipped:          {counts['skipped']}")
    print(f"Failed:           {counts['failed']}")

    if counts["failed"] > 0:
        print(
            "\nRe-run the same session command after "
            "investigating failures. Existing valid results "
            "will be skipped automatically.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# py -3.13 runner\run_http_final.py --session 1
# py -3.13 runner\run_http_final.py --session 2