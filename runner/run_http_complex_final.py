from __future__ import annotations

import argparse
import csv
import hashlib
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

WORKLOAD = "complex"
HOST = "127.0.0.1"
PORT = 3000
PROCESS_URL = f"http://{HOST}:{PORT}/process"
HEALTH_URL = f"http://{HOST}:{PORT}/health"
EXPECTED_BODY = (
    '{"customerId":1204,"itemCount":7,"subtotal":90,'
    '"discount":9,"total":81,"status":"processed"}'
)

RUNTIMES = ("node", "bun", "deno")
CONNECTIONS = (10, 50, 100)
SESSION_REPETITIONS = {1: range(1, 6), 2: range(6, 11)}

MANIFEST_FIELDS = [
    "run_id", "workload", "session_id", "sequence", "repetition",
    "runtime", "connections", "pipelining", "warmup_seconds",
    "measurement_seconds", "timeout_seconds", "cooldown_seconds",
    "shuffle_seed", "started_at", "finished_at", "status",
    "requests_per_second", "mean_latency_ms", "median_latency_ms",
    "p90_latency_ms", "p97_5_latency_ms", "p99_latency_ms",
    "maximum_latency_ms", "total_requests", "bytes_per_second",
    "errors", "timeouts", "mismatches", "non_2xx", "result_file",
    "server_output_log", "server_error_log", "warmup_error_log",
    "autocannon_error_log", "error_message",
]


class BenchmarkError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a final complex HTTP benchmark session."
    )
    parser.add_argument("--session", required=True, type=int, choices=(1, 2))
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--pipelining", type=int, default=1)
    parser.add_argument("--cooldown", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20260803)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--stop-on-failure", action="store_true")
    args = parser.parse_args()

    for name in ("warmup", "duration", "timeout", "pipelining", "cooldown"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name} must be greater than zero.")

    return args


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise BenchmarkError(f"'{name}' was not found on PATH.")
    return path


def server_path(project_root: Path, runtime: str) -> Path:
    path = (
        project_root / "benchmarks" / "http" / "complex"
        / runtime / "server.mjs"
    )
    if not path.exists():
        raise BenchmarkError(f"Missing server file: {path}")
    return path


def runtime_command(project_root: Path, runtime: str) -> list[str]:
    path = server_path(project_root, runtime)
    if runtime == "node":
        return [executable("node"), str(path)]
    if runtime == "bun":
        return [executable("bun"), str(path)]
    if runtime == "deno":
        return [
            executable("deno"), "run",
            "--allow-net=127.0.0.1:3000", str(path)
        ]
    raise BenchmarkError(f"Unsupported runtime: {runtime}")


def autocannon_path(project_root: Path) -> Path:
    name = "autocannon.cmd" if os.name == "nt" else "autocannon"
    path = project_root / "node_modules" / ".bin" / name
    if not path.exists():
        raise BenchmarkError(
            f"Autocannon was not found at {path}. Run npm install."
        )
    return path


def shell_command(program: Path, args: list[str]) -> list[str]:
    if os.name == "nt":
        command_line = subprocess.list2cmdline([str(program), *args])
        return [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d", "/s", "/c", command_line
        ]
    return [str(program), *args]


def command_output(command: list[str], cwd: Path) -> str:
    result = subprocess.run(
        command, cwd=cwd, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace", timeout=20, check=False
    )
    if result.returncode != 0:
        raise BenchmarkError(
            f"Command failed: {' '.join(command)}\n{result.stdout}"
        )
    return result.stdout.strip()


