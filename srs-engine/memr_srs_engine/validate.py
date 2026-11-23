"""The same closed JSON-schema subset the Memr host uses for SRS contracts."""
import math

MAX_SAFE_INTEGER = 9007199254740991
KEYWORDS = {"type", "properties", "required", "items", "enum", "minimum", "maximum",
            "maxLength", "maxItems", "additionalProperties"}
TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def matches_type(kind, value):
    if kind == "null":
        return value is None
    if kind == "object":
        return type(value) is dict
    if kind == "array":
        return type(value) is list
    if kind == "string":
        return type(value) is str
    if kind == "boolean":
        return type(value) is bool
    if kind == "integer":
        return type(value) in (int, float) and abs(value) <= MAX_SAFE_INTEGER and value == int(value)
    if kind == "number":
        return type(value) in (int, float) and abs(value) <= float.fromhex("0x1.fffffffffffffp+1023")
    return False


def kinds(schema):
    value = schema.get("type")
    return value if type(value) is list else [value]


def check_schema(schema, ancestors=()):
    """Validate once at startup; unsupported dialects must fail closed."""
    if type(schema) is not dict or id(schema) in ancestors or len(ancestors) > 64:
        raise ValueError("Invalid or cyclic contract schema")
    if set(schema) - KEYWORDS:
        raise ValueError("Unsupported contract schema keyword")
    types = kinds(schema)
    if not types or any(type(t) is not str or t not in TYPES for t in types) or len(set(types)) != len(types):
        raise ValueError("Invalid contract type")
    if len([t for t in types if t != "null"]) > 1:
        raise ValueError("Only nullable unions are supported")
    parents = (*ancestors, id(schema))
    if "object" in types:
        props, required = schema.get("properties"), schema.get("required")
        if schema.get("additionalProperties") is not False or type(props) is not dict or type(required) is not list:
            raise ValueError("Objects must be closed with explicit properties and required")
        if any(type(key) is not str or key not in props for key in required) or len(set(required)) != len(required):
            raise ValueError("Invalid required properties")
        for key, child in props.items():
            if key in {"__proto__", "constructor", "prototype"}:
                raise ValueError("Unsafe contract property")
            check_schema(child, parents)
    elif set(schema) & {"properties", "required", "additionalProperties"}:
        raise ValueError("Object keywords on a non-object")
    if "array" in types:
        check_schema(schema.get("items"), parents)
    elif "items" in schema:
        raise ValueError("Items on a non-array")
    for key, kind in (("maxLength", "string"), ("maxItems", "array")):
        if key in schema and (kind not in types or not matches_type("integer", schema[key]) or schema[key] < 0):
            raise ValueError("Invalid contract length bound")
    for key in ("minimum", "maximum"):
        if key in schema and (not set(types) & {"integer", "number"} or not matches_type("number", schema[key])):
            raise ValueError("Invalid contract numeric bound")
        if "integer" in types and key in schema and not matches_type("integer", schema[key]):
            raise ValueError("Unsafe integer bound")
    if schema.get("minimum", -math.inf) > schema.get("maximum", math.inf):
        raise ValueError("Reversed contract bounds")
    if "enum" in schema:
        values = schema["enum"]
        if type(values) is not list or not values:
            raise ValueError("Invalid contract enum")
        for index, value in enumerate(values):
            if type(value) in (dict, list) or not any(matches_type(t, value) for t in types):
                raise ValueError("Invalid contract enum value")
            if any(type(value) is type(other) and value == other for other in values[:index]):
                raise ValueError("Duplicate contract enum value")


def validate_value(schema, value, path="$"):
    """Return bounded diagnostics with field locations, never input values."""
    issues = []

    def visit(current, input_value, location, depth):
        if len(issues) >= 100:
            return
        if depth > 64:
            issues.append(f"{location}: too deeply nested")
            return
        if not any(matches_type(t, input_value) for t in kinds(current)):
            issues.append(f"{location}: wrong type")
            return
        if "enum" in current and input_value not in current["enum"]:
            issues.append(f"{location}: not in enum")
        if input_value is None:
            return
        if type(input_value) in (int, float):
            if input_value < current.get("minimum", -math.inf) or input_value > current.get("maximum", math.inf):
                issues.append(f"{location}: outside bounds")
        elif type(input_value) is str:
            if len(input_value) > current.get("maxLength", math.inf):
                issues.append(f"{location}: too long")
        elif type(input_value) is list:
            if len(input_value) > current.get("maxItems", math.inf):
                issues.append(f"{location}: too many items")
                return
            for index, item in enumerate(input_value):
                visit(current["items"], item, f"{location}[{index}]", depth + 1)
                if len(issues) >= 100:
                    break
        elif type(input_value) is dict:
            for key in current["required"]:
                if key not in input_value:
                    issues.append(f"{location}: missing required property")
            for key, item in input_value.items():
                if len(issues) >= 100:
                    break
                if key not in current["properties"]:
                    issues.append(f"{location}: unexpected property")
                else:
                    visit(current["properties"][key], item, f"{location}.{key}", depth + 1)

    visit(schema, value, path, 0)
    return issues[:100]
