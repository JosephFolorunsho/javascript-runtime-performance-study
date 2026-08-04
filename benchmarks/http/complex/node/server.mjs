import http from "node:http";

import { processOrder, ValidationError } from "../shared/process_order.mjs";

const HOST = "127.0.0.1";
const PORT = 3000;
const MAX_BODY_BYTES = 16 * 1024;

function sendJson(response, statusCode, payload) {
  const body = JSON.stringify(payload);

  response.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
  });

  response.end(body);
}

const server = http.createServer((request, response) => {
  if (request.method === "GET" && request.url === "/health") {
    sendJson(response, 200, {
      status: "ready",
    });

    return;
  }

  if (request.method !== "POST" || request.url !== "/process") {
    sendJson(response, 404, {
      error: "Not found",
    });

    return;
  }

  let body = "";
  let bodySize = 0;
  let requestRejected = false;

  request.setEncoding("utf8");

  request.on("data", (chunk) => {
    if (requestRejected) {
      return;
    }

    bodySize += Buffer.byteLength(chunk);

    if (bodySize > MAX_BODY_BYTES) {
      requestRejected = true;

      sendJson(response, 413, {
        error: "Request body too large",
      });

      return;
    }

    body += chunk;
  });

  request.on("end", () => {
    if (requestRejected) {
      return;
    }

    try {
      const payload = JSON.parse(body);
      const result = processOrder(payload);

      sendJson(response, 200, result);
    } catch (error) {
      if (error instanceof SyntaxError || error instanceof ValidationError) {
        sendJson(response, 400, {
          error: error.message,
        });

        return;
      }

      sendJson(response, 500, {
        error: "Internal server error",
      });
    }
  });

  request.on("error", () => {
    if (!response.headersSent) {
      sendJson(response, 400, {
        error: "Unable to read request body",
      });
    }
  });
});

server.on("error", (error) => {
  console.error(
    JSON.stringify({
      event: "server_error",
      runtime: "node",
      message: error.message,
    }),
  );

  process.exitCode = 1;
});

server.listen(PORT, HOST, () => {
  console.log(
    JSON.stringify({
      event: "ready",
      workload: "complex",
      runtime: "node",
      host: HOST,
      port: PORT,
    }),
  );
});
