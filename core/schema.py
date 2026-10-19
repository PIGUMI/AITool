"""最小のJSON Schema検証（type / required / enum / properties / items / additionalProperties）。"""
from __future__ import annotations

from typing import Any

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """違反の一覧を返す（空なら適合）。"""
    errors: list[str] = []
    t = schema.get("type")
    if t:
        py = _TYPES[t]
        ok = isinstance(value, py) and not (t in ("integer", "number") and isinstance(value, bool))
        if not ok:
            return [f"{path}: {t} ではありません"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {schema['enum']} のいずれかではありません")
    if t == "object":
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: 必須です")
        for key, v in value.items():
            if key in props:
                errors += validate(v, props[key], f"{path}.{key}")
            elif schema.get("additionalProperties", True) is False:
                errors.append(f"{path}.{key}: 未定義の引数です")
    if t == "array" and "items" in schema:
        for i, v in enumerate(value):
            errors += validate(v, schema["items"], f"{path}[{i}]")
    return errors
