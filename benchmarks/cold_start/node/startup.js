const data = {
  runtime: "benchmark",
  status: "ready",
  values: [1, 2, 3, 4, 5],
};

JSON.stringify(data);

process.stdout.write("READY");
