"""Load order generation from collection metadata and mod requirements."""

from collections import defaultdict
from pathlib import Path
from typing import Any

from .manifest import CollectionManifest


class LoadOrderError(Exception):
    """Raised when load order generation fails."""
    pass


class LoadOrderGenerator:
    """Generates mod and plugin load order from collection metadata.

    Now file-aware: a single Nexus mod (mod_id) can contribute multiple files
    to the collection (e.g. core pak + texture pak + compatibility patch).
    The load order tracks both mod_id (for phase/dependency ordering) and
    file_id (for exact file identification and download tracking).
    """

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
            mods: List of mod file info dicts from GraphQL.
                  Each entry has: mod_id, file_id, mod_name, filename, optional, etc.
                  There may be MULTIPLE entries with the same mod_id (multi-file mods).
            mod_requirements: Map of mod_id -> list of required mod_ids
            game_domain: Game domain name (e.g., 'starfield', 'baldursgate3')
        """
        self.manifest = manifest
        self.game_domain = game_domain.lower()
        self.is_bethesda = self.game_domain in self.BETHESDA_GAMES

        # Index all files by file_id (primary key)
        self.files_by_file_id: dict[int, dict[str, Any]] = {
            m["file_id"]: m for m in mods
        }

        # Index files by mod_id (one mod may have multiple files)
        self.files_by_mod_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for m in mods:
            self.files_by_mod_id[m["mod_id"]].append(m)

        # For dependency resolution we operate at mod_id level
        # (dependencies are between mods, not individual files)
        self.mod_requirements = mod_requirements

        # Unique mod_ids in this collection (for topo sort)
        self.all_mod_ids: set[int] = set(self.files_by_mod_id.keys())

        # Convenience: mod_name by mod_id (use first file's name)
        self.mod_name_by_id: dict[int, str] = {
            mid: files[0].get("mod_name", f"Unknown [{mid}]")
            for mid, files in self.files_by_mod_id.items()
        }

    def generate(self, output_dir: Path) -> list[Path]:
        """
        Generate load order files in the output directory.

        Returns list of written file paths.
        """
        written = []

        # Sort mods by phase + dependencies (returns ordered list of mod_ids)
        ordered_mod_ids = self._sort_mods()

        # Write load order (one line per FILE, grouped by mod)
        load_order_path = output_dir / "load-order.txt"
        self._write_load_order(ordered_mod_ids, load_order_path)
        written.append(load_order_path)

        # Generate plugins.txt for Bethesda games
        if self.is_bethesda and self.manifest.plugins:
            plugins_path = output_dir / "plugins.txt"
            self._write_plugins(plugins_path)
            written.append(plugins_path)

        return written

    def _sort_mods(self) -> list[int]:
        """
        Topological sort of mods by phase + dependencies.

        Operates at mod_id level (dependencies are between mods, not files).
        Returns ordered list of mod_ids.

        Uses:
        1. Phase grouping from manifest (phase 0 before phase 1, etc.)
        2. modRules from collection manifest (before/after/requires edges)
        3. modRequirements from GraphQL (author-declared dependencies)
        """
        all_mod_ids = self.all_mod_ids

        # Build adjacency list: edges[a] = {b} means mod a must load before mod b
        edges: dict[int, set[int]] = defaultdict(set)
        in_degree: dict[int, int] = {mid: 0 for mid in all_mod_ids}

        def _add_edge(before_id: int, after_id: int) -> None:
            """Add a before→after ordering edge, avoiding duplicates."""
            if before_id == after_id:
                return
            if before_id not in all_mod_ids or after_id not in all_mod_ids:
                return
            if after_id not in edges[before_id]:
                edges[before_id].add(after_id)
                in_degree[after_id] = in_degree.get(after_id, 0) + 1

        # 1. modRules from collection manifest
        # Rules use logicalFileName to identify files, which maps to mod_ids
        lf_to_id = self.manifest.logical_name_to_mod_id
        for rule in self.manifest.mod_rules:
            rule_type = rule.get("type", "")
            source_lf = rule.get("source", {}).get("logicalFileName", "")
            ref_lf = rule.get("reference", {}).get("logicalFileName", "")

            source_mod_id = lf_to_id.get(source_lf)
            ref_mod_id = lf_to_id.get(ref_lf)

            if source_mod_id is None or ref_mod_id is None:
                continue

            if rule_type == "before":
                # source loads before reference
                _add_edge(source_mod_id, ref_mod_id)
            elif rule_type == "after":
                # source loads after reference
                _add_edge(ref_mod_id, source_mod_id)
            elif rule_type == "requires":
                # source requires reference → reference loads first
                _add_edge(ref_mod_id, source_mod_id)

        # 2. modRequirements from GraphQL (author-declared dependencies)
        for mod_id, req_ids in self.mod_requirements.items():
            for req_id in req_ids:
                # Required mod loads before dependent mod
                _add_edge(req_id, mod_id)

        # 3. Kahn's algorithm with phase-aware priority queue
        # Lower phase = higher priority. Within same phase, sort by name for stability.
        import heapq
        queue: list[tuple[int, str, int]] = []  # (phase, name_lower, mod_id)

        for mod_id in all_mod_ids:
            if in_degree.get(mod_id, 0) == 0:
                phase = self.manifest.mod_phases.get(mod_id, 0)
                name = self.mod_name_by_id.get(mod_id, "")
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
                    name = self.mod_name_by_id.get(neighbor, "")
                    heapq.heappush(queue, (phase, name.lower(), neighbor))

        if visited < len(all_mod_ids):
            # Cycle detected — append remaining mods in phase/name order
            remaining = all_mod_ids - set(result)
            for mod_id in sorted(
                remaining,
                key=lambda m: (
                    self.manifest.mod_phases.get(m, 0),
                    self.mod_name_by_id.get(m, "").lower(),
                ),
            ):
                result.append(mod_id)

        return result

    def _write_load_order(self, ordered_mod_ids: list[int], path: Path) -> None:
        """
        Write mod load order file.

        For multi-file mods, all files from that mod appear together in order,
        since they share the same phase and dependencies.

        Format:
            N. [mod_id:file_id] mod_name - file_name
        """
        # Count total files for header
        total_files = sum(
            len(self.files_by_mod_id.get(mid, []))
            for mid in ordered_mod_ids
        )

        lines = [
            "# Mod Load Order",
            "# Generated by nexus-collection-dl",
            f"# Game: {self.game_domain}",
            f"# Total mods: {len(ordered_mod_ids)}",
            f"# Total files: {total_files}",
            "#",
            "# Mods are listed in load order (first = loaded first).",
            "# Multi-file mods list each file on a separate line.",
            "# Format: N. [mod_id:file_id] Mod Name - File Name",
            "# Phase groups are separated by blank lines.",
            "",
        ]

        current_phase = None
        entry_num = 0

        for mod_id in ordered_mod_ids:
            phase = self.manifest.mod_phases.get(mod_id, 0)
            mod_name = self.mod_name_by_id.get(mod_id, f"Unknown [{mod_id}]")
            files = self.files_by_mod_id.get(mod_id, [])

            if phase != current_phase:
                if current_phase is not None:
                    lines.append("")
                lines.append(f"# --- Phase {phase} ---")
                current_phase = phase

            if len(files) == 1:
                # Single file mod — simple format
                entry_num += 1
                f = files[0]
                file_id = f.get("file_id", 0)
                file_name = f.get("filename", "")
                opt = " [optional]" if f.get("optional") else ""
                lines.append(
                    f"{entry_num:4d}. [{mod_id}:{file_id}] {mod_name}{opt}"
                )
            else:
                # Multi-file mod — show mod header then each file
                lines.append(f"       # {mod_name} ({len(files)} files)")
                for f in files:
                    entry_num += 1
                    file_id = f.get("file_id", 0)
                    file_name = f.get("filename", "")
                    opt = " [optional]" if f.get("optional") else ""
                    lines.append(
                        f"{entry_num:4d}. [{mod_id}:{file_id}] {mod_name} - {file_name}{opt}"
                    )

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")

    def _write_plugins(self, path: Path) -> None:
        """Write plugins.txt for Bethesda games from collection metadata."""
        lines = [
            "# Plugin Load Order (from collection metadata)",
            "# Generated by nexus-collection-dl",
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

    def get_ordered_files(self) -> list[dict[str, Any]]:
        """
        Return all mod files in load order as a flat list.

        Useful for generating modsettings.lsx or other game-specific
        configuration files that need the exact ordered file list.

        Returns list of file dicts, each with:
            mod_id, file_id, mod_name, filename, optional, phase
        """
        ordered_mod_ids = self._sort_mods()
        result = []
        for mod_id in ordered_mod_ids:
            files = self.files_by_mod_id.get(mod_id, [])
            phase = self.manifest.mod_phases.get(mod_id, 0)
            for f in files:
                result.append({**f, "phase": phase})
        return result
