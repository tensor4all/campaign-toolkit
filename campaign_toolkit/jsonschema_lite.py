"""A small, dependency-free JSON Schema validator.

The toolkit depends on `pyyaml` alone, so the campaign's JSON Schemas are checked
by this subset validator rather than the `jsonschema` package. It implements the
draft 2020-12 keywords the campaign schemas actually use; an unknown keyword is
ignored rather than guessed at. `validate()` raises `ValidationError` on the first
failure, like `jsonschema.validate`, and carries a JSON path and a message.
"""
from __future__ import annotations

import re


class SchemaError(Exception):
    """The schema itself is not a shape this validator can read."""


class ValidationError(Exception):
    def __init__(self, message: str, json_path: str = "$"):
        super().__init__(message)
        self.message = message
        self.json_path = json_path


def check_schema(schema):
    """Refuse a schema that is not an object. A deeper structural check buys nothing here."""
    if not isinstance(schema, dict):
        raise SchemaError("a JSON Schema must be an object")


_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
}


def _is(value, name):
    check = _TYPES.get(name)
    return True if check is None else check(value)


def _path(path, key):
    return f"{path}.{key}"


def _check(instance, schema, path, errors):
    if not isinstance(schema, dict):
        return
    names = schema.get("type")
    if names is not None:
        names = names if isinstance(names, list) else [names]
        if not any(_is(instance, n) for n in names):
            errors.append((path, f"{instance!r} is not of type {' or '.join(repr(n) for n in names)}"))
            return  # jsonschema stops after a type error
    if "const" in schema and instance != schema["const"]:
        errors.append((path, f"{instance!r} is not the constant {schema['const']!r}"))
        return
    if "enum" in schema and instance not in schema["enum"]:
        errors.append((path, f"{instance!r} is not one of {schema['enum']!r}"))
        return
    if isinstance(instance, str):
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append((path, f"{instance!r} does not match {schema['pattern']!r}"))
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append((path, f"{instance!r} is too short"))
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append((path, f"{instance!r} is too long"))
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append((path, f"{instance!r} is less than the minimum {schema['minimum']!r}"))
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append((path, f"{instance!r} is greater than the maximum {schema['maximum']!r}"))
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append((path, f"{instance!r} is too short"))
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append((path, f"{instance!r} is too long"))
        if "uniqueItems" in schema and schema["uniqueItems"]:
            seen = [repr(v) for v in instance]
            if len(set(seen)) != len(seen):
                errors.append((path, "array items are not unique"))
        if "items" in schema:
            for i, item in enumerate(instance):
                _check(item, schema["items"], f"{path}[{i}]", errors)
    if isinstance(instance, dict):
        if "minProperties" in schema and len(instance) < schema["minProperties"]:
            errors.append((path, f"{instance!r} has too few properties"))
        if "maxProperties" in schema and len(instance) > schema["maxProperties"]:
            errors.append((path, f"{instance!r} has too many properties"))
        for name in schema.get("required", []):
            if name not in instance:
                errors.append((path, f"{name!r} is a required property"))
        props = schema.get("properties", {})
        for key, value in instance.items():
            if key in props:
                _check(value, props[key], _path(path, key), errors)
                continue
            extra = schema.get("additionalProperties", True)
            if extra is False:
                errors.append((path, f"additional properties are not allowed ({key!r} was unexpected)"))
            elif isinstance(extra, dict):
                _check(value, extra, _path(path, key), errors)


def validate(instance, schema):
    """Raise `ValidationError` for the first violation, or return None."""
    check_schema(schema)
    errors = []
    _check(instance, schema, "$", errors)
    if errors:
        path, message = errors[0]
        raise ValidationError(message, json_path=path)
