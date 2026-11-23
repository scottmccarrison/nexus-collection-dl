"""Bare `nexus-dl` is the front door.

Three shapes, and the second one is the reason this has tests at all:

1. Interactive and configured: boot the shell over the collections root.
   The shell is the product's default seat now, not a thing you have to
   read the docs to find.
2. Not interactive (a pipe, a script, a CI step): print usage and exit.
   A REPL is not a thing to drop into a script. This must never hang and
   must never try to prompt.
3. Interactive but unconfigured: say so. (The setup wizard boots here in
   the next slice; until then this is a message, not a prompt.)

``--json`` never boots the shell. That output is parsed, and the shell
is interactive or config-only, which is exactly what ``--json`` already
refuses for `shell`, `nxm`, and `handler`.
"""

from __future__ import annotations

from click.testing import CliRunner

from nexus_collection_dl import cli as cli_module
from nexus_collection_dl.config import UserConfig


def _run(args: list[str], *, interactive: bool, monkeypatch):
    """Invoke bare with a chosen interactivity.

    CliRunner swaps sys.stdout for its own capture during invoke, so
    patching isatty on the outer streams does not reach the probe. The
    branching is what matters here; the probe itself is unit-tested
    below against fake streams.
    """
    monkeypatch.setattr(cli_module, "_is_interactive", lambda: interactive)
    return CliRunner().invoke(cli_module.main, args, input="")


def test_interactive_probe_needs_both_streams():
    """A pipe on either side is not a REPL, whatever the other one is."""

    class Fake:
        def __init__(self, tty: bool):
            self._tty = tty

        def isatty(self) -> bool:
            return self._tty

    import sys

    real_stdin, real_stdout = sys.stdin, sys.stdout
    for stdin_tty, stdout_tty, expected in (
        (True, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, False),
    ):
        sys.stdin, sys.stdout = Fake(stdin_tty), Fake(stdout_tty)  # type: ignore[assignment]
        try:
            assert cli_module._is_interactive() is expected, (stdin_tty, stdout_tty)
        finally:
            sys.stdin, sys.stdout = real_stdin, real_stdout


def test_bare_on_a_pipe_prints_usage_and_does_not_hang(tmp_path, monkeypatch):
    """The guard. A script calling `nexus-dl` must get usage, not a REPL.

    This is the failure mode the milestone's caution names: bare
    invocation on a pipe prints usage. It must not prompt, must not
    block, and must not boot the shell.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    UserConfig(collections_root=str(tmp_path / "mods")).save()

    r = _run([], interactive=False, monkeypatch=monkeypatch)
    assert r.exit_code != 0 or "Usage" in r.output, r.output
    assert "Usage" in r.output, r.output
    assert "/help" not in r.output, "the shell booted on a pipe"


def test_bare_with_stdout_redirected_prints_usage(tmp_path, monkeypatch):
    """`nexus-dl > file` is not a REPL either.

    Output going somewhere other than a terminal means this is a script
    or a capture, not a person typing.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    UserConfig(collections_root=str(tmp_path / "mods")).save()

    r = _run([], interactive=False, monkeypatch=monkeypatch)
    assert "Usage" in r.output, r.output
    assert "/help" not in r.output


def test_bare_interactive_and_configured_boots_the_shell(tmp_path, monkeypatch):
    """The point of the whole change: the default seat is the shell."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    root = tmp_path / "mods"
    root.mkdir()
    UserConfig(collections_root=str(root)).save()

    booted: list[tuple] = []
    import nexus_collection_dl.shell as shell_module

    def fake_run_shell(svc, root_dir, console=None):
        booted.append((str(root_dir),))
        return 0

    monkeypatch.setattr(shell_module, "run_shell", fake_run_shell)
    r = _run([], interactive=True, monkeypatch=monkeypatch)
    assert r.exit_code == 0, r.output
    assert booted, "the shell never ran"
    assert booted[0][0] == str(root)


def test_bare_interactive_and_unconfigured_boots_the_wizard(tmp_path, monkeypatch):
    """A fresh install gets the wizard, not a REPL with no root.

    The front door: config missing means setup, and setup is what writes
    the config that makes the shell useful.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    booted: list = []
    import nexus_collection_dl.shell as shell_module

    monkeypatch.setattr(
        shell_module, "run_shell", lambda *a, **k: booted.append(a) or 0
    )
    r = _run([], interactive=True, monkeypatch=monkeypatch)
    assert not booted, "the shell booted with no collections root"
    assert "setup" in r.output.lower(), r.output


def test_json_bare_never_boots_the_shell(tmp_path, monkeypatch):
    """--json is parsed output. A REPL cannot be the answer to it."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    root = tmp_path / "mods"
    root.mkdir()
    UserConfig(collections_root=str(root)).save()

    booted: list = []
    import nexus_collection_dl.shell as shell_module

    monkeypatch.setattr(
        shell_module, "run_shell", lambda *a, **k: booted.append(a) or 0
    )
    r = _run(["--json"], interactive=True, monkeypatch=monkeypatch)
    assert not booted
    assert "Usage" in r.output or "not supported" in r.output.lower()


def test_explicit_shell_command_still_works(tmp_path, monkeypatch):
    """The explicit path must not change. Only the bare form is new."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    root = tmp_path / "mods"
    root.mkdir()

    booted: list = []
    import nexus_collection_dl.shell as shell_module

    def fake_run_shell(svc, root_dir, console=None):
        booted.append(str(root_dir))
        return 0

    monkeypatch.setattr(shell_module, "run_shell", fake_run_shell)
    r = CliRunner().invoke(cli_module.main, ["shell", str(root)])
    assert r.exit_code == 0, r.output
    assert booted == [str(root)]
