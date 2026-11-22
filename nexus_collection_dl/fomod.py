"""FOMOD option folder resolution - filter unselected mod options during deploy.

This is a heuristic, and it is worth being clear about what it can and
cannot do, because it runs at deploy time over an extraction tree where
every mod's archive contents are already merged.

What it does: given the option names a collection manifest says were
selected, work out which top-level directories belong to options that
were NOT selected, and hand those names back so the deploy can skip them.

What it cannot do, and why:

* **It works on top-level directories only.** ``deploy`` matches a skip
  name against ``parts[0]`` of a relative path, so two options that
  install from a shared top-level directory cannot be separated by this
  filter. The ``uunp``/``cbbe``/``vanilla`` layout (one top-level per
  option) is separable; a ``textures``/``meshes`` layout is not.
* **It does not know which mod produced which folder.** Once extracted,
  ownership is gone. The fallback below avoids one mod's choices
  skipping another mod's folders, but it can still keep a folder it
  should skip when names happen to overlap.
* **It reads every plugin regardless of its group.** A plugin in a group
  the manifest never offered still contributes folders here.

Correct option handling means parsing the installer properly and
extracting only the chosen folders at extraction time. Until that lands,
this narrows the damage rather than preventing it.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .fomod_plan import iter_manifest_choices


def _parse_xml(config_path: Path) -> ET.Element | None:
    """Read a ModuleConfig.xml whatever it is encoded as.

    Real configs are commonly UTF-16LE with a BOM and CRLF; Nexus
    archives have also been seen in UTF-8 and Windows-1252 regardless of
    the XML declaration, so try UTF-8 first (ElementTree's default) and
    fall back to UTF-16.
    """
    try:
        try:
            return ET.parse(config_path).getroot()
        except ET.ParseError:
            with open(config_path, "r", encoding="utf-16") as f:
                content = f.read()
            return ET.fromstring(content)
    except Exception:
        return None


def _top_level(source: str, is_folder: bool) -> str:
    """First path component of an archive-relative source, or "".

    ``source`` is relative to the archive root and real configs use
    backslashes (``uunp\\textures``, ``01 Main Mod\\Foo.esp``). After
    extraction the first component is the top-level directory name in
    the extraction tree, which is the granularity the deploy filter
    matches at.

    A source with no separator is ambiguous on its own: ``<folder
    source="pack">`` is a top-level *directory*, while ``<file
    source="addon.txt">`` is a file at the archive root. The element tag
    decides. A root-level file has no directory to skip, so it
    contributes nothing rather than a bogus directory name.
    """
    normalised = source.replace("\\", "/").strip()
    if "/" not in normalised:
        return normalised.lower() if is_folder else ""
    return normalised.split("/")[0].strip().lower()


def parse_module_config(config_path: Path) -> dict[str, set[str]]:
    """Map each option name (lowercased) to its archive top-level dirs.

    Both ``<folder>`` and ``<file>`` sources are reduced to their first
    path component, because the deploy filter matches against top-level
    directory names. Every source of a plugin is collected, not just the
    first: a plugin commonly installs both textures and meshes, and
    dropping the rest silently loses the folders that should have been
    skipped when the option is unselected.
    """
    root = _parse_xml(config_path)
    if root is None:
        return {}

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    def tag(name: str) -> str:
        return f"{ns}{name}" if ns else name

    option_to_folders: dict[str, set[str]] = {}

    for plugin in root.iter(tag("plugin")):
        name = plugin.get("name", "").strip().lower()
        if not name:
            continue
        folders = option_to_folders.setdefault(name, set())
        for files_elem in plugin.iter(tag("files")):
            for child in files_elem:
                if child.tag not in (tag("folder"), tag("file")):
                    continue
                top = _top_level(
                    child.get("source", ""), child.tag == tag("folder")
                )
                if top:
                    folders.add(top)

    return option_to_folders


def _get_required_folders(config_path: Path) -> set[str]:
    """Top-level archive dirs under ``requiredInstallFiles``, lowercased.

    These always install whatever the user picks, so they are never
    skipped.
    """
    root = _parse_xml(config_path)
    if root is None:
        return set()

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    def tag(name: str) -> str:
        return f"{ns}{name}" if ns else name

    required: set[str] = set()
    for req in root.iter(tag("requiredInstallFiles")):
        for child in req:
            if child.tag not in (tag("folder"), tag("file")):
                continue
            top = _top_level(child.get("source", ""), child.tag == tag("folder"))
            if top:
                required.add(top)

    return required


def _names_match(choices: dict, option_to_folders: dict[str, set[str]]) -> bool:
    """True when any selected option name appears in the config.

    This is the config-to-mod match. It must not be based on whether the
    selection maps to folders: an option can be a valid selection and
    install nothing of its own (a config whose plugin carries only
    ``conditionFlags``, for instance), and treating that as "wrong
    config" would push a real selection into the name-matching fallback.
    """
    for _, _, option_name in iter_manifest_choices(choices):
        if option_name.strip().lower() in option_to_folders:
            return True
    return False


def resolve_selected_folders(
    choices: dict, option_to_folders: dict[str, set[str]]
) -> set[str]:
    """Archive top-level dirs belonging to the options that were selected.

    ``choices`` is the manifest's own structure (see
    ``fomod_plan.iter_manifest_choices``). Only the selected options are
    present, so the unselected side has to come from the config.
    """
    selected: set[str] = set()
    for _, _, option_name in iter_manifest_choices(choices):
        name = option_name.strip().lower()
        if name in option_to_folders:
            selected |= option_to_folders[name]
    return selected


# Pattern for numbered option folders: two or three digits, a separator,
# then the name. The separator is space, dash or underscore. Real FOMOD
# archives use a plain space far more often than the dash this originally
# assumed (`01 Main Mod`, `02 Military Camps` in the test fixtures), and
# a folder like `01Main` with no separator at all is not an option
# folder and must not match.
_NUMBERED_PREFIX_RE = re.compile(r"^\d{2,3}[\s\-_]+(.+)$")


def _fuzzy_match_folder_to_choice(folder_name: str, choice_names: set[str]) -> bool:
    """Check if a folder name (with number prefix stripped) matches any choice name."""
    # Strip numbered prefix: "01 - Bright Bullet Tracers" -> "Bright Bullet Tracers"
    m = _NUMBERED_PREFIX_RE.match(folder_name)
    if m:
        stripped = m.group(1).strip().lower()
    else:
        stripped = folder_name.strip().lower()

    # Exact match
    if stripped in choice_names:
        return True

    # Partial match - folder name contained in a choice or vice versa
    for choice in choice_names:
        if stripped in choice or choice in stripped:
            return True

    return False


def build_fomod_skip_set(mods_dir: Path, mod_choices: dict[int, dict]) -> set[str]:
    """Top-level folder names to skip during deployment.

    Strategy 1: a ``ModuleConfig.xml`` on disk is parsed and its option
    folders cross-referenced against the manifest's choices. Strategy 2
    covers mods that have choices but no usable config, by matching
    numbered folders on disk against the selected option names.

    Returns folder names as they appear on disk (original case), because
    ``deploy`` compares them against the real first path component.
    """
    skip_set: set[str] = set()

    if not mod_choices:
        return skip_set

    all_selected_names: dict[int, set[str]] = {}
    for mod_id, choices in mod_choices.items():
        selected_names: set[str] = set()
        for _, _, option_name in iter_manifest_choices(choices):
            name = option_name.strip().lower()
            if name:
                selected_names.add(name)
        all_selected_names[mod_id] = selected_names

    # Strategy 1: parse the configs that are on disk.
    config_handled_mods: set[int] = set()
    for config_path in mods_dir.glob("**/ModuleConfig.xml"):
        # Only process configs at reasonable depth (not deeply nested)
        rel = config_path.relative_to(mods_dir)
        if len(rel.parts) > 3:
            continue

        option_to_folders = parse_module_config(config_path)
        if not option_to_folders:
            continue

        required_folders = _get_required_folders(config_path)

        # Try to match this config to a mod with choices
        for mod_id, choices in mod_choices.items():
            if mod_id in config_handled_mods:
                continue

            if not _names_match(choices, option_to_folders):
                continue

            selected = resolve_selected_folders(choices, option_to_folders)
            config_handled_mods.add(mod_id)

            # Top-level dirs the config mentions that are neither
            # selected nor always-installed are the unselected options.
            all_config_dirs: set[str] = set()
            for dirs in option_to_folders.values():
                all_config_dirs |= dirs
            unselected = all_config_dirs - selected - required_folders

            for item in mods_dir.iterdir():
                if item.is_dir() and item.name.lower() in unselected:
                    skip_set.add(item.name)

    # Strategy 2: numbered folders for mods that have choices but no
    # usable config. If every mod with choices was matched to a config
    # above there is nothing left for the fallback to guess about.
    unhandled = [
        m for m in all_selected_names if m not in config_handled_mods
        and all_selected_names[m]
    ]
    numbered_folders = []
    if unhandled:
        for item in mods_dir.iterdir():
            if item.is_dir() and _NUMBERED_PREFIX_RE.match(item.name):
                numbered_folders.append(item.name)

    if numbered_folders:
        # A folder is skipped only when it matches NO mod's selected
        # names - including mods the config already handled, so a
        # folder someone picked is never skipped as a side effect of
        # guessing about a different mod. Matching per mod would let one
        # mod's choices skip another mod's folders: the extraction tree
        # is shared and folder ownership is not recorded, so "this
        # folder does not match mod B" does not mean "this folder is
        # unselected".
        #
        # The residual risk is the opposite error: a folder that
        # fuzzy-matches some mod's selected name is kept even when it is
        # unselected for its own mod. Keeping too much is the safer of
        # the two here, and it is still wrong.
        every_selected: set[str] = set()
        for selected_names in all_selected_names.values():
            every_selected |= selected_names

        if every_selected:
            for folder_name in numbered_folders:
                if not _fuzzy_match_folder_to_choice(folder_name, every_selected):
                    skip_set.add(folder_name)

    return skip_set
