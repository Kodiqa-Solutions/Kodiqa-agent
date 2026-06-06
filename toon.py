"""TOON (Token-Oriented Object Notation) encoder — a compact, LLM-friendly encoding.

For an array of uniform flat objects it writes the field names ONCE (like a CSV
header) instead of repeating them on every row, and inlines scalar arrays — cutting
~30-60% of the tokens vs JSON on large uniform data. Nested/non-uniform values fall
back to inline JSON so the output is always unambiguous and round-trippable enough
for a model to read. Used for tool results when `/toon` is enabled.

Example:
    {"users":[{"id":1,"name":"Alice"},{"id":2,"name":"Bob"}]}
becomes:
    users[2]{id,name}:
      1,Alice
      2,Bob
"""

import json

_INDENT = "  "


def _is_scalar(v):
    return v is None or isinstance(v, (str, int, float, bool))


def _looks_number(s):
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def _fmt_scalar(v):
    """Render a scalar; bare when safe, JSON-quoted when it could be misread."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if (s == "" or s != s.strip()
            or any(c in s for c in (",", ":", '"', "\n", "{", "}", "[", "]"))
            or s in ("true", "false", "null") or _looks_number(s)):
        return json.dumps(s)
    return s


def _table_keys(arr):
    """If arr is a non-empty list of dicts that share the same key set and have only
    scalar values, return the key order (from the first item); else None."""
    if not arr or not all(isinstance(x, dict) for x in arr):
        return None
    keys = list(arr[0].keys())
    keyset = set(keys)
    for x in arr:
        if set(x.keys()) != keyset or not all(_is_scalar(v) for v in x.values()):
            return None
    return keys


def _encode(value, key, indent, lines):
    pad = _INDENT * indent
    head = f"{key}" if key else ""
    if _is_scalar(value):
        lines.append(f"{pad}{key}: {_fmt_scalar(value)}" if key else f"{pad}{_fmt_scalar(value)}")
        return
    if isinstance(value, dict):
        if not value:
            lines.append(f"{pad}{key}: {{}}" if key else f"{pad}{{}}")
            return
        if key:
            lines.append(f"{pad}{key}:")
        child = indent + (1 if key else 0)
        for k, v in value.items():
            _encode(v, k, child, lines)
        return
    if isinstance(value, list):
        keys = _table_keys(value)
        if keys is not None:
            lines.append(f"{pad}{head}[{len(value)}]{{{','.join(keys)}}}:")
            row_pad = _INDENT * (indent + 1)
            for row in value:
                lines.append(row_pad + ",".join(_fmt_scalar(row.get(k)) for k in keys))
        elif all(_is_scalar(x) for x in value):
            lines.append(f"{pad}{head}[{len(value)}]: " + ",".join(_fmt_scalar(x) for x in value))
        else:
            # mixed / nested array — fall back to compact JSON (always unambiguous)
            lines.append(f"{pad}{head}: {json.dumps(value, separators=(',', ':'))}"
                         if key else f"{pad}{json.dumps(value, separators=(',', ':'))}")
        return
    # anything else (shouldn't happen for JSON) → JSON
    lines.append(f"{pad}{key}: {json.dumps(value)}" if key else f"{pad}{json.dumps(value)}")


def to_toon(obj):
    """Encode a JSON-compatible object (or JSON string) as TOON. Non-JSON strings
    are returned unchanged."""
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except (ValueError, TypeError):
            return obj
    lines = []
    _encode(obj, "", 0, lines)
    return "\n".join(lines)


def maybe_toon(text):
    """If `text` is JSON and TOON would be shorter, return the TOON form (with a
    one-line hint); otherwise return `text` unchanged. Safe to call on anything —
    it never makes output longer."""
    if not isinstance(text, str):
        return text
    t = text.strip()
    if not (t.startswith("{") or t.startswith("[")):
        return text
    try:
        obj = json.loads(t)
    except (ValueError, TypeError):
        return text
    candidate = "# TOON (compact tabular encoding)\n" + to_toon(obj)
    return candidate if len(candidate) < len(text) else text
