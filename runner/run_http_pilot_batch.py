from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path


CONNECTION_LEVELS = (10, 50, 100)

RUNTIME_ORDERS = (
    ("node", "bun", "deno"),
    ("bun", "deno", "node"),
    ("deno", "node", "bun"),
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the complete repeated HTTP pilot experiment."
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
        help="Request timeout in seconds. Default: 10.",
    )

    parser.add_argument(
        "--pipelining",
        type=int,
        default=1,
        help="Pipelining factor. Default: 1.",
    )

    parser.add_argument(
        "--cooldown",
        type=int,
        default=5,
        help="Cool-down period between observations. Default: 5.",
    )

    return parser.parse_args()


def build_command(
    single_runner: Path,
    *,
    runtime: str,
    connections: int,
    repetition: int,
    warmup: int,
    duration: int,
    timeout: int,
    pipelining: int,
) -> list[str]:
    return [
        sys.executable,
        str(single_runner),
        "--runtime",
        runtime,
        "--connections",
        str(connections),
        "--repetition",
        str(repetition),
        "--warmup",
        str(warmup),
        "--duration",
        str(duration),
        "--timeout",
        str(timeout),
        "--pipelining",
        str(pipelining),
    ]


def main() -> int:
    args = parse_arguments()

    project_root = Path(__file__).resolve().parents[1]
    single_runner = project_root / "runner" / "run_http_pilot.py"

    if not single_runner.exists():
        print(
            f"Single-observation runner not found:\n{single_runner}",
            file=sys.stderr,
        )
        return 1

    total_observations = (
        len(CONNECTION_LEVELS)
        * len(RUNTIME_ORDERS)
        * len(RUNTIME_ORDERS[0])
    )

    completed = 0
    failed_runs: list[str] = []

    print("\nRepeated HTTP pilot")
    print("-------------------")
    print(f"Connection levels: {CONNECTION_LEVELS}")
    print(f"Repetitions:       {len(RUNTIME_ORDERS)}")
    print(f"Total observations: {total_observations}")

    for repetition, runtime_order in enumerate(
        RUNTIME_ORDERS,
        start=1,
    ):
        print(
            f"\nRepetition {repetition} runtime order: "
            f"{' → '.join(runtime_order)}"
        )

        for connections in CONNECTION_LEVELS:
            print(f"\nConnection level: {connections}")

            for runtime in runtime_order:
                run_id = (
                    f"{runtime}"
                    f"_c{connections:03d}"
                    f"_r{repetition:02d}"
                )

                command = build_command(
                    single_runner,
                    runtime=runtime,
                    connections=connections,
                    repetition=repetition,
                    warmup=args.warmup,
                    duration=args.duration,
                    timeout=args.timeout,
                    pipelining=args.pipelining,
                )

                print(
                    f"\n[{completed + 1}/{total_observations}] "
                    f"Starting {run_id}"
                )

                completed_process = subprocess.run(
                    command,
                    cwd=project_root,
                    check=False,
                )

                completed += 1

                if completed_process.returncode != 0:
                    failed_runs.append(run_id)
                    print(
                        f"\nRun failed: {run_id}",
                        file=sys.stderr,
                    )
                else:
                    print(f"\nRun completed: {run_id}")

                if completed < total_observations:
                    print(
                        f"Applying {args.cooldown}-second cool-down..."
                    )
                    time.sleep(args.cooldown)

    print("\nPilot batch complete.")
    print(f"Attempted observations: {completed}")
    print(f"Successful observations: {completed - len(failed_runs)}")
    print(f"Failed observations: {len(failed_runs)}")

    if failed_runs:
        print("\nFailed run IDs:", file=sys.stderr)

        for run_id in failed_runs:
            print(f"- {run_id}", file=sys.stderr)

        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())