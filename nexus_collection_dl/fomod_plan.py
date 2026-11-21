"""FOMOD install-plan model: parse ``ModuleConfig.xml`` and resolve what installs.

This module is the real installer model. ``fomod.py`` next door is the
top-level-folder heuristic that survives only as the fallback for mods
whose config cannot be read.

Three things the heuristic could not express, each proven against a real
archive:

- Options partition nested paths inside shared top-levels. XPMSE splits
  ``Skeleton Rig\\HDT`` from ``Skeleton Rig\\HDT 4.51``; both reduce to
  the top-level ``skeleton rig``, so a keep-or-skip decision on the
  top-level installs every skeleton style at once.
- ``source`` carries a ``destination`` that may rename or move the
  content. NVAC installs ``<folder source="NVSE" destination="SKSE"/>``.
- Sources are relative to the ``fomod/`` directory's parent, not the
  archive root. SPID wraps everything in one folder and writes
  ``AE/SKSE/Plugins`` relative to that wrapper.

So the plan is a list of (source, destination) pairs at full path depth,
and deploy executes it instead of guessing.

Scope: FOMOD 2.0/5.0 ``ModuleConfig.xml``. FOMOD 1.0 installers
(``fomod/script.cs``, ``fomod/script.lua``) are detected elsewhere and
refused; nothing here reads them.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

__all__ = [
    "AllOf",
    "AnyOf",
    "FileDependency",
    "FileEntry",
    "FlagDependency",
    "FommDependency",
    "GameDependency",
    "GameState",
    "Group",
    "InstallEntry",
    "InstallPlan",
    "InstallStep",
    "ModuleConfig",
    "Option",
    "Pattern",
    "load_module_config",
    "load_module_config_bytes",
    "plan_source_root",
    "resolve_install_plan",
    "selections_from_manifest",
]


# ── Conditions ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FileDependency:
    """``<fileDependency file="X" state="Active"/>``."""

    file: str
    state: str  # 'Active' | 'Inactive' | 'Missing'


@dataclass(frozen=True)
class FlagDependency:
    """``<flagDependency flag="X" value="Y"/>``."""

    flag: str
    value: str


@dataclass(frozen=True)
class GameDependency:
    """``<gameDependency version="V"/>``: game version V or newer."""

    version: str


@dataclass(frozen=True)
class FommDependency:
    """``<fommDependency version="V"/>``: FOMM version V or newer."""

    version: str


@dataclass(frozen=True)
class AllOf:
    """``<dependencies operator="And">``."""

    deps: tuple[object, ...]


@dataclass(frozen=True)
class AnyOf:
    """``<dependencies operator="Or">``."""

    deps: tuple[object, ...]


Condition = (
    FileDependency
    | FlagDependency
    | GameDependency
    | FommDependency
    | AllOf
    | AnyOf
    | None
)


# ── Config model ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FileEntry:
    """One ``<file>`` or ``<folder>`` file-system item."""

    source: str
    destination: str
    is_folder: bool
    always_install: bool = False
    install_if_usable: bool = False
    priority: int = 0


@dataclass(frozen=True)
class Option:
    """One ``<plugin>``: a choice inside a group."""

    name: str
    description: str
    option_type: str  # 'Required' | 'Recommended' | 'Optional' | 'NotUsable' | 'CouldBeUsable' | ''
    files: tuple[FileEntry, ...]
    flags: tuple[tuple[str, str], ...]
    type_patterns: tuple[tuple[Condition, str], ...] = ()


@dataclass(frozen=True)
class Group:
    """One ``<group>`` inside an install step."""

    name: str
    group_type: str  # 'SelectAtLeastOne' | 'SelectAtMostOne' | 'SelectExactlyOne' | 'SelectAll' | 'SelectAny'
    options: tuple[Option, ...]


@dataclass(frozen=True)
class InstallStep:
    """One ``<installStep>``."""

    name: str
    visible: Condition
    groups: tuple[Group, ...]


@dataclass(frozen=True)
class Pattern:
    """One ``<pattern>`` under ``conditionalFileInstalls``."""

    dependencies: Condition
    files: tuple[FileEntry, ...]


@dataclass(frozen=True)
class ModuleConfig:
    """A parsed ``ModuleConfig.xml``."""

    module_name: str
    module_dependencies: Condition
    required: tuple[FileEntry, ...]
    steps: tuple[InstallStep, ...]
    patterns: tuple[Pattern, ...]

    def all_entries(self) -> tuple[FileEntry, ...]:
        """Every entry the config mentions, selected or not.

        Used for ownership: a file under one of these sources belongs to
        this mod even when the entry is not part of the install plan.
        """
        entries: list[FileEntry] = list(self.required)
        for step in self.steps:
            for group in step.groups:
                for option in group.options:
                    entries.extend(option.files)
        for pattern in self.patterns:
            entries.extend(pattern.files)
        return tuple(entries)


# ── Game state and evaluation ───────────────────────────────────────────


@dataclass(frozen=True)
class GameState:
    """What the evaluator knows about the install target.

    ``None`` means unknown, and an unknown input makes the condition
    indeterminate rather than false: refusing to guess is what keeps a
    multi-game installer like NVAC on the legacy fallback instead of
    silently picking one of its game branches.
    """

    plugin_states: Mapping[str, str] | None = None
    game_version: str | None = None
    fomm_version: str | None = None

    def plugin_state(self, name: str) -> str | None:
        if self.plugin_states is None:
            return None
        return self.plugin_states.get(name.lower(), "Missing")


def _version_at_least(actual: str, required: str) -> bool:
    def parts(v: str) -> tuple[int, ...]:
        out: list[int] = []
        for piece in v.strip().split("."):
            digits = "".join(ch for ch in piece if ch.isdigit())
            out.append(int(digits) if digits else 0)
        return tuple(out)

    a, r = parts(actual), parts(required)
    n = max(len(a), len(r))
    a = a + (0,) * (n - len(a))
    r = r + (0,) * (n - len(r))
    return a >= r


def evaluate(condition: Condition, flags: Mapping[str, str], game: GameState) -> bool | None:
    """Tri-state evaluation: True, False, or None when undecidable."""
    if condition is None:
        return True
    if isinstance(condition, FlagDependency):
        return flags.get(condition.flag, "") == condition.value
    if isinstance(condition, FileDependency):
        state = game.plugin_state(condition.file)
        if state is None:
            return None
        return state == condition.state
    if isinstance(condition, GameDependency):
        if game.game_version is None:
            return None
        return _version_at_least(game.game_version, condition.version)
    if isinstance(condition, FommDependency):
        if game.fomm_version is None:
            return None
        return _version_at_least(game.fomm_version, condition.version)
    if isinstance(condition, AllOf):
        seen_none = False
        for dep in condition.deps:
            result = evaluate(dep, flags, game)  # type: ignore[arg-type]
            if result is False:
                return False
            if result is None:
                seen_none = True
        return None if seen_none else True
    if isinstance(condition, AnyOf):
        seen_none = False
        for dep in condition.deps:
            result = evaluate(dep, flags, game)  # type: ignore[arg-type]
            if result is True:
                return True
            if result is None:
                seen_none = True
        return None if seen_none else False
    return None


# ── Parsing ─────────────────────────────────────────────────────────────


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def _parse_xml_bytes(raw: bytes) -> ET.Element | None:
    """UTF-8 first, UTF-16 then cp1252 fallback. Real configs are UTF-16LE."""
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return ET.fromstring(raw.decode(encoding))
        except (UnicodeDecodeError, ET.ParseError):
            continue
    return None


def _parse_xml(config_path: Path) -> ET.Element | None:
    try:
        return _parse_xml_bytes(config_path.read_bytes())
    except OSError:
        return None


def _child_elements(elem: ET.Element, name: str) -> Iterator[ET.Element]:
    for child in elem:
        if _strip_ns(child.tag) == name:
            yield child


def _first_child(elem: ET.Element, name: str) -> ET.Element | None:
    return next(_child_elements(elem, name), None)


def _bool_attr(elem: ET.Element, name: str) -> bool:
    return elem.get(name, "false").strip().lower() == "true"


def _int_attr(elem: ET.Element, name: str) -> int:
    try:
        return int(elem.get(name, "0"))
    except ValueError:
        return 0


def _parse_condition(elem: ET.Element | None) -> Condition:
    """Parse a dependency element or an ``<dependencies>`` container."""
    if elem is None:
        return None
    tag = _strip_ns(elem.tag)
    if tag == "dependencies":
        operator = elem.get("operator", "And").strip().lower()
        deps = tuple(
            parsed
            for child in elem
            if (parsed := _parse_condition(child)) is not None
        )
        if not deps:
            return None
        return AnyOf(deps) if operator == "or" else AllOf(deps)
    if tag == "fileDependency":
        return FileDependency(
            file=elem.get("file", ""),
            state=elem.get("state", "Missing").strip().capitalize(),
        )
    if tag == "flagDependency":
        return FlagDependency(flag=elem.get("flag", ""), value=elem.get("value", ""))
    if tag == "gameDependency":
        return GameDependency(version=elem.get("version", ""))
    if tag == "fommDependency":
        return FommDependency(version=elem.get("version", ""))
    # Unknown wrapper (``<visible>``, ``<moduleDependencies>``): its
    # children are the conditions. One child is that condition; several
    # are implicitly ANDed.
    deps = tuple(
        parsed for child in elem if (parsed := _parse_condition(child)) is not None
    )
    if not deps:
        return None
    return deps[0] if len(deps) == 1 else AllOf(deps)


def _parse_condition_container(elem: ET.Element | None, name: str) -> Condition:
    """Parse ``<name>`` holding either one dependency or a container."""
    if elem is None:
        return None
    target = _first_child(elem, name)
    return _parse_condition(target)


def _parse_file_entry(item: ET.Element) -> FileEntry | None:
    tag = _strip_ns(item.tag)
    if tag not in ("folder", "file"):
        return None
    source = item.get("source", "").replace("\\", "/").strip().strip("/")
    if not source:
        return None
    destination = item.get("destination", "").replace("\\", "/").strip().strip("/")
    return FileEntry(
        source=source,
        destination=destination,
        is_folder=tag == "folder",
        always_install=_bool_attr(item, "alwaysInstall"),
        install_if_usable=_bool_attr(item, "installIfUsable"),
        priority=_int_attr(item, "priority"),
    )


def _parse_file_list(container: ET.Element | None) -> tuple[FileEntry, ...]:
    if container is None:
        return ()
    entries: list[FileEntry] = []
    for item in container:
        entry = _parse_file_entry(item)
        if entry is not None:
            entries.append(entry)
    return tuple(entries)


def _parse_option(plugin: ET.Element) -> Option:
    name = plugin.get("name", "").strip()
    description_elem = _first_child(plugin, "description")
    description = (description_elem.text or "").strip() if description_elem is not None else ""

    flags: list[tuple[str, str]] = []
    flags_elem = _first_child(plugin, "conditionFlags")
    if flags_elem is not None:
        for flag in _child_elements(flags_elem, "flag"):
            # The schema puts the value in an attribute, but real configs
            # (go-fomod's suite) carry it as the element text.
            value = flag.get("value")
            if value is None:
                value = (flag.text or "").strip()
            flags.append((flag.get("name", ""), value))

    option_type = ""
    type_patterns: list[tuple[Condition, str]] = []
    descriptor = _first_child(plugin, "typeDescriptor")
    if descriptor is not None:
        static = _first_child(descriptor, "type")
        if static is not None:
            option_type = static.get("name", "").strip()
        patterns = _first_child(descriptor, "patterns")
        if patterns is not None:
            for pattern in _child_elements(patterns, "pattern"):
                type_elem = _first_child(pattern, "type")
                type_patterns.append(
                    (
                        _parse_condition_container(pattern, "dependencies"),
                        type_elem.get("name", "").strip() if type_elem is not None else "",
                    )
                )

    return Option(
        name=name,
        description=description,
        option_type=option_type,
        files=_parse_file_list(_first_child(plugin, "files")),
        flags=tuple(flags),
        type_patterns=tuple(type_patterns),
    )


def _parse_group(group: ET.Element) -> Group:
    # ``<plugins>`` wraps the plugins in the schema; some hand-written
    # configs put ``<plugin>`` directly under the group. Accept both.
    container = _first_child(group, "plugins")
    source = container if container is not None else group
    options = tuple(
        _parse_option(plugin) for plugin in _child_elements(source, "plugin")
    )
    return Group(
        name=group.get("name", "").strip(),
        group_type=group.get("type", "SelectAny").strip(),
        options=options,
    )


def _parse_step(step: ET.Element) -> InstallStep:
    groups_elem = _first_child(step, "optionalFileGroups")
    groups = tuple(
        _parse_group(group) for group in _child_elements(groups_elem, "group")
    ) if groups_elem is not None else ()
    return InstallStep(
        name=step.get("name", "").strip(),
        visible=_parse_condition_container(step, "visible"),
        groups=groups,
    )


def _parse_pattern(pattern: ET.Element) -> Pattern:
    return Pattern(
        dependencies=_parse_condition_container(pattern, "dependencies"),
        files=_parse_file_list(_first_child(pattern, "files")),
    )


def _build_config(root: ET.Element) -> ModuleConfig:
    module_name = ""
    name_elem = _first_child(root, "moduleName")
    if name_elem is not None and name_elem.text:
        module_name = name_elem.text.strip()

    required: tuple[FileEntry, ...] = ()
    required_elem = _first_child(root, "requiredInstallFiles")
    if required_elem is not None:
        required = _parse_file_list(required_elem)

    steps: tuple[InstallStep, ...] = ()
    steps_elem = _first_child(root, "installSteps")
    if steps_elem is not None:
        steps = tuple(
            _parse_step(step) for step in _child_elements(steps_elem, "installStep")
        )

    patterns: tuple[Pattern, ...] = ()
    conditional = _first_child(root, "conditionalFileInstalls")
    if conditional is not None:
        patterns_elem = _first_child(conditional, "patterns")
        if patterns_elem is not None:
            patterns = tuple(
                _parse_pattern(pattern)
                for pattern in _child_elements(patterns_elem, "pattern")
            )

    return ModuleConfig(
        module_name=module_name,
        module_dependencies=_parse_condition_container(root, "moduleDependencies"),
        required=required,
        steps=steps,
        patterns=patterns,
    )


def load_module_config(config_path: Path) -> ModuleConfig | None:
    """Parse a ``ModuleConfig.xml`` into the full model, or None if unreadable."""
    root = _parse_xml(config_path)
    return None if root is None else _build_config(root)


def load_module_config_bytes(raw: bytes) -> ModuleConfig | None:
    """Parse config bytes straight out of an archive, without extracting it."""
    root = _parse_xml_bytes(raw)
    return None if root is None else _build_config(root)


def plan_source_root(config_path: Path) -> Path:
    """The directory sources are relative to: the ``fomod/`` directory's parent.

    A config at ``<root>/fomod/ModuleConfig.xml`` resolves sources against
    ``<root>``. A wrapped archive (SPID) puts the config at
    ``<root>/Some Mod/fomod/ModuleConfig.xml`` and writes sources against
    ``<root>/Some Mod``. Only the config's own position says which.
    """
    return config_path.parent.parent


# ── Planning ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InstallEntry:
    """A resolved install: source path to copy, destination to copy it to.

    Both paths use ``/`` separators and no leading slash. ``source`` is
    relative to the source root (see ``plan_source_root``); ``destination``
    is relative to the game's data directory.
    """

    source: str
    destination: str
    is_folder: bool
    priority: int
    origin: str  # 'required' | option name | 'conditional'


@dataclass
class InstallPlan:
    """What a given selection installs, plus how much of it was guesswork."""

    entries: tuple[InstallEntry, ...]
    indeterminate: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    def lookup(self, relative_path: str) -> tuple[str, int] | None:
        """Map a source-root-relative file path to its (destination, priority).

        Folder entries cover everything beneath their source. File entries
        cover their exact path. When several entries cover a file the one
        with the highest priority wins, because that is what ``priority``
        means in the schema. Matching is case-insensitive; the destination
        keeps the on-disk casing, since that is what gets installed.
        """
        rel_raw = relative_path.replace("\\", "/").strip().strip("/")
        if not rel_raw:
            return None
        rel = rel_raw.lower()
        best: tuple[str, int] | None = None
        best_priority = -10**9
        for entry in self.entries:
            src = entry.source.lower()
            if entry.is_folder:
                if rel == src:
                    continue
                if not rel.startswith(src + "/"):
                    continue
                remainder = rel_raw[len(entry.source) + 1 :]
                destination = _join_dest(entry.destination, remainder)
            else:
                if rel != src:
                    continue
                destination = entry.destination or entry.source.rsplit("/", 1)[-1]
            if entry.priority >= best_priority:
                best_priority = entry.priority
                best = (destination, entry.priority)
        return best


def _join_dest(destination: str, remainder: str) -> str:
    remainder = remainder.strip().strip("/")
    if not destination:
        return remainder
    if not remainder:
        return destination
    return f"{destination}/{remainder}"


def _normalize_option_type(option: Option, flags: Mapping[str, str], game: GameState) -> tuple[str, bool]:
    """Resolve an option's effective type. Returns (type, indeterminate)."""
    if option.type_patterns:
        seen_none = False
        for condition, type_name in option.type_patterns:
            result = evaluate(condition, flags, game)
            if result is True:
                return type_name, False
            if result is None:
                seen_none = True
        return ("Optional", seen_none)
    return (option.option_type, False)


