const HOST = "127.0.0.1";
const PORT = 3000;

const RESPONSE_BODY =
  '{"message":"JavaScript runtime benchmark","status":"success"}';

const NOT_FOUND_BODY = '{"error":"Not found"}';

const server = Bun.serve({
  hostname: HOST,
  port: PORT,

  fetch(request) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/json") {
      return new Response(RESPONSE_BODY, {
        status: 200,
        headers: {
          "Content-Type": "application/json; charset=utf-8",
        },
      });
    }

    return new Response(NOT_FOUND_BODY, {
      status: 404,
      headers: {
        "Content-Type": "application/json; charset=utf-8",
      },
    });
  },

  error(error) {
    console.error(
      JSON.stringify({
        event: "request_error",
        runtime: "bun",
        message: error.message,
      }),
    );

    return new Response('{"error":"Internal server error"}', {
      status: 500,
      headers: {
        "Content-Type": "application/json; charset=utf-8",
      },
    });
  },
});

console.log(
  JSON.stringify({
    event: "ready",
    runtime: "bun",
    host: server.hostname,
    port: server.port,
  }),
);
