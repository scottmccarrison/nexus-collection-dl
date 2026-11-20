"""Turning user input into FOMOD option picks.

Three inputs feed the same place: repeatable ``--fomod-option`` flags, a
``--fomod-config`` JSON file, and (for a human at a tty) the interactive
prompts. All three produce :class:`Pick` records, and picks are matched
against the parsed config to give the ``(step, group) -> option names``
mapping the install plan wants.

Picks are stored as (step, group, option) triples rather than as the
resolved mapping so that a later run, facing a changed installer, can
work out which picks still name real options and which need asking
again. That is the "keep matching picks, re-ask the rest" rule.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .fomod_plan import ModuleConfig, Option

__all__ = [
    "Pick",
    "PickAsker",
    "PickError",
    "load_picks_file",
    "normalize_picks",
    "parse_option_flags",
    "picks_for_file",
    "picks_to_selections",
    "prompt_for_picks",
]


class PickError(ValueError):
    """A pick does not name something the installer offers.

    The message names the group or option as the user wrote it. It never
    names a file path or an issue number.
    """


@dataclass(frozen=True)
class Pick:
    """One (step, group, option) choice.

    ``step`` may be empty when the group name is unique in the config;
    the matcher resolves it and complains only when the name is
    ambiguous. ``file`` scopes a pick to one archive in a batch import
    and is empty for the single-mod case; it is input plumbing and is
    not persisted.
    """

    step: str
    group: str
    option: str
    file: str = ""


def parse_option_flags(values: Sequence[str]) -> list[Pick]:
    """Parse repeatable ``--fomod-option`` values.

    Two forms: ``Group:Option`` and ``Step:Group:Option``. The first
    colon separates, so a name that contains a colon has to go through
    the JSON file instead. That limitation is deliberate: splitting on
    the last colon, or trying each split in turn, turns one typo into a
    confusing silent mismatch.
    """
    picks: list[Pick] = []
    for raw in values:
        parts = raw.split(":", 2)
        if len(parts) == 2:
            group, option = parts
            picks.append(Pick(step="", group=group.strip(), option=option.strip()))
        elif len(parts) == 3:
            step, group, option = parts
            picks.append(
                Pick(step=step.strip(), group=group.strip(), option=option.strip())
            )
        else:
            raise PickError(
                f"--fomod-option needs 'Group:Option' or 'Step:Group:Option', got {raw!r}"
            )
        if not picks[-1].group or not picks[-1].option:
            raise PickError(f"--fomod-option has an empty group or option: {raw!r}")
    return picks


def load_picks_file(path: Path) -> list[Pick]:
    """Read picks from a JSON file.

    The file is a list of objects, each with ``group`` and ``option``,
    an optional ``step``, and an optional ``file`` naming the archive
    the pick belongs to in a batch import:

    ``[{"group": "Installation", "option": "Extended"},
    {"step": "Rigs", "group": "Animation Rig Map", "option": "XPMS(S)E TBBP"}]``
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise PickError(f"cannot read the FOMOD choices file: {e.strerror or e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise PickError(f"the FOMOD choices file is not valid JSON: {e.msg}") from e

    if not isinstance(data, list):
        raise PickError("the FOMOD choices file must be a JSON list of picks")

    picks: list[Pick] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise PickError(f"pick {index + 1} is not a JSON object")
        group = str(item.get("group", "")).strip()
        option = str(item.get("option", "")).strip()
        step = str(item.get("step", "")).strip()
        file = str(item.get("file", "")).strip()
        if not group or not option:
            raise PickError(f"pick {index + 1} needs a 'group' and an 'option'")
        picks.append(Pick(step=step, group=group, option=option, file=file))
    return picks


def normalize_picks(picks: Sequence[Pick], config: ModuleConfig) -> list[Pick]:
    """Resolve picks to the (step, group) names the config actually uses.

    Stored picks are the record a later run re-reads, so they carry the
    resolved step and group rather than whatever shorthand the user
    typed. Raises :class:`PickError` when a pick names nothing, which is
    the same error a bad flag gives.
    """
    selections, unmatched = picks_to_selections(picks, config)
    if unmatched:
        names = ", ".join(f"{p.group}:{p.option}" for p in unmatched)
        raise PickError(f"the installer has no such FOMOD option: {names}")
    out: list[Pick] = []
    for (step_name, group_name), option_names in selections.items():
        for option in option_names:
            out.append(Pick(step=step_name, group=group_name, option=option))
    return out


def picks_for_file(picks: Sequence[Pick], filename: str) -> list[Pick]:
    """The picks that apply to one archive: unscoped picks plus its own."""
    name = filename.strip().lower()
    return [p for p in picks if not p.file or p.file.strip().lower() == name]


def _group_locations(config: ModuleConfig, group_name: str) -> list[tuple[str, str]]:
    wanted = group_name.strip().lower()
    return [
        (step.name, group.name)
        for step in config.steps
        for group in step.groups
        if group.name.strip().lower() == wanted
    ]


def picks_to_selections(
    picks: Sequence[Pick], config: ModuleConfig
) -> tuple[dict[tuple[str, str], tuple[str, ...]], list[Pick]]:
    """Match picks against the config.

    Returns the selections the install plan wants plus the picks that
    name nothing, so a caller can report or re-ask them. A pick whose
    group appears in more than one step and gives no step is rejected
    rather than guessed at.

    Group and option names match case-insensitively and with surrounding
    space ignored. A pick whose option does not exist inside its group is
    unmatched even when the name exists elsewhere: silently mapping it
    somewhere else is how a wrong install happens.
    """
    chosen: dict[tuple[str, str], list[str]] = {}
    unmatched: list[Pick] = []

    for pick in picks:
        locations = _group_locations(config, pick.group)
        if pick.step:
            wanted_step = pick.step.strip().lower()
            locations = [loc for loc in locations if loc[0].strip().lower() == wanted_step]

        if not locations:
            unmatched.append(pick)
            continue
        if len(locations) > 1:
            raise PickError(
                f"group {pick.group!r} appears in more than one step; "
                f"give the step as 'Step:Group:Option'"
            )

        step_name, group_name = locations[0]
        group = next(
            g
            for s in config.steps
            if s.name == step_name
            for g in s.groups
            if g.name == group_name
        )
        option_names = {o.name.strip().lower() for o in group.options}
        if pick.option.strip().lower() not in option_names:
            unmatched.append(pick)
            continue

        chosen.setdefault((step_name, group_name), []).append(pick.option.strip())

    return {key: tuple(names) for key, names in chosen.items()}, unmatched


# ── the interactive walk ────────────────────────────────────────────────


class PickAsker(Protocol):
    """How the picker talks to a human.

    Two front-ends drive the same walk: the CLI's click prompts and the
    shell's plain ``input()`` lines. Both implement these three methods;
    the walk below never touches a terminal itself.

    ``choose_one`` returns a 1-based index into the options, or None when
    the question allows no answer. ``choose_many`` returns 1-based
    indices. Both re-ask on bad input until they get something valid.
    """

    def tell(self, text: str) -> None: ...

    def choose_one(self, prompt: str, count: int, allow_none: bool) -> int | None: ...

    def choose_many(
        self, prompt: str, count: int, at_least_one: bool
    ) -> list[int]: ...


def prompt_for_picks(
    config: ModuleConfig, archive_name: str, kept: Sequence[Pick], asker: PickAsker
) -> list[Pick]:
    """Walk the installer's groups and collect the picks ``kept`` lacks.

    Asks in install order because an answer sets flags and a later step's
    ``visible`` condition can depend on them: asking everything up front
    would evaluate a step's visibility before the flags that reveal it
    exist. Groups already covered by ``kept`` are not asked again, which
    is the keep-matching-picks rule on the interactive path.
    """
    from .fomod_plan import GameState, evaluate

    resolved, _ = picks_to_selections(kept, config) if kept else ({}, [])
    flags: dict[str, str] = {}
    picks: list[Pick] = []
    header_shown = False

    def record(option: Option, step_name: str, group_name: str) -> None:
        picks.append(Pick(step=step_name, group=group_name, option=option.name))
        flags.update(option.flags)

    def header() -> None:
        nonlocal header_shown
        if not header_shown:
            header_shown = True
            asker.tell(f"FOMOD options for {archive_name}")

    for step in config.steps:
        if evaluate(step.visible, flags, GameState()) is False:
            continue
        for group in step.groups:
            if (step.name, group.name) in resolved:
                # Already chosen: absorb its flags for the steps below,
                # but do not return it again. The caller owns the kept
                # picks and prepends them to what this walk returns.
                wanted = {n.strip().lower() for n in resolved[(step.name, group.name)]}
                for option in group.options:
                    if option.name.strip().lower() in wanted:
                        flags.update(option.flags)
                continue
            if group.group_type == "SelectAll":
                for option in group.options:
                    record(option, step.name, group.name)
                continue

            for option in group.options:
                if option.option_type == "Required":
                    record(option, step.name, group.name)
            askable = [
                o
                for o in group.options
                if o.option_type not in ("Required", "NotUsable")
            ]
            if not askable:
                continue

            header()
            asker.tell(f"{step.name} / {group.name} ({group.group_type})")
            for i, option in enumerate(askable, 1):
                hint = " [recommended]" if option.option_type == "Recommended" else ""
                asker.tell(f"  {i}. {option.name}{hint}")
                if option.description:
                    asker.tell(f"     {option.description}")

            if group.group_type in ("SelectExactlyOne", "SelectAtMostOne"):
                allow_none = group.group_type == "SelectAtMostOne"
                n = asker.choose_one(
                    "  Pick one" + (", or press Enter for none" if allow_none else ""),
                    len(askable),
                    allow_none=allow_none,
                )
                if n is not None:
                    record(askable[n - 1], step.name, group.name)
            else:
                at_least_one = group.group_type == "SelectAtLeastOne"
                nums = asker.choose_many(
                    "  Pick any, comma separated"
                    + (" (at least one)" if at_least_one else ""),
                    len(askable),
                    at_least_one=at_least_one,
                )
                for n in nums:
                    record(askable[n - 1], step.name, group.name)
    return picks
