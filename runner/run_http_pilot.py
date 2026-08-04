from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


URL = "http://127.0.0.1:3000/json"
EXPECTED_BODY = (
    '{"message":"JavaScript runtime benchmark","status":"success"}'
)


class BenchmarkError(RuntimeError):
    """Raised when a benchmark stage cannot be completed."""


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one HTTP pilot observation against Node.js, Bun, or Deno."
        )
    )

    parser.add_argument(
        "--runtime",
        required=True,
        choices=("node", "bun", "deno"),
        help="Runtime hosting the HTTP server.",
    )

    parser.add_argument(
        "--connections",
        type=int,
        default=10,
        help="Number of concurrent connections. Default: 10.",
    )

    parser.add_argument(
        "--warmup",
        type=int,
        default=5,
        help="Warm-up duration in seconds. Default: 5.",
    )

    parser.add_argument(
        "--duration",
        type=int,
        default=20,
        help="Measured duration in seconds. Default: 20.",
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
        "--repetition",
        type=int,
        default=1,
        help="Repetition number used in the result filename. Default: 1.",
    )

    args = parser.parse_args()

    positive_fields = (
        "connections",
        "warmup",
        "duration",
        "timeout",
        "pipelining",
        "repetition",
    )

    for field in positive_fields:
        if getattr(args, field) <= 0:
            parser.error(f"--{field} must be greater than zero.")

    return args


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_executable(name: str) -> str:
    executable = shutil.which(name)

    if executable is None:
        raise BenchmarkError(
            f"Could not find '{name}' on PATH. "
            f"Confirm that {name} is installed and restart the terminal."
        )

    return executable


def get_runtime_command(
    runtime: str,
    project_root: Path,
) -> list[str]:
    if runtime == "node":
        return [
            find_executable("node"),
            str(project_root / "benchmarks" / "http" / "node" / "server.js"),
        ]

    if runtime == "bun":
        return [
            find_executable("bun"),
            str(project_root / "benchmarks" / "http" / "bun" / "server.js"),
        ]

    if runtime == "deno":
        return [
            find_executable("deno"),
            "run",
            "--allow-net=127.0.0.1:3000",
            str(project_root / "benchmarks" / "http" / "deno" / "server.js"),
        ]

    raise BenchmarkError(f"Unsupported runtime: {runtime}")


def get_autocannon_command(
    project_root: Path,
    arguments: list[str],
) -> list[str]:
    if os.name == "nt":
        autocannon_path = (
            project_root
            / "node_modules"
            / ".bin"
            / "autocannon.cmd"
        )

        if not autocannon_path.exists():
            raise BenchmarkError(
                "The local Autocannon executable was not found at:\n"
                f"{autocannon_path}\n\n"
                "Run 'npm install' from the project root."
            )

        # Windows .cmd files must be invoked through cmd.exe.
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

    autocannon_path = (
        project_root
        / "node_modules"
        / ".bin"
        / "autocannon"
    )

    if not autocannon_path.exists():
        raise BenchmarkError(
            f"Autocannon was not found at {autocannon_path}."
        )

    return [str(autocannon_path), *arguments]


def wait_for_server(
    process: subprocess.Popen[str],
    maximum_wait_seconds: int = 15,
) -> None:
    deadline = time.monotonic() + maximum_wait_seconds

    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BenchmarkError(
                "The server process exited before becoming ready."
            )

        try:
            request = urllib.request.Request(
                URL,
                method="GET",
            )

            with urllib.request.urlopen(
                request,
                timeout=1,
            ) as response:
                body = response.read().decode("utf-8")
                content_type = response.headers.get(
                    "Content-Type",
                    "",
                )

                if (
                    response.status == 200
                    and body == EXPECTED_BODY
                    and content_type.startswith("application/json")
                ):
                    return

        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
        ):
            pass

        time.sleep(0.25)

    raise BenchmarkError(
        "The server did not become ready within "
        f"{maximum_wait_seconds} seconds."
    )


def stop_process(process: subprocess.Popen[str] | None) -> None:
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
        project_root,
        arguments,
    )

    with stderr_path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as error_file:
        try:
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
        except subprocess.TimeoutExpired as error:
            raise BenchmarkError(
                "Autocannon exceeded the allowed execution time."
            ) from error

    if completed.returncode != 0:
        error_text = stderr_path.read_text(
            encoding="utf-8",
            errors="replace",
        )

        raise BenchmarkError(
            f"Autocannon exited with code {completed.returncode}.\n"
            f"See: {stderr_path}\n\n"
            f"{error_text}"
        )

    return completed.stdout or ""


def validate_result(
    result: dict[str, Any],
    expected_connections: int,
) -> None:
    required_sections = (
        "latency",
        "requests",
        "throughput",
    )

    for section in required_sections:
        if section not in result:
            raise BenchmarkError(
                f"Autocannon JSON is missing the '{section}' section."
            )

    if result.get("connections") != expected_connections:
        raise BenchmarkError(
            "The result contains an unexpected connection count."
        )

    if result.get("url") != URL:
        raise BenchmarkError(
            "The result contains an unexpected benchmark URL."
        )


def update_manifest(
    manifest_path: Path,
    record: dict[str, Any],
) -> None:
    fieldnames = [
        "run_id",
        "runtime",
        "connections",
        "pipelining",
        "warmup_seconds",
        "measurement_seconds",
        "timeout_seconds",
        "repetition",
        "started_at",
        "finished_at",
        "status",
        "result_file",
        "error_message",
    ]

    existing_records: list[dict[str, str]] = []

    if manifest_path.exists():
        with manifest_path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as manifest_file:
            reader = csv.DictReader(manifest_file)
            existing_records = list(reader)

    # Replace an older entry for the same run rather than duplicating it.
    existing_records = [
        row
        for row in existing_records
        if row.get("run_id") != str(record["run_id"])
    ]

    existing_records.append(
        {
            field: str(record.get(field, ""))
            for field in fieldnames
        }
    )

    with manifest_path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as manifest_file:
        writer = csv.DictWriter(
            manifest_file,
            fieldnames=fieldnames,
        )

        writer.writeheader()
        writer.writerows(existing_records)


