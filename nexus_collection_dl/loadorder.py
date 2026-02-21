"""Load order generation from collection metadata and mod requirements."""

from collections import defaultdict
from pathlib import Path
from typing import Any

from .manifest import CollectionManifest


class LoadOrderError(Exception):
    """Raised when load order generation fails."""
    pass


class LoadOrderGenerator:
    """Generates mod and plugin load order from collection metadata."""

    # Bethesda game domains that support ESP/ESM plugins
    BETHESDA_GAMES = {
        "starfield",
        "skyrimspecialedition",
        "skyrim",
        "fallout4",
        "falloutnv",
        "fallout3",
        "oblivion",
        "morrowind",
        "fallout4vr",
        "skyrimvr",
        "fallout76",
        "enderal",
        "enderalspecialedition",
    }

    def __init__(
        self,
        manifest: CollectionManifest,
        mods: list[dict[str, Any]],
        mod_requirements: dict[int, list[int]],
        game_domain: str,
    ):
        """
        Args:
            manifest: Parsed collection manifest
            mods: List of mod info dicts (from GraphQL, must have mod_id and mod_name)
            mod_requirements: Map of mod_id -> list of required mod_ids (from GraphQL modRequirements)
            game_domain: Game domain name (e.g., 'starfield', 'baldursgate3')
        """
        self.manifest = manifest
        self.mods = {m["mod_id"]: m for m in mods}
        self.mod_requirements = mod_requirements
        self.game_domain = game_domain.lower()
        self.is_bethesda = self.game_domain in self.BETHESDA_GAMES

    def generate(self, output_dir: Path) -> list[Path]:
        """
        Generate load order files in the output directory.

        Returns list of written file paths.
        """
        written = []

        # Generate mod load order (all games)
        mod_order = self._sort_mods()
        load_order_path = output_dir / "load-order.txt"
        self._write_load_order(mod_order, load_order_path)
        written.append(load_order_path)

        # Generate plugins.txt for Bethesda games (from collection metadata)
        if self.is_bethesda and self.manifest.plugins:
            plugins_path = output_dir / "plugins.txt"
            self._write_plugins(plugins_path)
            written.append(plugins_path)

        return written

    def _sort_mods(self) -> list[int]:
        """
        Topological sort of mods by phase + dependencies.

        Uses:
        1. Phase grouping (phase 0 mods before phase 1, etc.)
        2. modRules from collection (before/after edges)
        3. modRequirements from GraphQL (author dependencies)
        """
        # Collect all mod IDs in this collection
        all_mod_ids = set(self.mods.keys())

        # Build adjacency list: edges[a] = [b] means a must come before b
        edges: dict[int, set[int]] = defaultdict(set)
        in_degree: dict[int, int] = {mid: 0 for mid in all_mod_ids}

        # 1. Phase ordering: create edges from lower phase to higher phase mods
        phase_groups: dict[int, list[int]] = defaultdict(list)
        for mod_id in all_mod_ids:
            phase = self.manifest.mod_phases.get(mod_id, 0)
            phase_groups[phase].append(mod_id)

        sorted_phases = sorted(phase_groups.keys())
        for i in range(len(sorted_phases) - 1):
            current_phase = sorted_phases[i]
            next_phase = sorted_phases[i + 1]
            # Any mod in current phase must come before any mod in next phase
            # We only add edge from last mod in current to first in next (lightweight)
            # Actually, for correctness with topo sort, we need proper phase barriers.
            # Use a simpler approach: assign phase weight and sort by it, then topo within.
            pass  # Handled below via weighted sort

        # 2. modRules from collection manifest
        for rule in self.manifest.mod_rules:
            rule_type = rule.get("type", "")
            source_id = rule.get("source", {}).get("modId")
            target_id = rule.get("target", {}).get("modId")

            if source_id is None or target_id is None:
                continue
            source_id = int(source_id)
            target_id = int(target_id)

            if source_id not in all_mod_ids or target_id not in all_mod_ids:
                continue

            if rule_type == "before":
                # source should load before target
                edges[source_id].add(target_id)
                in_degree[target_id] = in_degree.get(target_id, 0) + 1
            elif rule_type == "after":
                # source should load after target
                edges[target_id].add(source_id)
                in_degree[source_id] = in_degree.get(source_id, 0) + 1
            elif rule_type == "requires":
                # source requires target -> target before source
                edges[target_id].add(source_id)
                in_degree[source_id] = in_degree.get(source_id, 0) + 1

        # 3. modRequirements from GraphQL
        for mod_id, req_ids in self.mod_requirements.items():
            if mod_id not in all_mod_ids:
                continue
            for req_id in req_ids:
                if req_id not in all_mod_ids:
                    continue
                # Required mod should come before dependent
                if req_id not in edges or mod_id not in edges[req_id]:
                    edges[req_id].add(mod_id)
                    in_degree[mod_id] = in_degree.get(mod_id, 0) + 1

        # Kahn's algorithm with phase-aware priority
        # Mods with lower phase get priority, then by name for stability
        import heapq
        queue: list[tuple[int, str, int]] = []  # (phase, name, mod_id)
        for mod_id in all_mod_ids:
            if in_degree.get(mod_id, 0) == 0:
                phase = self.manifest.mod_phases.get(mod_id, 0)
                name = self.mods.get(mod_id, {}).get("mod_name", "")
                heapq.heappush(queue, (phase, name.lower(), mod_id))

        result: list[int] = []
        visited = 0

        while queue:
            _, _, mod_id = heapq.heappop(queue)
            result.append(mod_id)
            visited += 1

            for neighbor in sorted(edges.get(mod_id, set())):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    phase = self.manifest.mod_phases.get(neighbor, 0)
                    name = self.mods.get(neighbor, {}).get("mod_name", "")
                    heapq.heappush(queue, (phase, name.lower(), neighbor))

        if visited < len(all_mod_ids):
            # Cycle detected — add remaining mods in phase/name order
            remaining = all_mod_ids - set(result)
            for mod_id in sorted(
                remaining,
                key=lambda m: (
                    self.manifest.mod_phases.get(m, 0),
                    self.mods.get(m, {}).get("mod_name", "").lower(),
                ),
            ):
                result.append(mod_id)

        return result

    def _write_load_order(self, mod_order: list[int], path: Path) -> None:
        """Write mod load order file."""
        lines = [
            "# Mod Load Order",
            f"# Generated by nexus-collection-dl",
            f"# Game: {self.game_domain}",
            f"# Total mods: {len(mod_order)}",
            "#",
            "# Mods are listed in load order (first = loaded first).",
            "# Phase groups are separated by blank lines.",
            "",
        ]

        current_phase = None
        for i, mod_id in enumerate(mod_order, 1):
            phase = self.manifest.mod_phases.get(mod_id, 0)
            mod = self.mods.get(mod_id, {})
            name = mod.get("mod_name", f"Unknown (ID: {mod_id})")

            if phase != current_phase:
                if current_phase is not None:
                    lines.append("")
                lines.append(f"# --- Phase {phase} ---")
                current_phase = phase

            lines.append(f"{i:4d}. [{mod_id}] {name}")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")

    def _write_plugins(self, path: Path) -> None:
        """Write plugins.txt for Bethesda games from collection metadata."""
        lines = [
            "# Plugin Load Order (from collection metadata)",
            f"# Generated by nexus-collection-dl",
            f"# Game: {self.game_domain}",
            "#",
            "# This is the collection author's intended plugin order.",
            "# For LOOT-sorted order, install libloot and re-run.",
            "",
        ]

        for plugin in self.manifest.plugins:
            filename = plugin.get("filename", "")
            enabled = plugin.get("enabled", True)
            if not filename:
                continue
            prefix = "*" if enabled else ""
            lines.append(f"{prefix}{filename}")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
