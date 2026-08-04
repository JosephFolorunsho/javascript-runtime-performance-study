const http = require("node:http");

const HOST = "127.0.0.1";
const PORT = 3000;

const RESPONSE_BODY =
  '{"message":"JavaScript runtime benchmark","status":"success"}';

const NOT_FOUND_BODY = '{"error":"Not found"}';

const server = http.createServer((request, response) => {
  if (request.method === "GET" && request.url === "/json") {
    response.writeHead(200, {
      "Content-Type": "application/json; charset=utf-8",
    });

    response.end(RESPONSE_BODY);
    return;
  }

  response.writeHead(404, {
    "Content-Type": "application/json; charset=utf-8",
  });

  response.end(NOT_FOUND_BODY);
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
      runtime: "node",
      host: HOST,
      port: PORT,
    }),
  );
});
