"""Resolve installed CK3 localization labels for semantic interfaces."""

from pathlib import Path
import re


class GameLabels:
    def __init__(self, executable, *, include_all=False):
        directory = Path(executable).parents[1] / "game" / "localization" / "simp_chinese"
        self.values = {}
        names = ("titles_l_simp_chinese.yml", "traits_l_simp_chinese.yml", "government_l_simp_chinese.yml", "laws_l_simp_chinese.yml")
        paths = sorted(directory.rglob("*.yml")) if include_all else [directory / name for name in names]
        if not include_all:
            paths += sorted(directory.rglob("*buildings_l_simp_chinese.yml"))
        for path in paths:
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                match = re.match(r'\s+([^\s:#]+):\s*(?:\d+\s*)?"(.*)"\s*(?:#.*)?$', line)
                if match:
                    self.values[match[1]] = match[2].replace('\\"', '"').replace('\\n', '\n')

    def name(self, key):
        if not isinstance(key, str):
            return None
        value = self.values.get(key, self.values.get("trait_" + key, self.values.get("building_type_" + key)))
        for _ in range(4):
            if value is None:
                return None
            replaced = re.sub(r"\$([^$]+)\$", lambda match: self.values.get(match[1], match[0]), value)
            if replaced == value:
                break
            value = replaced
        return value

    def apply(self, value):
        if isinstance(value, list):
            return [self.apply(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: self.apply(item) for key, item in value.items()}
        if "key" in result and "name" not in result:
            name = self.name(result["key"])
            if name is not None:
                result["name"] = name
        if "building_key" in result:
            name = self.name(result["building_key"])
            if name is not None:
                result["building_name"] = name
        if isinstance(result.get("government"), str):
            name = self.name(result["government"])
            if name is not None:
                result["government_name"] = name
        return result
