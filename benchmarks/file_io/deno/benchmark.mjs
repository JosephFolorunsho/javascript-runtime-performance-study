function fail(message) {
  console.error(message);
  Deno.exit(1);
}

function validateData(data, expectedBytes) {
  if (data.byteLength !== expectedBytes) {
    fail(
      `Unexpected byte length: expected ${expectedBytes}, got ${data.byteLength}`,
    );
  }

  if (data[0] !== 0 || data[data.length - 1] !== 255) {
    fail("Fixture-content validation failed.");
  }
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
] = Deno.args;

const expectedBytes = Number(expectedBytesText);

if (!["read", "write"].includes(operation)) {
  fail("Operation must be 'read' or 'write'.");
}

if (!Number.isInteger(expectedBytes) || expectedBytes <= 0) {
  fail("Expected byte count must be a positive integer.");
}

if (operation === "read") {
  const warmupData = await Deno.readFile(inputPath);
  validateData(warmupData, expectedBytes);

  const start = performance.now();
  const measuredData = await Deno.readFile(inputPath);
  const end = performance.now();

  validateData(measuredData, expectedBytes);

  await Deno.stdout.write(
    new TextEncoder().encode(
      JSON.stringify(
        result("read", measuredData.byteLength, end - start),
      ),
    ),
  );
} else {
  const payload = await Deno.readFile(inputPath);
  validateData(payload, expectedBytes);

  const start = performance.now();
  await Deno.writeFile(outputPath, payload);
  const end = performance.now();

  await Deno.stdout.write(
    new TextEncoder().encode(
      JSON.stringify(
        result("write", payload.byteLength, end - start),
      ),
    ),
  );
}
