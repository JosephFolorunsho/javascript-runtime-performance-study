import { processOrder, ValidationError } from "../shared/process_order.mjs";

const HOST = "127.0.0.1";
const PORT = 3000;

function jsonResponse(status, payload) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
    },
  });
}

const server = Bun.serve({
  hostname: HOST,
  port: PORT,

  async fetch(request) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/health") {
      return jsonResponse(200, {
        status: "ready",
      });
    }

    if (request.method !== "POST" || url.pathname !== "/process") {
      return jsonResponse(404, {
        error: "Not found",
      });
    }

    try {
      const body = await request.text();
      const payload = JSON.parse(body);
      const result = processOrder(payload);

      return jsonResponse(200, result);
    } catch (error) {
      if (error instanceof SyntaxError || error instanceof ValidationError) {
        return jsonResponse(400, {
          error: error.message,
        });
      }

      return jsonResponse(500, {
        error: "Internal server error",
      });
    }
  },

  error(error) {
    console.error(
      JSON.stringify({
        event: "request_error",
        runtime: "bun",
        message: error.message,
      }),
    );

    return jsonResponse(500, {
      error: "Internal server error",
    });
  },
});

console.log(
  JSON.stringify({
    event: "ready",
    workload: "complex",
    runtime: "bun",
    host: server.hostname,
    port: server.port,
  }),
);