def _group_defaults(group: Group) -> tuple[str, ...]:
    """What a compliant installer pre-selects when the user picks nothing."""
    if group.group_type == "SelectAll":
        return tuple(option.name for option in group.options)
    picked = [o.name for o in group.options if o.option_type == "Required"]
    if not picked:
        picked = [o.name for o in group.options if o.option_type == "Recommended"]
    return tuple(picked)


Selections = Mapping[tuple[str, str], tuple[str, ...]]


def resolve_install_plan(
    config: ModuleConfig,
    selections: Selections | None = None,
    game: GameState | None = None,
) -> InstallPlan:
    """Compute what the given selections install.

    ``selections`` maps ``(step_name, group_name)`` to the chosen option
    names. A group with no entry falls back to the schema's defaults
    (Required options, or Recommended when there are none; every option
    of a ``SelectAll`` group). Options of type Required install even when
    nothing selected them.

    The result is marked indeterminate whenever a condition that decided
    something could not be evaluated, so callers can fall back rather
    than ship a guess.
    """
    game = game or GameState()
    selections = selections or {}
    indeterminate = False
    notes: list[str] = []

    module_gate = evaluate(config.module_dependencies, {}, game)
    if module_gate is False:
        return InstallPlan(entries=(), indeterminate=False, notes=("module dependencies not met",))
    if module_gate is None:
        # Requirements like "SKSE is installed" are a warning, not a
        # file-selection decision. Dropping the plan here would fall back
        # to installing every option at once, which is strictly worse.
        notes.append("module dependencies could not be verified")

    selected_entries: list[InstallEntry] = []
    flags: dict[str, str] = {}

    def add_entry(entry: FileEntry, origin: str, install: bool) -> None:
        if install:
            selected_entries.append(
                InstallEntry(
                    source=entry.source,
                    destination=_strip_data_prefix(entry.destination),
                    is_folder=entry.is_folder,
                    priority=entry.priority,
                    origin=origin,
                )
            )

    # Required content installs whatever the user picks.
    for entry in config.required:
        add_entry(entry, "required", True)

    # Steps in order: flags set by an earlier step gate later visibility.
    for step in config.steps:
        visible = evaluate(step.visible, flags, game)
        if visible is False:
            continue
        if visible is None:
            indeterminate = True
            notes.append(f"step {step.name!r} visibility could not be evaluated")

        for group in step.groups:
            key = (step.name, group.name)
            if key in selections:
                chosen = {name.strip().lower() for name in selections[key]}
            else:
                chosen = {name.strip().lower() for name in _group_defaults(group)}

            for option in group.options:
                option_type, type_uncertain = _normalize_option_type(option, flags, game)
                is_selected = option.name.strip().lower() in chosen
                if option_type == "Required":
                    is_selected = True
                if group.group_type == "SelectAll":
                    is_selected = True

                # An uncertain type only matters where the type decides
                # something: whether an un-picked option defaults in, or
                # whether its installIfUsable entries count as usable.
                # An explicitly picked option installs its files whatever
                # the type says.
                if type_uncertain and not is_selected:
                    indeterminate = True

                if is_selected:
                    for flag_name, flag_value in option.flags:
                        flags[flag_name] = flag_value

                usable = option_type not in ("NotUsable",)
                for entry in option.files:
                    install = (
                        is_selected
                        or entry.always_install
                        or (entry.install_if_usable and usable)
                    )
                    add_entry(entry, option.name, install)

    # Conditional file installs: patterns whose dependencies hold.
    for index, pattern in enumerate(config.patterns):
        result = evaluate(pattern.dependencies, flags, game)
        if result is None:
            indeterminate = True
            notes.append(f"conditional pattern {index} could not be evaluated")
            continue
        if result:
            for entry in pattern.files:
                add_entry(entry, "conditional", True)

    return InstallPlan(
        entries=tuple(selected_entries),
        indeterminate=indeterminate,
        notes=tuple(notes),
    )


