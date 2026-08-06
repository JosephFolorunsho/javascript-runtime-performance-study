from __future__ import annotations

import hashlib
from pathlib import Path

FILE_SIZE_BYTES = 100 * 1024 * 1024
BLOCK = bytes(range(256)) * 4096  # 1 MiB deterministic block


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    output_path = (
        project_root
        / "data"
        / "fixtures"
        / "file_io"
        / "read_fixture_100mib.bin"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists() and output_path.stat().st_size == FILE_SIZE_BYTES:
        print(f"Fixture already exists: {output_path}")
    else:
        with output_path.open("wb") as file_handle:
            remaining = FILE_SIZE_BYTES

            while remaining > 0:
                chunk = BLOCK[: min(len(BLOCK), remaining)]
                file_handle.write(chunk)
                remaining -= len(chunk)

        print(f"Created fixture: {output_path}")

    digest = hashlib.sha256(output_path.read_bytes()).hexdigest()

    print(f"Size: {output_path.stat().st_size} bytes")
    print(f"SHA-256: {digest}")


if __name__ == "__main__":
    main()

# py -3.13 runner\generate_file_io_fixture.py 