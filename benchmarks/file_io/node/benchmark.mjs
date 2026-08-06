import {
  readFile,
  writeFile,
} from "node:fs/promises";

function fail(message) {
  console.error(message);
  process.exit(1);
}

function validateData(data, expectedBytes) {
  if (data.byteLength !== expectedBytes) {
    fail(
      `Unexpected byte length: expected ${expectedBytes}, got ${data.byteLength}`,
    );
  }

  const view = data instanceof Uint8Array
    ? data
    : new Uint8Array(data);

  if (view[0] !== 0 || view[view.length - 1] !== 255) {
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
] = process.argv.slice(2);

const expectedBytes = Number(expectedBytesText);

if (!["read", "write"].includes(operation)) {
  fail("Operation must be 'read' or 'write'.");
}

if (!Number.isInteger(expectedBytes) || expectedBytes <= 0) {
  fail("Expected byte count must be a positive integer.");
}

if (operation === "read") {
  const warmupData = await readFile(inputPath);
  validateData(warmupData, expectedBytes);

  const start = performance.now();
  const measuredData = await readFile(inputPath);
  const end = performance.now();

  validateData(measuredData, expectedBytes);

  process.stdout.write(
    JSON.stringify(result("read", measuredData.byteLength, end - start)),
  );
} else {
  const payload = await readFile(inputPath);
  validateData(payload, expectedBytes);

  const start = performance.now();
  await writeFile(outputPath, payload);
  const end = performance.now();

  process.stdout.write(
    JSON.stringify(result("write", payload.byteLength, end - start)),
  );
}