def fixture_text(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BenchmarkError(f"Invalid fixture: {path}") from error
    if not isinstance(payload, dict):
        raise BenchmarkError("The fixture must contain a JSON object.")
    return json.dumps(payload, separators=(",", ":"))


def metadata(
    project_root: Path,
    cannon: Path,
    fixture: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    implementation_files = {
        "fixture": fixture,
        "shared_logic": (
            project_root / "benchmarks" / "http" / "complex"
            / "shared" / "process_order.mjs"
        ),
        **{
            f"{runtime}_server": server_path(project_root, runtime)
            for runtime in RUNTIMES
        },
    }

    for label, path in implementation_files.items():
        if not path.exists():
            raise BenchmarkError(f"Missing {label}: {path}")

    runtime_versions = {}
    for runtime in RUNTIMES:
        path = executable(runtime)
        runtime_versions[runtime] = {
            "executable": path,
            "version": command_output([path, "--version"], project_root),
        }

    return {
        "protocol": {
            "workload": WORKLOAD,
            "method": "POST",
            "url": PROCESS_URL,
            "health_url": HEALTH_URL,
            "connections": list(CONNECTIONS),
            "pipelining": args.pipelining,
            "warmup_seconds": args.warmup,
            "measurement_seconds": args.duration,
            "timeout_seconds": args.timeout,
            "cooldown_seconds": args.cooldown,
            "repetitions_per_configuration": 10,
            "total_observations": 90,
            "base_seed": args.seed,
            "expected_body": EXPECTED_BODY,
        },
        "implementation_hashes": {
            label: {
                "path": path.relative_to(project_root).as_posix(),
                "sha256": sha256(path),
            }
            for label, path in implementation_files.items()
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
            "runtimes": runtime_versions,
            "autocannon": {
                "executable": str(cannon),
                "version": command_output(
                    shell_command(cannon, ["--version"]), project_root
                ),
            },
        },
    }


def freeze_metadata(path: Path, current: dict[str, Any]) -> None:
    if not path.exists():
        path.write_text(
            json.dumps({"created_at": utc_now(), **current}, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Created frozen metadata: {path}")
        return

    existing = json.loads(path.read_text(encoding="utf-8"))
    for section in ("protocol", "implementation_hashes", "tools"):
        if existing.get(section) != current.get(section):
            raise BenchmarkError(
                "The protocol, implementation, or environment differs "
                f"from the frozen metadata. Inspect {path}."
            )
    print("Frozen protocol and environment verified.")


def ensure_port_free() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((HOST, PORT))
        except OSError as error:
            raise BenchmarkError(
                f"Port {PORT} is in use. Stop the manual validation server."
            ) from error


def http_request(
    url: str, method: str, body: str | None = None
) -> tuple[int, str, str]:
    data = body.encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body else {}
    request = urllib.request.Request(
        url, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return (
                response.status,
                response.headers.get("Content-Type", ""),
                response.read().decode("utf-8"),
            )
    except urllib.error.URLError as error:
        raise BenchmarkError(f"Endpoint validation failed: {error}") from error


def validate_server(body: str) -> None:
    status, content_type, health_body = http_request(
        HEALTH_URL, "GET"
    )
    if status != 200 or not content_type.startswith("application/json"):
        raise BenchmarkError("Health endpoint validation failed.")
    try:
        if json.loads(health_body) != {"status": "ready"}:
            raise BenchmarkError("Unexpected health response body.")
    except json.JSONDecodeError as error:
        raise BenchmarkError("Health endpoint returned invalid JSON.") from error

    status, content_type, response_body = http_request(
        PROCESS_URL, "POST", body
    )
    if status != 200:
        raise BenchmarkError(f"POST /process returned HTTP {status}.")
    if not content_type.startswith("application/json"):
        raise BenchmarkError("POST /process returned an invalid content type.")
    if response_body != EXPECTED_BODY:
        raise BenchmarkError(
            f"Unexpected body. Expected {EXPECTED_BODY}; got {response_body}"
        )


def wait_for_server(
    process: subprocess.Popen[str], body: str, timeout: int = 15
) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BenchmarkError("Server exited before becoming ready.")
        try:
            validate_server(body)
            return
        except BenchmarkError as error:
            last_error = str(error)
            time.sleep(0.25)
    raise BenchmarkError(
        f"Server was not ready within {timeout}s. Last error: {last_error}"
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
    cannon: Path,
    fixture: Path,
    *,
    connections: int,
    duration: int,
    timeout: int,
    pipelining: int,
    json_output: bool,
    stderr_path: Path,
) -> str:
    args = [
        "--connections", str(connections),
        "--duration", str(duration),
        "--pipelining", str(pipelining),
        "--timeout", str(timeout),
        "--method", "POST",
        "--headers", "Content-Type=application/json",
        "--input", str(fixture),
        "--no-progress",
    ]
    if json_output:
        args.append("--json")
    args.append(PROCESS_URL)

    with stderr_path.open("w", encoding="utf-8", newline="") as error_file:
        result = subprocess.run(
            shell_command(cannon, args),
            cwd=project_root,
            stdout=subprocess.PIPE if json_output else subprocess.DEVNULL,
            stderr=error_file,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=duration + timeout + 30,
            check=False,
        )

    if result.returncode != 0:
        details = stderr_path.read_text(
            encoding="utf-8", errors="replace"
        )
        raise BenchmarkError(
            f"Autocannon exited with code {result.returncode}.\n{details}"
        )
    return result.stdout or ""


def validate_result(
    result: dict[str, Any], connections: int, pipelining: int
) -> None:
    for section in ("latency", "requests", "throughput"):
        if section not in result:
            raise BenchmarkError(f"Missing result section: {section}")
    if result.get("url") != PROCESS_URL:
        raise BenchmarkError("Unexpected result URL.")
    if result.get("connections") != connections:
        raise BenchmarkError("Unexpected connection count.")
    if result.get("pipelining") != pipelining:
        raise BenchmarkError("Unexpected pipelining value.")
    if result["requests"].get("total", 0) <= 0:
        raise BenchmarkError("No requests were completed.")


def status_for(result: dict[str, Any]) -> str:
    total = sum(
        int(result.get(name, 0))
        for name in ("errors", "timeouts", "mismatches", "non2xx")
    )
    return "success" if total == 0 else "quality_warning"


def manifest_record(
    *,
    run_id: str,
    item: dict[str, Any],
    args: argparse.Namespace,
    started_at: str,
    finished_at: str,
    status: str,
    result: dict[str, Any] | None,
    paths: dict[str, Path],
    project_root: Path,
    error_message: str = "",
) -> dict[str, Any]:
    requests = result.get("requests", {}) if result else {}
    latency = result.get("latency", {}) if result else {}
    throughput = result.get("throughput", {}) if result else {}

    return {
        "run_id": run_id,
        "workload": WORKLOAD,
        "session_id": item["session_id"],
        "sequence": item["sequence"],
        "repetition": item["repetition"],
        "runtime": item["runtime"],
        "connections": item["connections"],
        "pipelining": args.pipelining,
        "warmup_seconds": args.warmup,
        "measurement_seconds": args.duration,
        "timeout_seconds": args.timeout,
        "cooldown_seconds": args.cooldown,
        "shuffle_seed": item["shuffle_seed"],
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "requests_per_second": requests.get("average", ""),
        "mean_latency_ms": latency.get("mean", ""),
        "median_latency_ms": latency.get("p50", ""),
        "p90_latency_ms": latency.get("p90", ""),
        "p97_5_latency_ms": latency.get("p97_5", ""),
        "p99_latency_ms": latency.get("p99", ""),
        "maximum_latency_ms": latency.get("max", ""),
        "total_requests": requests.get("total", ""),
        "bytes_per_second": throughput.get("average", ""),
        "errors": result.get("errors", "") if result else "",
        "timeouts": result.get("timeouts", "") if result else "",
        "mismatches": result.get("mismatches", "") if result else "",
        "non_2xx": result.get("non2xx", "") if result else "",
        "result_file": paths["result"].relative_to(project_root).as_posix(),
        "server_output_log": paths["server_out"].relative_to(
            project_root
        ).as_posix(),
        "server_error_log": paths["server_err"].relative_to(
            project_root
        ).as_posix(),
        "warmup_error_log": paths["warmup_err"].relative_to(
            project_root
        ).as_posix(),
        "autocannon_error_log": paths["cannon_err"].relative_to(
            project_root
        ).as_posix(),
        "error_message": error_message,
    }


def upsert_manifest(path: Path, record: dict[str, Any]) -> None:
    rows: list[dict[str, str]] = []
    if path.exists():
        with path.open(
            "r", encoding="utf-8-sig", newline=""
        ) as manifest_file:
            rows = list(csv.DictReader(manifest_file))

    rows = [row for row in rows if row.get("run_id") != record["run_id"]]
    rows.append({
        field: str(record.get(field, "")) for field in MANIFEST_FIELDS
    })
    rows.sort(key=lambda row: (
        int(row["session_id"]),
        int(row["repetition"]),
        int(row["sequence"]),
    ))

    temporary = path.with_suffix(".tmp")
    with temporary.open(
        "w", encoding="utf-8", newline=""
    ) as manifest_file:
        writer = csv.DictWriter(
            manifest_file, fieldnames=MANIFEST_FIELDS
        )
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def execution_plan(session: int, seed: int) -> list[dict[str, Any]]:
    combinations = [
        (runtime, connections)
        for runtime in RUNTIMES
        for connections in CONNECTIONS
    ]
    plan = []
    sequence = 0

    for repetition in SESSION_REPETITIONS[session]:
        shuffle_seed = seed + repetition
        shuffled = combinations.copy()
        random.Random(shuffle_seed).shuffle(shuffled)

        for runtime, connections in shuffled:
            sequence += 1
            plan.append({
                "session_id": session,
                "sequence": sequence,
                "repetition": repetition,
                "runtime": runtime,
                "connections": connections,
                "shuffle_seed": shuffle_seed,
            })
    return plan


def save_plan(path: Path, plan: list[dict[str, Any]]) -> None:
    fields = [
        "session_id", "sequence", "repetition",
        "runtime", "connections", "shuffle_seed"
    ]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(plan)


def run_observation(
    *,
    project_root: Path,
    cannon: Path,
    fixture: Path,
    body: str,
    output_dir: Path,
    log_dir: Path,
    manifest_path: Path,
    item: dict[str, Any],
    args: argparse.Namespace,
) -> str:
    runtime = item["runtime"]
    connections = item["connections"]
    run_id = (
        f"http_complex_s{item['session_id']:02d}"
        f"_r{item['repetition']:02d}_{runtime}_c{connections:03d}"
    )

    paths = {
        "result": output_dir / f"{run_id}.json",
        "server_out": log_dir / f"{run_id}-server-output.log",
        "server_err": log_dir / f"{run_id}-server-error.log",
        "warmup_err": log_dir / f"{run_id}-warmup-error.log",
        "cannon_err": log_dir / f"{run_id}-autocannon-error.log",
    }

    if paths["result"].exists() and not args.force:
        existing = json.loads(
            paths["result"].read_text(encoding="utf-8")
        )
        validate_result(existing, connections, args.pipelining)
        status = status_for(existing)
        upsert_manifest(
            manifest_path,
            manifest_record(
                run_id=run_id,
                item=item,
                args=args,
                started_at=existing.get("start", ""),
                finished_at=existing.get("finish", ""),
                status=status,
                result=existing,
                paths=paths,
                project_root=project_root,
            ),
        )
        print(f"Skipping existing valid result: {run_id}")
        return "skipped"

    ensure_port_free()
    process: subprocess.Popen[str] | None = None
    result: dict[str, Any] | None = None
    started_at = utc_now()
    status = "failed"
    error_message = ""

    try:
        print(
            f"\n[{item['sequence']}/45] {run_id}\n"
            f"Starting {runtime} at {connections} connections..."
        )

        with (
            paths["server_out"].open(
                "w", encoding="utf-8", newline=""
            ) as stdout_file,
            paths["server_err"].open(
                "w", encoding="utf-8", newline=""
            ) as stderr_file,
        ):
            process = subprocess.Popen(
                runtime_command(project_root, runtime),
                cwd=project_root,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                ),
            )

            wait_for_server(process, body)
            print("Server ready and response validated.")

            print(f"Warm-up: {args.warmup} seconds...")
            run_autocannon(
                project_root, cannon, fixture,
                connections=connections,
                duration=args.warmup,
                timeout=args.timeout,
                pipelining=args.pipelining,
                json_output=False,
                stderr_path=paths["warmup_err"],
            )

            if process.poll() is not None:
                raise BenchmarkError("Server exited during warm-up.")

            validate_server(body)

            print(f"Measured run: {args.duration} seconds...")
            output = run_autocannon(
                project_root, cannon, fixture,
                connections=connections,
                duration=args.duration,
                timeout=args.timeout,
                pipelining=args.pipelining,
                json_output=True,
                stderr_path=paths["cannon_err"],
            ).strip()

            if not output:
                raise BenchmarkError("Autocannon returned no JSON.")

            result = json.loads(output)
            validate_result(result, connections, args.pipelining)
            validate_server(body)
            status = status_for(result)

            paths["result"].write_text(
                json.dumps(result, indent=2) + "\n",
                encoding="utf-8",
            )

            print(
                f"Completed: {run_id}\n"
                f"Requests/sec: {result['requests']['average']}\n"
                f"Mean latency: {result['latency']['mean']} ms\n"
                f"P99 latency: {result['latency']['p99']} ms\n"
                f"Status: {status}"
            )

    except (
        BenchmarkError,
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as error:
        error_message = str(error)
        print(f"Failed: {run_id}\n{error_message}", file=sys.stderr)

    finally:
        stop_process(process)
        upsert_manifest(
            manifest_path,
            manifest_record(
                run_id=run_id,
                item=item,
                args=args,
                started_at=started_at,
                finished_at=utc_now(),
                status=status,
                result=result,
                paths=paths,
                project_root=project_root,
                error_message=error_message,
            ),
        )

    return status


def main() -> int:
    args = parse_args()
    project_root = Path(__file__).resolve().parents[1]
    cannon = autocannon_path(project_root)

    fixture = (
        project_root / "data" / "fixtures"
        / "http" / "complex_request.json"
    )
    if not fixture.exists():
        raise BenchmarkError(f"Fixture not found: {fixture}")
    body = fixture_text(fixture)

    raw_dir = project_root / "data" / "raw" / "http" / "complex"
    session_dir = raw_dir / f"session_{args.session:02d}"
    log_dir = (
        project_root / "results" / "logs"
        / "http_complex_final" / f"session_{args.session:02d}"
    )
    session_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = raw_dir / "http_complex_final_manifest.csv"
    metadata_path = raw_dir / "http_complex_final_metadata.json"
    plan_path = session_dir / "execution_order.csv"

    freeze_metadata(
        metadata_path,
        metadata(project_root, cannon, fixture, args),
    )

    plan = execution_plan(args.session, args.seed)
    save_plan(plan_path, plan)

    print("\nFinal complex HTTP benchmark")
    print("----------------------------")
    print(f"Session:            {args.session}")
    print(f"Repetitions:        {list(SESSION_REPETITIONS[args.session])}")
    print(f"Observations:       {len(plan)}")
    print(f"Endpoint:           POST {PROCESS_URL}")
    print(f"Warm-up:            {args.warmup} seconds")
    print(f"Measured duration:  {args.duration} seconds")
    print(f"Cool-down:          {args.cooldown} seconds")
    print(f"Execution plan:     {plan_path}")
    print(f"Manifest:           {manifest_path}")

    outcomes: list[str] = []

    for index, item in enumerate(plan, start=1):
        outcome = run_observation(
            project_root=project_root,
            cannon=cannon,
            fixture=fixture,
            body=body,
            output_dir=session_dir,
            log_dir=log_dir,
            manifest_path=manifest_path,
            item=item,
            args=args,
        )
        outcomes.append(outcome)

        if outcome == "failed" and args.stop_on_failure:
            break

        if index < len(plan) and outcome != "skipped":
            print(f"Cool-down: {args.cooldown} seconds...")
            time.sleep(args.cooldown)

    counts = Counter(outcomes)
    print("\nSession summary")
    print("---------------")
    print(f"Successful:       {counts['success']}")
    print(f"Quality warnings: {counts['quality_warning']}")
    print(f"Skipped:          {counts['skipped']}")
    print(f"Failed:           {counts['failed']}")

    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

#  py -3.13 runner\run_http_complex_final.py --session 1 --stop-on-failure
#  py -3.13 runner\run_http_complex_final.py --session 2 --stop-on-failure