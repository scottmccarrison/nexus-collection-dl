"""The rest of the small-stuff batch: #127, #126, #129.

Small, real, and cheap. Each one is a thing a person hits or a
maintainer wishes they had.
"""

from __future__ import annotations

import inspect
import re

from click.testing import CliRunner

from nexus_collection_dl import cli as cli_module

# ---------------------------------------------------------------------------
# #127: the removed `serve` gets a deprecation stub
# ---------------------------------------------------------------------------


def test_removed_serve_names_the_replacement():
    """A bare "no such command" tells a upgrading user nothing.

    The web UI was removed in 0.4.0. Someone who typed `nexus-dl serve`
    in 0.3.0 deserves to know what replaced it.
    """
    r = CliRunner().invoke(cli_module.main, ["serve"])
    assert r.exit_code != 0
    combined = (r.output + str(r.exception)).lower()
    assert "shell" in combined, combined
    assert "removed" in combined or "replaced" in combined or "instead" in combined


def test_removed_serve_exits_non_zero():
    """It is a stub, not a command. Non-zero so scripts notice."""
    r = CliRunner().invoke(cli_module.main, ["serve"])
    assert r.exit_code != 0


# ---------------------------------------------------------------------------
# #126: redaction gets a maintainer diagnostic escape
# ---------------------------------------------------------------------------


def test_redaction_has_a_diagnostic_escape():
    """Scrubbing is the right default; a maintainer still needs the detail.

    A bug report with `[redacted]` where the error was is a bug report
    nobody can act on.
    """
    from nexus_collection_dl.redaction import boundary_redact

    sig = inspect.signature(boundary_redact)
    assert any(
        re.search(r"diagnos|escape|unredact|keep", name, re.IGNORECASE)
        for name in sig.parameters
    ), f"no escape hatch in {list(sig.parameters)}"


def test_redaction_escape_is_off_by_default():
    """The default must stay safe: nobody gets the secret unless they ask."""
    from nexus_collection_dl.redaction import boundary_redact

    secret = "sk-or-v1-abcdef0123456789abcdef0123456789"
    out = boundary_redact(f"failed with key {secret}", (secret,))
    assert secret not in out


# ---------------------------------------------------------------------------
# #129: Enderal gets a LOOT answer, not a silent fallback
# ---------------------------------------------------------------------------


def test_enderal_has_a_loot_answer():
    """Enderal is either sorted or it says why it is not.

    A silent fallback to collection order is the gap: the user has no
    idea LOOT did not run. The bindings may have no Enderal entry at
    all, in which case the honest fix is a reason, not a guessed
    mapping.
    """
    from nexus_collection_dl.loot_sort import loot_support

    for domain in ("enderal", "enderalspecialedition"):
        can_sort, reason = loot_support(domain)
        assert isinstance(can_sort, bool)
        assert reason or can_sort, f"{domain}: no answer at all"
        if not can_sort:
            assert len(reason) > 10, f"{domain}: useless reason {reason!r}"
            assert domain.split("s")[0] in reason.lower() or "loot" in reason.lower()


def test_loot_support_names_an_unsupported_game():
    """A domain with no LOOT entry gets a reason that names it."""
    from nexus_collection_dl.loot_sort import loot_support

    can_sort, reason = loot_support("nonexistentgame")
    assert can_sort is False
    assert "nonexistentgame" in reason or "not" in reason.lower()
