"""Terminal progress for long operations.

Two halves, and the tests pin both.

1. The deploy write loop reports itself. It used to say "Deploying N
   files" and then go silent for the whole loop, which on a real
   collection is long enough to read as a hang.
2. A progress line renderer that updates in place on a TTY, prints
   periodic lines when not, and prints nothing when disabled (`--json`).

Downloads already have a Rich progress bar (downloader.py). Extraction
reports per-mod through the service's progress callback. The write loop
was the silent phase.
"""

from __future__ import annotations

import io
from pathlib import Path

from nexus_collection_dl.deploy import (
    BackupStore,
    DeployJournal,
    DeploymentPlan,
)
from nexus_collection_dl.deploy import (
    deploy as run_deploy,
)
from nexus_collection_dl.progress import ProgressLine


class FakeClock:
    """A clock the tests can step without sleeping."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


def _plan(staging: Path, names: list[str]) -> DeploymentPlan:
    entries = []
    for name in names:
        src = staging / name
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(name.encode())
        entries.append((src, Path(name)))
    return DeploymentPlan(data_files=entries, source_root=staging)


# ---------------------------------------------------------------------------
# The deploy write loop reports itself
# ---------------------------------------------------------------------------


def test_deploy_reports_progress_for_each_file(tmp_path):
    """The loop has to say where it is, not just that it started.

    "Deploying N files" followed by silence is the hang shape. The
    callback must advance as files land.
    """
    staging = tmp_path / "staging"
    game = tmp_path / "game"
    game.mkdir()
    plan = _plan(staging, ["a.esp", "b.esp", "c.esp"])

    events: list[tuple[int, int, str]] = []
    run_deploy(
        plan,
        game,
        method="copy",
        dry_run=False,
        backups=BackupStore(game),
        journal=DeployJournal(game),
        ownership={},
        on_progress=lambda done, total, detail: events.append((done, total, detail)),
    )

    assert events, "the write loop reported nothing"
    dones = [done for done, _total, _detail in events]
    assert dones == sorted(dones), f"progress went backwards: {dones}"
    assert dones[-1] == 3
    for _done, total, _detail in events:
        assert total == 3


def test_deploy_progress_names_the_file_being_written(tmp_path):
    """The detail has to place the user in the loop, not just count."""
    staging = tmp_path / "staging"
    game = tmp_path / "game"
    game.mkdir()
    plan = _plan(staging, ["a.esp", "b.esp"])

    details: list[str] = []
    run_deploy(
        plan,
        game,
        method="copy",
        dry_run=False,
        backups=BackupStore(game),
        journal=DeployJournal(game),
        ownership={},
        on_progress=lambda done, total, detail: details.append(detail),
    )

    joined = " ".join(details)
    assert "a.esp" in joined
    assert "b.esp" in joined


def test_deploy_without_a_callback_still_works(tmp_path):
    """The callback is optional: existing callers must not break."""
    staging = tmp_path / "staging"
    game = tmp_path / "game"
    game.mkdir()
    plan = _plan(staging, ["a.esp"])
    result = run_deploy(
        plan,
        game,
        method="copy",
        dry_run=False,
        backups=BackupStore(game),
        journal=DeployJournal(game),
        ownership={},
    )
    assert result.errors == []
    assert (game / "Data" / "a.esp").exists()


# ---------------------------------------------------------------------------
# The renderer: in place on a TTY, periodic lines when not
# ---------------------------------------------------------------------------


def test_progress_line_updates_in_place_on_a_tty():
    """A TTY run overwrites one line instead of scrolling hundreds."""
    out = io.StringIO()
    clock = FakeClock()
    line = ProgressLine(total=3, label="Deploying", stream=out, is_tty=True, clock=clock)

    for i in range(1, 4):
        line.update(i, f"file{i}.esp")
    line.close()

    text = out.getvalue()
    assert "\r" in text, "a TTY run must return to the start of the line"
    assert text.count("\n") <= 2, f"spammed newlines: {text!r}"
    assert "file1.esp" in text
    assert "file3.esp" in text


def test_progress_line_prints_periodic_lines_off_a_tty():
    """A pipe or a log gets lines on a cadence, not one per file."""
    out = io.StringIO()
    clock = FakeClock()
    line = ProgressLine(
        total=100,
        label="Deploying",
        stream=out,
        is_tty=False,
        clock=clock,
        interval=1.0,
    )

    for i in range(1, 101):
        line.update(i, f"file{i}.esp")
        clock.tick(0.1)
    line.close()

    text = out.getvalue()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines, "printed nothing"
    assert len(lines) < 100, f"one line per file is the spam shape: {len(lines)}"
    assert len(lines) >= 2, f"so few lines the cadence never fired: {len(lines)}"


def test_progress_line_is_silent_when_disabled():
    """--json must see no progress at all: it is parsing the output."""
    out = io.StringIO()
    line = ProgressLine(total=3, label="Deploying", stream=out, enabled=False)
    line.update(1, "a.esp")
    line.update(2, "b.esp")
    line.close()
    assert out.getvalue() == ""


def test_progress_line_closes_with_a_final_line():
    """Whatever the cadence, the last state has to land."""
    out = io.StringIO()
    clock = FakeClock()
    line = ProgressLine(
        total=3, label="Deploying", stream=out, is_tty=False, clock=clock, interval=100.0
    )
    for i in range(1, 4):
        line.update(i, f"file{i}.esp")
        clock.tick(0.01)
    line.close()

    text = out.getvalue()
    assert "file3.esp" in text or "3/3" in text, text


def test_progress_line_handles_zero_total():
    """A plan with nothing in it must not divide by zero."""
    out = io.StringIO()
    line = ProgressLine(total=0, label="Deploying", stream=out, is_tty=False)
    line.close()
    assert "Deploying" in out.getvalue() or out.getvalue() == ""


def test_progress_line_never_writes_to_stdout():
    """Progress is diagnostics. It goes to stderr so pipes stay clean."""
    import sys

    line = ProgressLine(total=1, label="Deploying", is_tty=False)
    assert line.stream is sys.stderr
    line.close()


# ---------------------------------------------------------------------------
# The CLI wiring: deploy shows a moving line, --json stays clean
# ---------------------------------------------------------------------------


def _cli_deploy_setup(tmp_path):
    from nexus_collection_dl.state import CollectionState

    mods = tmp_path / "mods"
    (mods / "a").mkdir(parents=True)
    (mods / "a" / "one.esp").write_bytes(b"1")
    (mods / "a" / "two.esp").write_bytes(b"2")
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.save()
    game = tmp_path / "game"
    game.mkdir()
    return mods, game


def test_cli_deploy_shows_the_moving_line(tmp_path):
    """The deploy loop has to show movement, on stderr.

    `capture="fd"` keeps the streams apart, which is the only way to
    assert the real contract: progress on stderr, result on stdout.
    """
    from click.testing import CliRunner

    from nexus_collection_dl import cli as cli_module

    mods, game = _cli_deploy_setup(tmp_path)
    r = CliRunner(capture="fd").invoke(
        cli_module.main, ["deploy", str(mods), "--game-dir", str(game)]
    )
    assert r.exit_code == 0, r.output
    err = r.stderr or ""
    assert "Deploying" in err, f"no progress on stderr: {err!r}"
    assert "one.esp" in err or "two.esp" in err


def test_cli_deploy_json_stdout_stays_parseable(tmp_path):
    """--json: stdout is the envelope and nothing else.

    The design is Console(stderr=True) under --json so a caller can pipe
    stdout straight into a parser. Progress prose on stderr is fine;
    progress prose on stdout would break the parse.
    """
    import json

    from click.testing import CliRunner

    from nexus_collection_dl import cli as cli_module

    mods, game = _cli_deploy_setup(tmp_path)
    r = CliRunner(capture="fd").invoke(
        cli_module.main,
        ["--json", "deploy", str(mods), "--game-dir", str(game)],
    )
    assert r.exit_code == 0, r.output
    assert "Deploying" not in (r.stdout or "")
    assert "Classifying" not in (r.stdout or "")
    json.loads(r.stdout)
