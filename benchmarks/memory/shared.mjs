const ALLOCATION_BYTES = 100 * 1024 * 1024;
const HOLD_MS = 5000;

export async function runMemoryWorkload(mode) {
  if (mode !== "idle" && mode !== "allocated") {
    throw new Error(`Unsupported memory workload mode: ${mode}`);
  }

  let retained = null;

  if (mode === "allocated") {
    retained = new Uint8Array(ALLOCATION_BYTES);

    // Touch every byte so the allocation is physically committed rather
    // than remaining only as reserved virtual address space.
    retained.fill(1);

    // Keep a live reference for the full observation so the allocation
    // cannot be reclaimed during memory sampling.
    globalThis.__memoryBenchmarkRetained = retained;
  }

  const checksum = retained === null
    ? 0
    : retained[0] + retained[retained.length - 1];

  console.log(
    JSON.stringify({
      event: "READY",
      mode,
      allocation_bytes: mode === "allocated" ? ALLOCATION_BYTES : 0,
      checksum,
    }),
  );

  // Remain alive long enough for the external sampler to observe a
  // steady post-readiness memory footprint.
  await new Promise((resolve) => setTimeout(resolve, HOLD_MS));
}