def _strip_data_prefix(destination: str) -> str:
    destination = destination.replace("\\", "/").strip().strip("/")
    if destination.lower() == "data":
        return ""
    if destination.lower().startswith("data/"):
        return destination[5:].strip().strip("/")
    return destination


def iter_manifest_choices(choices: Mapping[str, Any]) -> Iterator[tuple[str, str, str]]:
    """Yield ``(step_name, group_name, option_name)`` for each recorded pick.

    The manifest records ``{"type": "fomod", "options": [{"name": <step>,
    "groups": [{"name": <group>, "choices": [{"name": <option>, "idx": N}]}]}]}``,
    verbatim from a live collection's state. The step name may be empty
    and the option name may not. ``idx`` is carried as data only: what it
    counts has never been verified against a live collection, and nothing
    here locates an option by it.
    """
    for step in choices.get("options") or []:
        step_name = str(step.get("name") or "").strip()
        for group in step.get("groups") or []:
            group_name = str(group.get("name") or "").strip()
            for pick in group.get("choices") or []:
                option_name = str(pick.get("name") or "").strip()
                if option_name:
                    yield step_name, group_name, option_name


def _match_by_names(
    candidates: list[tuple[str, str]], step_name: str, group_name: str
) -> tuple[str, str]:
    """Pick the candidate a manifest's own names point at.

    The group name decides; the step name only narrows further when the
    manifest has one, because a real manifest's step name can be empty
    while the config's is not.
    """
    want_step = step_name.strip().lower()
    want_group = group_name.strip().lower()
    for cand_step, cand_group in candidates:
        if cand_group.strip().lower() != want_group:
            continue
        if want_step and cand_step.strip().lower() != want_step:
            continue
        return cand_step, cand_group
    for cand_step, cand_group in candidates:
        if cand_group.strip().lower() == want_group:
            return cand_step, cand_group
    return candidates[0]


def selections_from_manifest(
    choices: Mapping[str, Any], config: ModuleConfig
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Convert a collection manifest's choice structure to planner selections.

    Each recorded option is located by name against the config, so the
    keys are the config's own ``(step_name, group_name)`` pairs whatever
    names the manifest carries. When the same option name appears in more
    than one group, the manifest's group and step names disambiguate; an
    option that matches nothing in the config is dropped.
    """
    by_name: dict[str, list[tuple[str, str]]] = {}
    for step in config.steps:
        for group in step.groups:
            for option in group.options:
                by_name.setdefault(option.name.strip().lower(), []).append(
                    (step.name, group.name)
                )

    picked: dict[tuple[str, str], list[str]] = {}
    for step_name, group_name, option_name in iter_manifest_choices(choices):
        candidates = by_name.get(option_name.lower())
        if not candidates:
            continue
        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            chosen = _match_by_names(candidates, step_name, group_name)
        picked.setdefault(chosen, []).append(option_name)

    return {key: tuple(names) for key, names in picked.items()}
