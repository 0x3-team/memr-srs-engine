import { areaMethods, object as o } from "./_schema.mjs";
import { handle, mutation, progress } from "./_shapes.mjs";
export default areaMethods("progress", [
  ["get", "Poll a long operation, including its terminal outcome or typed error.", o({ operationId: handle }, []), progress, { mutating: false, longOperation: false }],
  ["abort", "Request cancellation of a specific abortable long operation.", o({ operationId: handle }), mutation(), { mutating: true, longOperation: false }],
]);
