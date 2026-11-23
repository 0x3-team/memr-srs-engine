import http from "node:http";
import https from "node:https";
import { randomBytes } from "node:crypto";
import { Transform } from "node:stream";
import * as zlib from "node:zlib";
import { fileURLToPath } from "node:url";

const METHODS = { sync: new Set(["hostKey", "meta", "start", "applyGraves", "applyChanges", "chunk", "applyChunk", "sanityCheck2", "finish", "abort", "upload", "download"]),
  msync: new Set(["begin", "mediaChanges", "uploadChanges", "downloadFiles", "mediaSanity"]) };
// Match the pinned server's default compressed and expanded payload bounds.
const COMPRESSED_LIMIT = 100 * 1024 * 1024;
const EXPANDED_LIMIT = 300 * 1024 * 1024;
const REQUEST_HEADERS = ["anki-sync", "content-type", "content-length", "content-encoding", "accept", "user-agent"];
// Chunked completion must wait for the decoder's final size check. Forwarding
// Content-Length lets a client accept the last byte before that check runs.
const RESPONSE_HEADERS = ["anki-original-size", "content-type", "content-encoding"];

function destination(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error("invalid_params"); }
  const host = url.hostname.toLowerCase().replace(/\.$/u, "");
  if (["ankiweb.net", "ankiuser.net"].some(domain => host === domain || host.endsWith(`.${domain}`))) throw new Error("unsupported");
  const loopback = host === "localhost" || host === "[::1]" || /^127\.\d+\.\d+\.\d+$/u.test(host);
  if (url.protocol !== "https:" && !(url.protocol === "http:" && loopback)) throw new Error("unsupported");
  if (url.username || url.password || url.search || url.hash || !url.pathname.endsWith("/")) throw new Error("invalid_params");
  return url;
}

function uploadLimit() {
  let count = 0;
  return new Transform({ transform(bytes, _encoding, done) {
    count += bytes.length;
    done(count > COMPRESSED_LIMIT ? new Error("transfer_limit") : null, bytes);
  } });
}

// Validate compressed responses before passing their chunks to the native
// client, whose declared-size allocation otherwise trusts the remote server.
function checkedResponse(expected) {
  const decoder = zlib.createZstdDecompress();
  let compressed = 0;
  let expanded = 0;
  const stream = new Transform({
    transform(bytes, _encoding, done) {
      compressed += bytes.length;
      if (compressed > COMPRESSED_LIMIT) { done(new Error("transfer_limit")); return; }
      decoder.write(bytes, cause => done(cause, cause ? undefined : bytes));
    },
    flush(done) {
      decoder.once("end", () => done(expanded === expected ? null : new Error("invalid_response")));
      decoder.end();
    },
    destroy(cause, done) { decoder.destroy(); done(cause); },
  });
  decoder.on("data", bytes => {
    expanded += bytes.length;
    if (expanded > expected || expanded > EXPANDED_LIMIT) stream.destroy(new Error("transfer_limit"));
  });
  decoder.on("error", () => stream.destroy(new Error("invalid_response")));
  return stream;
}

