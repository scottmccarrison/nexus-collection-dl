"""FOMOD option folder resolution - filter unselected mod options during deploy."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path


def parse_module_config(config_path: Path) -> dict[str, str]:
    """
    Parse a ModuleConfig.xml to map option names to their source folders.

    Returns dict of {option_name_lower: source_folder_name}.
    Handles UTF-16 encoded files (common in FOMOD configs).
    """
    try:
        # Try UTF-8 first, then UTF-16
        try:
            tree = ET.parse(config_path)
        except ET.ParseError:
            with open(config_path, "r", encoding="utf-16") as f:
                content = f.read()
            tree = ET.ElementTree(ET.fromstring(content))
    except Exception:
        return {}

    root = tree.getroot()
    # Strip namespace if present
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    option_to_folder: dict[str, str] = {}

    # Find all plugin entries (install options)
    for plugin in root.iter(f"{ns}plugin") if ns else root.iter("plugin"):
        name = plugin.get("name", "")
        if not name:
            continue

        # Look for files/folders in this plugin's install section
        for files_elem in plugin.iter(f"{ns}files") if ns else plugin.iter("files"):
            for folder in files_elem.iter(f"{ns}folder") if ns else files_elem.iter("folder"):
                source = folder.get("source", "")
                if source:
                    option_to_folder[name.lower()] = source
                    break
            for file_elem in files_elem.iter(f"{ns}file") if ns else files_elem.iter("file"):
                source = file_elem.get("source", "")
                if source:
                    # Use the top-level folder from the source path
                    top_folder = source.split("/")[0].split("\\")[0]
                    if top_folder:
                        option_to_folder[name.lower()] = top_folder
                    break

    return option_to_folder


def _get_required_folders(config_path: Path) -> set[str]:
    """Extract requiredInstallFiles folders from ModuleConfig.xml."""
    try:
        try:
            tree = ET.parse(config_path)
        except ET.ParseError:
            with open(config_path, "r", encoding="utf-16") as f:
                content = f.read()
            tree = ET.ElementTree(ET.fromstring(content))
    except Exception:
        return set()

    root = tree.getroot()
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    required: set[str] = set()
    for req in root.iter(f"{ns}requiredInstallFiles") if ns else root.iter("requiredInstallFiles"):
        for folder in req.iter(f"{ns}folder") if ns else req.iter("folder"):
            source = folder.get("source", "")
            if source:
                top = source.split("/")[0].split("\\")[0]
                if top:
                    required.add(top.lower())
        for file_elem in req.iter(f"{ns}file") if ns else req.iter("file"):
            source = file_elem.get("source", "")
            if source:
                top = source.split("/")[0].split("\\")[0]
                if top:
                    required.add(top.lower())

    return required


def resolve_selected_folders(choices: dict, option_to_folder: dict[str, str]) -> set[str]:
    """
    Cross-reference manifest choices with ModuleConfig option-to-folder mapping.

    Returns set of SELECTED folder names (lowercase).
    """
    selected: set[str] = set()

    # choices structure from collection.json:
    # {"steps": [{"stepId": N, "selectedOptions": [{"groupId": N, "optionId": N, "optionName": "..."}]}]}
    steps = choices.get("steps", [])
    for step in steps:
        for option in step.get("selectedOptions", []):
            option_name = option.get("optionName", "").lower()
            if option_name in option_to_folder:
                folder = option_to_folder[option_name]
                selected.add(folder.lower())

    return selected


# Pattern for numbered option folders
_NUMBERED_PREFIX_RE = re.compile(r"^\d{2,3}\s*[-_]\s*(.+)$")


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
    """
    Build set of folder names to SKIP during deployment (unselected FOMOD options).

    Strategy:
    1. For mods with ModuleConfig.xml on disk: parse it, cross-reference with choices
    2. For mods with choices but no ModuleConfig.xml: use name matching on numbered folders

    Returns set of folder names (original case from disk) to skip.
    """
    skip_set: set[str] = set()

    if not mod_choices:
        return skip_set

    # Collect all choice option names for name-matching fallback
    all_choice_names: dict[int, set[str]] = {}
    all_selected_names: dict[int, set[str]] = {}
    for mod_id, choices in mod_choices.items():
        selected_names: set[str] = set()
        all_names: set[str] = set()
        for step in choices.get("steps", []):
            for option in step.get("selectedOptions", []):
                name = option.get("optionName", "").lower()
                if name:
                    selected_names.add(name)
                    all_names.add(name)
            # Also collect UNselected options if available
            # The manifest only has selectedOptions, so all_names == selected_names
            # We'll use folder enumeration to find unselected ones
        all_selected_names[mod_id] = selected_names
        all_choice_names[mod_id] = all_names

    # Strategy 1: Find ModuleConfig.xml files at root of mods_dir
    config_handled_mods: set[int] = set()
    for config_path in mods_dir.glob("**/ModuleConfig.xml"):
        # Only process configs at reasonable depth (not deeply nested)
        rel = config_path.relative_to(mods_dir)
        if len(rel.parts) > 3:
            continue

        option_to_folder = parse_module_config(config_path)
        if not option_to_folder:
            continue

        required_folders = _get_required_folders(config_path)

        # Try to match this config to a mod with choices
        for mod_id, choices in mod_choices.items():
            if mod_id in config_handled_mods:
                continue

            selected = resolve_selected_folders(choices, option_to_folder)
            if not selected:
                continue

            config_handled_mods.add(mod_id)

            # All folders mentioned in the config that are NOT selected and NOT required
            all_config_folders = {f.lower() for f in option_to_folder.values()}
            unselected = all_config_folders - selected - required_folders

            # Find actual folder names on disk that match unselected folders
            for item in mods_dir.iterdir():
                if item.is_dir() and item.name.lower() in unselected:
                    skip_set.add(item.name)

    # Strategy 2: Name matching for mods with choices but no ModuleConfig.xml
    # Look at numbered folders on disk and match against choice names
    numbered_folders = []
    for item in mods_dir.iterdir():
        if item.is_dir() and _NUMBERED_PREFIX_RE.match(item.name):
            numbered_folders.append(item.name)

    if numbered_folders:
        for mod_id, choices in mod_choices.items():
            if mod_id in config_handled_mods:
                continue

            selected_names = all_selected_names.get(mod_id, set())
            if not selected_names:
                continue

            # For each numbered folder, check if it matches a selected option
            for folder_name in numbered_folders:
                if not _fuzzy_match_folder_to_choice(folder_name, selected_names):
                    # This folder doesn't match any selected option - skip it
                    skip_set.add(folder_name)

    return skip_set
