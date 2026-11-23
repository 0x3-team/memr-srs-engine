import { areaMethods, empty, integer as i, object as o, string as s } from "./_schema.mjs";
import { mutation, syncServerStatus } from "./_shapes.mjs";
export default areaMethods("syncServer", [
  ["status", "Read loopback sync-server status and user names without credential material.", empty, syncServerStatus, { mutating: false, longOperation: false }],
  ["start", "Start the local sync server on loopback only; external reachability needs G-LAN.", o({ port: i(0, 65535) }, []), mutation({ status: syncServerStatus }), { mutating: true, longOperation: false }],
  ["stop", "Stop the supervised local sync-server child.", empty, mutation({ status: syncServerStatus }), { mutating: true, longOperation: false }],
  ["setUser", "Create or update a sync-server user with server-side password hashing.", o({ username: s(255), password: s(4096) }), mutation(), { mutating: true, longOperation: false }],
  ["removeUser", "Remove a local sync-server user after confirmation.", o({ username: s(255) }), mutation(), { mutating: true, longOperation: false }],
]);