export async function createSyncBridge(endpoint, onFault = () => {}) {
  const target = destination(endpoint);
  if (typeof zlib.createZstdDecompress !== "function") throw new Error("unsupported");
  const token = randomBytes(32).toString("base64url");
  const active = new Set();
  let closing;
  const server = http.createServer({ maxHeaderSize: 16384, requestTimeout: 600000, headersTimeout: 10000 }, (request, response) => {
    const match = new RegExp(`^/${token}/(sync|msync)/([A-Za-z0-9]+)$`, "u").exec(request.url ?? "");
    if (closing || request.method !== "POST" || request.headers.origin || request.headers.host !== `127.0.0.1:${server.address()?.port}`
        || !match || !METHODS[match[1]].has(match[2])) {
      response.writeHead(403, { "Content-Length": "0", Connection: "close" }); response.end(); return;
    }
    const headers = Object.fromEntries(REQUEST_HEADERS.filter(name => request.headers[name] !== undefined).map(name => [name, request.headers[name]]));
    const remote = (target.protocol === "https:" ? https : http).request(new URL(`${match[1]}/${match[2]}`, target), { method: "POST", headers, agent: false });
    const upload = uploadLimit();
    active.add(remote);
    const fail = () => {
      remote.destroy(); upload.destroy();
      if (response.headersSent) response.destroy();
      else { response.writeHead(502, { "Content-Length": "0", Connection: "close" }); response.end(); }
    };
    remote.once("close", () => active.delete(remote));
    remote.once("error", fail); upload.once("error", fail); request.once("error", fail);
    response.once("close", () => { remote.destroy(); upload.destroy(); });
    remote.setTimeout(30000, fail);
    remote.once("response", incoming => {
      const code = incoming.statusCode ?? 502;
      if (code >= 300 && code < 400) {
        onFault("redirect_refused"); incoming.destroy(); fail(); return;
      }
      if (code < 200 || code >= 300) {
        incoming.destroy(); response.writeHead(code, { "Content-Length": "0", Connection: "close" }); response.end(); return;
      }
      const original = incoming.headers["anki-original-size"];
      if (typeof original !== "string" || !/^\d{1,9}$/u.test(original) || Number(original) > EXPANDED_LIMIT) {
        onFault("response_limit"); incoming.destroy(); fail(); return;
      }
      response.writeHead(code, Object.fromEntries(RESPONSE_HEADERS.filter(name => incoming.headers[name] !== undefined).map(name => [name, incoming.headers[name]])));
      const download = checkedResponse(Number(original));
      incoming.once("error", () => { download.destroy(); response.destroy(); });
      download.once("error", () => { onFault("invalid_response"); incoming.destroy(); response.destroy(); });
      response.once("close", () => { incoming.destroy(); download.destroy(); });
      incoming.pipe(download).pipe(response);
    });
    request.pipe(upload).pipe(remote);
  });
  server.maxConnections = 8;
  server.on("clientError", (_cause, socket) => socket.destroy());
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen({ host: "127.0.0.1", port: 0, exclusive: true }, () => { server.off("error", reject); resolve(); });
  });
  return {
    endpoint: `http://127.0.0.1:${server.address().port}/${token}/`,
    close() {
      if (!closing) {
        for (const request of active) request.destroy();
        server.closeAllConnections();
        closing = new Promise((resolve, reject) => server.close(cause => cause ? reject(cause) : resolve()));
      }
      return closing;
    },
  };
}

async function main() {
  let bridge;
  let starting;
  let stopping = false;
  let input = Buffer.alloc(0);
  const sent = new Set();
  const send = value => process.stdout.write(`${JSON.stringify(value)}\n`);
  const stop = async () => {
    if (stopping) return;
    stopping = true;
    await starting?.catch(() => {});
    await bridge?.close();
    process.exit(0);
  };
  process.on("SIGTERM", () => void stop()); process.on("SIGINT", () => void stop());
  process.stdout.on("error", () => void stop()); process.stdin.on("end", () => void stop()); process.stdin.on("error", () => void stop());
  process.stdin.on("data", bytes => {
    if (starting || stopping) return;
    input = Buffer.concat([input, bytes]);
    if (input.length > 4096) { send({ error: "invalid_params" }); void stop(); return; }
    const end = input.indexOf(10);
    if (end < 0) return;
    starting = (async () => {
      try {
        const params = JSON.parse(input.subarray(0, end)); input = Buffer.alloc(0);
        if (!params || Object.keys(params).join() !== "endpoint") throw new Error("invalid_params");
        bridge = await createSyncBridge(params.endpoint, fault => { if (!sent.has(fault)) { sent.add(fault); send({ fault }); } });
        if (!stopping) send({ endpoint: bridge.endpoint });
      } catch (cause) {
        send({ error: ["unsupported", "invalid_params"].includes(cause.message) ? cause.message : "internal" });
        setImmediate(() => void stop());
      }
    })();
  });
  process.stdin.resume();
}

if (process.argv[1] === fileURLToPath(import.meta.url)) await main();