def print_summary(
    runtime: str,
    result: dict[str, Any],
) -> None:
    print("\nResult summary:")
    print(f"Runtime:             {runtime}")
    print(f"Connections:         {result['connections']}")
    print(f"Duration:            {result['duration']} seconds")
    print(
        "Requests/second:     "
        f"{result['requests']['average']}"
    )
    print(
        "Mean latency:        "
        f"{result['latency']['mean']} ms"
    )
    print(
        "Median latency:      "
        f"{result['latency']['p50']} ms"
    )
    print(
        "P99 latency:         "
        f"{result['latency']['p99']} ms"
    )
    print(
        "Total requests:      "
        f"{result['requests']['total']}"
    )
    print(f"Errors:              {result['errors']}")
    print(f"Timeouts:            {result['timeouts']}")
    print(f"Non-2xx responses:   {result['non2xx']}")


def main() -> int:
    args = parse_arguments()

    project_root = Path(__file__).resolve().parents[1]

    output_directory = (
        project_root
        / "data"
        / "pilot"
        / "http"
        / "repeated"
    )

    log_directory = (
        project_root
        / "results"
        / "logs"
        / "http_pilot"
    )

    output_directory.mkdir(parents=True, exist_ok=True)
    log_directory.mkdir(parents=True, exist_ok=True)

    run_id = (
        f"{args.runtime}"
        f"_c{args.connections:03d}"
        f"_r{args.repetition:02d}"
    )

    result_path = output_directory / f"{run_id}.json"
    manifest_path = output_directory / "pilot_manifest.csv"

    server_output_path = (
        log_directory
        / f"{run_id}-server-output.log"
    )

    server_error_path = (
        log_directory
        / f"{run_id}-server-error.log"
    )

    warmup_error_path = (
        log_directory
        / f"{run_id}-warmup-error.log"
    )

    measurement_error_path = (
        log_directory
        / f"{run_id}-autocannon-error.log"
    )

    runtime_command = get_runtime_command(
        args.runtime,
        project_root,
    )

    started_at = utc_now()
    finished_at = ""
    status = "failed"
    error_message = ""
    process: subprocess.Popen[str] | None = None

    try:
        print(f"\nHTTP pilot observation: {run_id}")
        print(f"Starting the {args.runtime} HTTP server...")

        creation_flags = 0

        if os.name == "nt":
            creation_flags = subprocess.CREATE_NO_WINDOW

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
                f"Running {args.warmup}-second warm-up..."
            )

            run_autocannon(
                project_root,
                connections=args.connections,
                duration=args.warmup,
                timeout=args.timeout,
                pipelining=args.pipelining,
                json_output=False,
                stderr_path=warmup_error_path,
            )

            if process.poll() is not None:
                raise BenchmarkError(
                    "The server exited during warm-up."
                )

            print("Warm-up completed.")
            print(
                f"Running {args.duration}-second measured test..."
            )

            json_text = run_autocannon(
                project_root,
                connections=args.connections,
                duration=args.duration,
                timeout=args.timeout,
                pipelining=args.pipelining,
                json_output=True,
                stderr_path=measurement_error_path,
            ).strip()

            if not json_text:
                raise BenchmarkError(
                    "Autocannon returned an empty JSON result."
                )

            try:
                result = json.loads(json_text)
            except json.JSONDecodeError as error:
                raise BenchmarkError(
                    "Autocannon returned invalid JSON."
                ) from error

            validate_result(
                result,
                expected_connections=args.connections,
            )

            result_path.write_text(
                json_text + "\n",
                encoding="utf-8",
            )

            status = "success"

        print("\nPilot observation completed successfully.")
        print(f"Result saved to:\n{result_path}")

        print_summary(
            args.runtime,
            result,
        )

        if result.get("errors", 0) > 0:
            print(
                "\nWarning: the run recorded connection errors.",
                file=sys.stderr,
            )

        if result.get("timeouts", 0) > 0:
            print(
                "\nWarning: the run recorded timeouts.",
                file=sys.stderr,
            )

        if result.get("non2xx", 0) > 0:
            print(
                "\nWarning: the run recorded non-2xx responses.",
                file=sys.stderr,
            )

        return 0

    except (
        BenchmarkError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        error_message = str(error)
        print(
            f"\nHTTP pilot observation failed:\n{error_message}",
            file=sys.stderr,
        )
        return 1

    finally:
        stop_process(process)
        finished_at = utc_now()

        update_manifest(
            manifest_path,
            {
                "run_id": run_id,
                "runtime": args.runtime,
                "connections": args.connections,
                "pipelining": args.pipelining,
                "warmup_seconds": args.warmup,
                "measurement_seconds": args.duration,
                "timeout_seconds": args.timeout,
                "repetition": args.repetition,
                "started_at": started_at,
                "finished_at": finished_at,
                "status": status,
                "result_file": result_path,
                "error_message": error_message,
            },
        )

        if process is not None:
            print(f"\nStopped the {args.runtime} server.")


if __name__ == "__main__":
    raise SystemExit(main())


## RUn in PoWerShEll
# py -3.13 runner\run_http_pilot.py --runtime deno 

# py -3.13 runner\run_http_pilot.py `
#     --runtime node `
#     --repetition 2