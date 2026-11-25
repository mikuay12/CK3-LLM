"""Map metadata, requested routes and factual movement information."""

import csv
import heapq
import math
from pathlib import Path
import re


class InstalledMap:
    """Static map labels and icon positions from the running game's installation."""

    def __init__(self, executable):
        directory = Path(executable).parents[1] / "game"
        configuration = (directory / "map_data" / "default.map").read_text(encoding="utf-8-sig")
        configuration = re.sub(r"#.*", "", configuration)
        self.types = {}
        for name, mode, contents in re.findall(
                r"(sea_zones|river_provinces|lakes|impassable_mountains|impassable_seas)"
                r"\s*=\s*(RANGE|LIST)\s*\{([^}]*)\}", configuration, re.IGNORECASE):
            values = [int(value) for value in contents.split()]
            if mode.casefold() == "range":
                if len(values) != 2 or values[0] > values[1]:
                    raise ValueError("Invalid map province range")
                values = range(values[0], values[1] + 1)
            self.types.update((value, name.casefold()) for value in values)
        definitions = re.search(r'\bdefinitions\s*=\s*"([^"]+)"', configuration)
        if definitions is None:
            raise ValueError("The map province definitions are unavailable")
        self.names = {}
        with (directory / "map_data" / definitions[1]).open(encoding="utf-8-sig") as stream:
            for row in csv.reader(stream, delimiter=";"):
                if len(row) >= 5 and row[0].isdigit() and int(row[0]) > 0:
                    self.names[int(row[0])] = row[4]
        # This locator covers the expanded map. The older unit_stack locator
        # contains only a subset of its provinces.
        locator = directory / "gfx" / "map" / "map_object_data" / "player_stack_locators.txt"
        self.positions = {}
        for identifier, coordinates in re.findall(
                r"\bid\s*=\s*(\d+)\s+position\s*=\s*\{([^}]+)\}",
                locator.read_text(encoding="utf-8-sig")):
            x, height, z = map(float, coordinates.split())
            if not all(math.isfinite(value) for value in (x, height, z)):
                raise ValueError("Invalid map position")
            self.positions[int(identifier)] = {"x": x, "z": z}
        self.terrains = {}
        self.default_land_terrain = None
        self.default_sea_terrain = None
        terrain_file = directory / "common" / "province_terrain" / "00_province_terrain.txt"
        if terrain_file.is_file():
            for raw_line in terrain_file.read_text(encoding="utf-8-sig").splitlines():
                line = raw_line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                key, value = (part.strip() for part in line.split("=", 1))
                if key == "default_land":
                    self.default_land_terrain = value
                elif key == "default_sea":
                    self.default_sea_terrain = value
                elif key.isdigit():
                    self.terrains[int(key)] = value

    def terrain(self, identifier):
        if identifier in self.terrains:
            return self.terrains[identifier]
        kind = self.types.get(identifier, "land")
        if kind == "land":
            return self.default_land_terrain
        if kind in ("sea_zones", "impassable_seas"):
            return self.default_sea_terrain
        return None

    def province(self, identifier):
        if type(identifier) is not int or identifier not in self.names:
            raise ValueError("Province map definition was not found")
        kind = self.types.get(identifier, "land")
        return {"province_id": identifier, "map_name": self.names[identifier], "map_type": kind,
                "terrain": self.terrain(identifier),
                "land_passable": kind == "land", "position": self.positions.get(identifier),
                "position_source": "installed_player_stack_locator", "distance_unit": "map_units"}

    def distance(self, origin, target):
        first = self.province(origin)["position"]
        second = self.province(target)["position"]
        if first is None or second is None:
            return None
        return math.hypot(first["x"] - second["x"], first["z"] - second["z"])


def compass_direction(origin_pos, target_pos):
    """Compute compact 8-way compass direction between two map positions ({x, z})."""
    if not origin_pos or not target_pos:
        return None
    dx = target_pos["x"] - origin_pos["x"]
    dz = target_pos["z"] - origin_pos["z"]
    angle = math.degrees(math.atan2(dz, dx))
    cardinal = ("E", "NE", "N", "NW", "W", "SW", "S", "SE")[int((angle + 360 + 22.5) // 45) % 8]
    return {"dx": round(dx, 2), "dz": round(dz, 2), "cardinal": cardinal}


def shortest_routes(node, origin, targets, *, include_straits=True, max_nodes=20000):
    """Dijkstra over directed native land edges, weighted by native movement cost."""
    targets = set(targets)
    distances = {origin: 0.0}
    previous = {}
    pending = [(0.0, origin)]
    found = {}
    visited = 0
    if not node(origin)["land_passable"]:
        return found
    while pending and targets:
        cost, current = heapq.heappop(pending)
        if cost != distances[current]:
            continue
        visited += 1
        if visited > max_nodes:
            raise ValueError("Route search exceeded the province budget")
        if current in targets:
            route = []
            cursor = current
            while cursor != origin:
                parent, edge = previous[cursor]
                route.append({"from_province_id": parent, **edge})
                cursor = parent
            route.reverse()
            found[current] = {"province_ids": [origin] + [edge["province_id"] for edge in route],
                              "movement_cost": cost, "segments": route}
            targets.remove(current)
        for edge in node(current)["edges"]:
            neighbor = edge["province_id"]
            if edge["crossing"] == "impassable":
                continue
            if edge["crossing"] == "strait" and not include_straits:
                continue
            if not node(neighbor)["land_passable"]:
                continue
            weight = edge["movement_cost"]
            if not math.isfinite(weight) or weight <= 0:
                raise ValueError("Invalid native movement cost")
            candidate = cost + weight
            if candidate < distances.get(neighbor, math.inf):
                distances[neighbor] = candidate
                previous[neighbor] = (current, edge)
                heapq.heappush(pending, (candidate, neighbor))
    return found


def movement_details(unit, edges, *, lock_threshold=0.5):
    """Expose stored movement data and unresolved native result fields."""
    path = unit["path"]
    edge = next((item for item in edges if path and item["province_id"] == path[0]), None)
    cost = edge["movement_cost"] if edge else None
    return {"next_province_id": path[0] if path else None,
            "destination_province_id": path[-1] if path else None,
            "leg_movement_cost": cost, "leg_progress_fraction": None,
            "movement_locked": None, "movement_lock_threshold": lock_threshold,
            "estimated_next_arrival_days": None, "timing_basis": "native_memory_fields",
            "derived_fields_status": "native_result_not_located"}


