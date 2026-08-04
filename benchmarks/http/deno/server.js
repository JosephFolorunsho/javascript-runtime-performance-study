const HOST = "127.0.0.1";
const PORT = 3000;

const RESPONSE_BODY =
  '{"message":"JavaScript runtime benchmark","status":"success"}';

const NOT_FOUND_BODY = '{"error":"Not found"}';

Deno.serve(
  {
    hostname: HOST,
    port: PORT,

    onListen({ hostname, port }) {
      console.log(
        JSON.stringify({
          event: "ready",
          runtime: "deno",
          host: hostname,
          port,
        }),
      );
    },
  },

  (request) => {
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
);
