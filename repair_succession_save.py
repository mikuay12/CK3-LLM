"""Create a repaired copy of a plain-text CK3 save with an orphaned succession.

Matches the two effects of CCloseSuccessionCommand on a disconnected played_character:
remove has_open_succession and its optional succession_data. The original is never edited.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re


TOKENS = re.compile(rb'\s+|\#[^\r\n]*|"(?:\\.|[^"\\])*"|[{}=]|[^\s{}="#]+')
PLAYED = re.compile(rb'(?m)^played_character\s*=\s*\{')


def _properties(data, start, end=None):
    """Yield immediate properties with exact byte spans, respecting quoted braces."""
    tokens = [(m.start(), m.end(), m.group()) for m in TOKENS.finditer(data, start, end or len(data))
              if not m.group().isspace() and not m.group().startswith(b"#")]
    if not tokens or tokens[0][2] != b"{":
        raise ValueError("Expected a save object")
    result = {}
    i = 1
    while i < len(tokens) and tokens[i][2] != b"}":
        key_start, _, key = tokens[i]
        if key in result or i + 2 >= len(tokens) or tokens[i + 1][2] != b"=":
            raise ValueError("Malformed or duplicate save property")
        value_start, value_end, value = tokens[i + 2]
        i += 3
        if value == b"{":
            depth = 1
            while i < len(tokens) and depth:
                token = tokens[i]
                depth += (token[2] == b"{") - (token[2] == b"}")
                value_end = token[1]
                i += 1
            if depth:
                raise ValueError("Unterminated save object")
        result[key] = (key_start, value_start, value_end)
    if i >= len(tokens):
        raise ValueError("Unterminated save object")
    return result, tokens[i][1]


def repair_bytes(data, *, player_name, character):
    if not data.startswith(b"SAV") or b"\n" not in data[:128]:
        raise ValueError("This repair supports plain-text SAV saves only")
    matches = []
    # A played_character is a root property written at column zero. Bound each
    # object before tokenizing, so a 200 MB save does not become millions of tokens.
    braces = re.compile(rb'"(?:\\.|[^"\\])*"|\#[^\r\n]*|[{}]')
    previous_end = 0
    for match in PLAYED.finditer(data):
        if match.start() < previous_end:
            continue
        start = match.end() - 1
        depth = 0
        end = None
        for token in braces.finditer(data, start):
            value = token.group()
            depth += (value == b"{") - (value == b"}")
            if depth == 0:
                end = token.end()
                break
        if end is None:
            raise ValueError("Unterminated played_character")
        previous_end = end
        fields, _ = _properties(data, start, end)

        def value(key, fields=fields):
            span = fields.get(key)
            return data[span[1]:span[2]] if span else None

        name = value(b"name")
        if name is None:
            continue
        name = json.loads(name.decode("utf-8")) if name.startswith(b'"') else name.decode("utf-8")
        if name == player_name and value(b"character") == str(character).encode("ascii"):
            matches.append((fields, value))
    if len(matches) != 1:
        raise ValueError("Expected exactly one matching played_character")
    fields, value = matches[0]
    # Do not act on an active human's unfinished choice of destiny.
    if value(b"player") != b"-1":
        raise ValueError("Target player is still connected; use native close_succession instead")
    if value(b"has_open_succession") != b"yes":
        raise ValueError("Target has no orphaned open succession")
    previous = None
    if b"succession_data" in fields:
        a, b, c = fields[b"succession_data"]
        details, _ = _properties(data, b, c)
        heir = details.get(b"player_heir")
        if heir and data[heir[1]:heir[2]] != str(character).encode("ascii"):
            raise ValueError("Current character does not match the pending player heir")
        old = details.get(b"previous_played")
        previous = int(data[old[1]:old[2]]) if old else None
    spans = [fields[key] for key in (b"has_open_succession", b"succession_data") if key in fields]
    changes = sorted((a, c) for a, _, c in spans)
    pieces = []
    cursor = 0
    for a, b in changes:
        pieces.append(data[cursor:a])
        cursor = b
    pieces.append(data[cursor:])
    return b"".join(pieces), {"player_name": player_name, "character": character,
                               "previous_character": previous,
                               "removed_fields": [key.decode() for key in
                                                  (b"has_open_succession", b"succession_data") if key in fields],
                               "removed_byte_ranges": changes}


def repair_file(source, output, *, player_name, character):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or output.exists():
        raise ValueError("Output must be a new file; the source is never overwritten")
    manifest = output.with_suffix(output.suffix + ".repair.json")
    if manifest.exists():
        raise ValueError("Repair manifest already exists")
    before = source.stat()
    data = source.read_bytes()
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(data) != after.st_size:
        raise ValueError("Source save changed while reading; retry after saving has finished")
    repaired, details = repair_bytes(data, player_name=player_name, character=character)
    report = {"source": str(source), "output": str(output),
              "source_sha256": hashlib.sha256(data).hexdigest(),
              "output_sha256": hashlib.sha256(repaired).hexdigest(), **details}
    with output.open("xb") as stream:
        stream.write(repaired)
    with manifest.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--player-name", required=True)
    parser.add_argument("--character", required=True, type=int)
    args = parser.parse_args()
    print(json.dumps(repair_file(args.source, args.output, player_name=args.player_name,
                                 character=args.character), ensure_ascii=True))


if __name__ == "__main__":
    main()
