/** The deliberately small JSON-schema dialect shared by the gateway and client. */
export const boolean = { type: "boolean" };
export const string = (maxLength = 4096) => ({ type: "string", maxLength });
export const integer = (minimum = 0, maximum = Number.MAX_SAFE_INTEGER) => ({ type: "integer", minimum, maximum });
export const number = (minimum = 0, maximum = Number.MAX_VALUE) => ({ type: "number", minimum, maximum });
export const enumeration = (...values) => ({ type: typeof values[0], enum: values });
export const array = (items, maxItems = 10000) => ({ type: "array", items, maxItems });
export const object = (properties, required = Object.keys(properties)) => ({
  type: "object", properties, required, additionalProperties: false,
});
export const nullable = (schema) => ({ ...schema, type: [schema.type, "null"] });
export const empty = object({});

const hasOwn = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const keywords = new Set(["type", "properties", "required", "items", "enum", "minimum", "maximum", "maxLength", "maxItems", "additionalProperties"]);
const types = new Set(["object", "array", "string", "integer", "number", "boolean", "null"]);
const plainObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value)
  && [Object.prototype, null].includes(Object.getPrototypeOf(value));
const kindList = (schema) => Array.isArray(schema.type) ? schema.type : [schema.type];
const matchesType = (type, value) => type === "null" ? value === null
  : type === "object" ? plainObject(value)
  : type === "array" ? Array.isArray(value)
  : type === "integer" ? Number.isSafeInteger(value)
  : type === "number" ? typeof value === "number" && Number.isFinite(value)
  : typeof value === type;

/** Fail closed on unknown keywords, ill-formed bounds, open objects and cycles. */
export function validateSchema(schema, path = "$", ancestors = new Set()) {
  if (!plainObject(schema)) return [`${path}: schema must be an object`];
  if (ancestors.has(schema)) return [`${path}: cyclic schema`];
  const parents = new Set([...ancestors, schema]);
  const issues = [];
  const kinds = kindList(schema);
  const add = (message) => issues.push(`${path}: ${message}`);
  for (const key of Object.keys(schema)) if (!keywords.has(key)) add(`unsupported keyword ${key}`);
  if (!kinds.length || new Set(kinds).size !== kinds.length || kinds.some((kind) => !types.has(kind))) add("invalid type");
  if (kinds.filter((kind) => kind !== "null").length > 1) add("only nullable type unions are supported");
  if (kinds.includes("object")) {
    if (schema.additionalProperties !== false) add("objects must have additionalProperties:false");
    if (!plainObject(schema.properties)) add("object properties are required");
    else for (const [key, child] of Object.entries(schema.properties)) {
      if (["__proto__", "constructor", "prototype"].includes(key)) add(`unsafe property ${key}`);
      issues.push(...validateSchema(child, `${path}.${key}`, parents));
    }
    if (!Array.isArray(schema.required) || new Set(schema.required).size !== schema.required.length
      || schema.required.some((key) => typeof key !== "string" || !hasOwn(schema.properties ?? {}, key))) add("invalid required properties");
  } else if (["properties", "required", "additionalProperties"].some((key) => hasOwn(schema, key))) add("object keywords on a non-object");
  if (kinds.includes("array")) issues.push(...validateSchema(schema.items, `${path}[]`, parents));
  else if (hasOwn(schema, "items")) add("items on a non-array");
  for (const [key, kind] of [["maxLength", "string"], ["maxItems", "array"]]) {
    if (hasOwn(schema, key) && (!kinds.includes(kind) || !Number.isSafeInteger(schema[key]) || schema[key] < 0)) add(`invalid ${key}`);
  }
  for (const key of ["minimum", "maximum"]) {
    if (hasOwn(schema, key) && (!kinds.some((kind) => ["number", "integer"].includes(kind)) || !Number.isFinite(schema[key]))) add(`invalid ${key}`);
    if (kinds.includes("integer") && hasOwn(schema, key) && !Number.isSafeInteger(schema[key])) add(`unsafe integer ${key}`);
  }
  if (schema.minimum > schema.maximum) add("minimum exceeds maximum");
  if (hasOwn(schema, "enum") && (!Array.isArray(schema.enum) || !schema.enum.length
    || new Set(schema.enum).size !== schema.enum.length || schema.enum.some((value) => !kinds.some((kind) => matchesType(kind, value))
      || (value !== null && typeof value === "object")))) add("invalid scalar enum");
  return issues;
}

/** Schema validity is checked at catalogue load; runtime validation is bounded. */
export function validateValue(schema, value, path = "$") {
  const issues = [];
  function visit(current, input, location) {
    if (issues.length >= 100) return;
    const add = (message) => issues.push(`${location}: ${message}`);
    if (!kindList(current).some((type) => matchesType(type, input))) { add("wrong type"); return; }
    if (current.enum && !current.enum.includes(input)) add("not in enum");
    if (input === null) return;
    if (typeof input === "number") {
      if (input < current.minimum || input > current.maximum) add("outside bounds");
    } else if (typeof input === "string") {
      if (current.maxLength !== undefined && [...input].length > current.maxLength) add("too long");
    } else if (Array.isArray(input)) {
      if (current.maxItems !== undefined && input.length > current.maxItems) { add("too many items"); return; }
      input.forEach((item, index) => visit(current.items, item, `${location}[${index}]`));
    } else if (plainObject(input)) {
      for (const key of current.required) if (!hasOwn(input, key)) add(`missing ${key}`);
      for (const key of Object.keys(input)) {
        if (!hasOwn(current.properties, key)) add(`unexpected ${key}`);
        else visit(current.properties[key], input[key], `${location}.${key}`);
      }
    }
  }
  visit(schema, value, path);
  return issues;
}

/**
 * Every definition states both `mutating` and `longOperation` in its options. There is
 * deliberately no default: a flag that is left out stays `undefined`, the catalogue check
 * rejects it, and the engine host treats such a method as neither a plain read nor safe to
 * detach. (A default of `false` once let a forgotten `mutating: true` pass as a plain read.)
 * The two placeholders only keep the generated catalogue's key order.
 */
export function areaMethods(area, definitions) {
  return definitions.map(([name, description, params, result, options = {}]) => ({
    name: `${area}.${name}`, description, mutating: undefined, longOperation: undefined, params, result, ...options,
  }));
}
