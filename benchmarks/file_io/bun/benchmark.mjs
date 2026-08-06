function fail(message) {
  console.error(message);
  process.exit(1);
}

function validateData(data, expectedBytes) {
  const view = data instanceof Uint8Array
    ? data
    : new Uint8Array(data);

  if (view.byteLength !== expectedBytes) {
    fail(
      `Unexpected byte length: expected ${expectedBytes}, got ${view.byteLength}`,
    );
  }

  if (view[0] !== 0 || view[view.length - 1] !== 255) {
    fail("Fixture-content validation failed.");
  }

  return view;
}

function result(operation, bytes, durationMs) {
  return {
    operation,
    bytes,
    duration_ms: durationMs,
    mib_per_second:
      (bytes / (1024 * 1024)) / (durationMs / 1000),
    status: "success",
  };
}

const [
  operation,
  inputPath,
  outputPath,
  expectedBytesText,
] = Bun.argv.slice(2);

const expectedBytes = Number(expectedBytesText);

if (!["read", "write"].includes(operation)) {
  fail("Operation must be 'read' or 'write'.");
}

if (!Number.isInteger(expectedBytes) || expectedBytes <= 0) {
  fail("Expected byte count must be a positive integer.");
}

if (operation === "read") {
  const warmupBuffer = await Bun.file(inputPath).arrayBuffer();
  validateData(warmupBuffer, expectedBytes);

  const start = performance.now();
  const measuredBuffer = await Bun.file(inputPath).arrayBuffer();
  const end = performance.now();

  validateData(measuredBuffer, expectedBytes);

  process.stdout.write(
    JSON.stringify(result("read", measuredBuffer.byteLength, end - start)),
  );
} else {
  const payload = validateData(
    await Bun.file(inputPath).arrayBuffer(),
    expectedBytes,
  );

  const start = performance.now();
  const bytesWritten = await Bun.write(outputPath, payload);
  const end = performance.now();

  if (bytesWritten !== expectedBytes) {
    fail(
      `Unexpected write count: expected ${expectedBytes}, got ${bytesWritten}`,
    );
  }

  process.stdout.write(
    JSON.stringify(result("write", bytesWritten, end - start)),
  );
}
