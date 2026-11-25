"""Resolve scope-name text and a bounded subset of CK3 event conditions."""

import re


EXPRESSION = re.compile(r"\[([^\[\]]+)\]")
CONDITION_TOKEN = re.compile(r'#[^\n]*|"(?:\\.|[^"\\])*"|>=|<=|!=|[{}=<>!]|[^\s{}=<>!#"]+|[^\s]')


def scope_value(name, context):
    if name.casefold() in ("root", "root.char"):
        return context.get("root")
    targets = context.get("targets", {})
    key = name.removeprefix("scope:")
    return targets.get(key, targets.get(key.removesuffix(".Char")))


def render_text(text, context, labels):
    """Keep unsupported expressions intact so partial rendering stays explicit."""
    def replace(match):
        expression = match[1]
        if "." not in expression:
            return match[0]
        name, function = expression.rsplit(".", 1)
        if function not in ("GetName", "GetNameNoTooltip", "GetFirstName", "GetFirstNameNoTooltip"):
            return match[0]
        value = scope_value(name, context)
        if not isinstance(value, dict) or value.get("resolved") is False or ("id" in value and value["id"] is None):
            return match[0]
        if value.get("type") == "char":
            resolved = value.get("name")
        elif value.get("type") == "lt" and function in ("GetName", "GetNameNoTooltip"):
            title = value.get("title") or {}
            resolved = title.get("name") or labels.name(title.get("key"))
        else:
            resolved = None
        return resolved if isinstance(resolved, str) and resolved else match[0]

    rendered = EXPRESSION.sub(replace, text)
    return rendered, list(dict.fromkeys(EXPRESSION.findall(rendered)))


def _all(values):
    if False in values:
        return False
    return None if None in values else True


def _any(values):
    if True in values:
        return True
    return None if None in values else False


def evaluate_condition(source, context):
    """Return true, false or unknown while retaining every unsupported predicate."""
    tokens = [match.group() for match in CONDITION_TOKEN.finditer(source) if not match.group().startswith("#")]
    index = 0

    def parse(depth=0):
        nonlocal index
        if depth > 64:
            raise ValueError("Event condition nesting exceeds the evaluation bound")
        rows = []
        while index < len(tokens) and tokens[index] != "}":
            if index + 2 >= len(tokens) or tokens[index + 1] not in ("=", "!=", ">", "<", ">=", "<="):
                raise ValueError("Unsupported event condition syntax")
            key, operator = tokens[index:index + 2]
            index += 2
            value = tokens[index]
            index += 1
            if value == "{":
                value = parse(depth + 1)
                if index >= len(tokens) or tokens[index] != "}":
                    raise ValueError("Unmatched event condition brace")
                index += 1
            rows.append((key, operator, value))
        return rows

    def evaluate(row):
        key, operator, value = row
        if operator != "=":
            return None
        if isinstance(value, list):
            children = [evaluate(child) for child in value]
            if key == "AND":
                return _all(children)
            if key == "OR":
                return _any(children)
            if key in ("NOR", "NAND"):
                result = _any(children) if key == "NOR" else _all(children)
                return None if result is None else not result
            if key == "NOT" and len(children) == 1:
                return None if children[0] is None else not children[0]
            return None
        if key == "always" and value in ("yes", "no"):
            return value == "yes"
        if key == "exists" and (value.casefold() == "root" or value.startswith("scope:")):
            scoped = scope_value(value, context)
            if scoped is None:
                return False
            if not isinstance(scoped, dict) or scoped.get("resolved") is False:
                return None
            if "id" in scoped:
                return scoped["id"] is not None
            return None
        if key.startswith("scope:") and value in ("yes", "no"):
            scoped = scope_value(key, context)
            if isinstance(scoped, dict) and scoped.get("type") == "boolean" and type(scoped.get("value")) is bool:
                return scoped["value"] == (value == "yes")
        return None

    try:
        rows = parse()
        if index != len(tokens):
            return None
        return _all([evaluate(row) for row in rows])
    except ValueError:
        return None
