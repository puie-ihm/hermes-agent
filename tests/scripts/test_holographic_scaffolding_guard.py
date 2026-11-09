"""The local patch that keeps holographic auto-extraction out of conversation scaffolding.

Upstream moved the holographic provider into the community catalog plugin (`cca83848ec`) and the
plugin does not carry the fork's two cherry-picks (`d4797d7115`, `2fa5763d10`). The guard lives in
`scripts/holographic_scaffolding_guard.py` and is re-applied to every installed copy, so what is
tested here is the patcher's own contract: it patches, it is idempotent, it refuses instead of
guessing when upstream's source moves, and the rule it inserts behaves.

The end-to-end run against a real plugin install and a copy of the live store is
`scripts/verify_holographic_scaffolding_guard.py` (needs the catalog plugin installed; not a unit
test).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import scripts.holographic_scaffolding_guard as guard

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "holographic_scaffolding_guard.py"


def build_plugin(root: Path) -> Path:
    """A minimal tree carrying the two anchors the patch matches, shaped like the plugin's files."""
    plugin = root / "holographic"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text('name: holographic\nversion: "0.26.5"\n', encoding="utf-8")
    (plugin / "screen.py").write_text(
        "import re\n\n\n" + guard.SCREEN_ANCHOR + "\n# What a fact must not be.\n_FACT_MAX_CHARS = 1000\n",
        encoding="utf-8")
    (plugin / "__init__.py").write_text(
        "from . import holographic as hrr\n" + guard.INIT_IMPORT_ANCHOR + "\n"
        "def _auto_extract_facts(messages):\n"
        "    for msg in messages:\n"
        "        content = msg.get('content', '')\n"
        "        if content is None:\n"
        "            continue\n"
        + textwrap.indent(guard.INIT_GUARD_ANCHOR, "") +
        "        yield content\n",
        encoding="utf-8")
    return plugin


def test_apply_is_idempotent_and_check_flips(tmp_path, capsys):
    plugin = build_plugin(tmp_path)
    assert guard.main(["check", "--plugin-dir", str(plugin)]) == 1
    assert guard.main(["apply", "--plugin-dir", str(plugin)]) == 0
    assert guard.main(["check", "--plugin-dir", str(plugin)]) == 0
    assert guard.main(["apply", "--plugin-dir", str(plugin)]) == 0
    out = capsys.readouterr().out
    assert "ALREADY" in out.splitlines()[-1]
    screen = (plugin / "screen.py").read_text(encoding="utf-8")
    init = (plugin / "__init__.py").read_text(encoding="utf-8")
    assert screen.count(guard.GUARD_ID) == 1
    assert init.count(guard.GUARD_ID) == 2  # import block + extraction guard
    assert screen.count("def strip_leading_author_prefix") == 1


def test_anchor_mismatch_is_refused_not_forced(tmp_path, capsys):
    plugin = build_plugin(tmp_path)
    screen = plugin / "screen.py"
    original = screen.read_text(encoding="utf-8").replace("_EXTRACT_MIN_CHARS_CJK", "_EXTRACT_MIN_CJK")
    screen.write_text(original, encoding="utf-8")
    assert guard.main(["apply", "--plugin-dir", str(plugin)]) == 2
    out = capsys.readouterr().out
    assert "anchor not found" in out
    assert screen.read_text(encoding="utf-8") == original  # nothing half-written
    assert guard.GUARD_ID not in (plugin / "__init__.py").read_text(encoding="utf-8")


def test_diff_writes_nothing(tmp_path, capsys):
    plugin = build_plugin(tmp_path)
    before = (plugin / "screen.py").read_text(encoding="utf-8")
    assert guard.main(["diff", "--plugin-dir", str(plugin)]) == 0
    out = capsys.readouterr().out
    assert "--- a/screen.py" in out and "+++ b/screen.py" in out
    assert (plugin / "screen.py").read_text(encoding="utf-8") == before


def test_pr_diff_omits_our_sentinels(tmp_path, capsys):
    """The patch we would contribute: same code, none of the markers only this patcher cares about."""
    plugin = build_plugin(tmp_path)
    assert guard.main(["diff", "--pr", "--plugin-dir", str(plugin)]) == 0
    out = capsys.readouterr().out
    assert "IHM-GUARD" not in out
    assert "+def scaffolding_in" in out and "+            content = strip_leading_author_prefix(content)" in out


def test_unknown_version_warns_but_still_matches_on_anchors(tmp_path, capsys):
    plugin = build_plugin(tmp_path)
    (plugin / "plugin.yaml").write_text('name: holographic\nversion: "9.9.9"\n', encoding="utf-8")
    assert guard.main(["apply", "--plugin-dir", str(plugin)]) == 0
    assert "WARN" in capsys.readouterr().out


def _rule():
    """The inserted screen.py code, executed on its own (it needs nothing but ``re``)."""
    ns: dict = {}
    exec("import re\n" + guard.SCREEN_ADDITION, ns)  # our own constant, not untrusted input
    return ns["scaffolding_in"], ns["strip_leading_author_prefix"]


def test_marker_set_covers_both_fork_cherry_picks():
    """Fidelity to d4797d7115 + 2fa5763d10: every marker the fork matched is still matched.

    The two cherry-picks touched this tuple twice (2fa5763d10 added the last three), so a tidy-up
    that drops one silently re-opens the regression for that writer's scaffolding.
    """
    ns: dict = {}
    exec("import re\n" + guard.SCREEN_ADDITION, ns)
    markers = {m.casefold() for m in ns["SCAFFOLDING_MARKERS"]}
    for fork_marker in ("[thread context", "[replying to:", "new assistant thread",
                        "[thread parent]", "[new message]"):
        assert any(fork_marker in m for m in markers), fork_marker


def test_older_guard_version_is_refused_not_reported_ok(tmp_path, capsys):
    """A v1 block already consumed the anchors; reporting "already patched" would hide a stale guard."""
    plugin = build_plugin(tmp_path)
    screen = plugin / "screen.py"
    screen.write_text(screen.read_text(encoding="utf-8").replace(
        guard.SCREEN_ANCHOR, guard.SCREEN_ANCHOR + "# IHM-GUARD: holographic-scaffolding-guard v1\n"), encoding="utf-8")
    assert guard.main(["apply", "--plugin-dir", str(plugin)]) == 2
    out = capsys.readouterr().out
    assert "v1 of this guard" in out and "re-clones" in out


def test_restore_returns_the_catalog_file_byte_for_byte(tmp_path, capsys):
    """`restore` before an update: the plugin's own git pull refuses a modified tracked file."""
    plugin = build_plugin(tmp_path)
    original = {p.name: p.read_text(encoding="utf-8") for p in (plugin / "screen.py", plugin / "__init__.py")}
    assert guard.main(["restore", "--plugin-dir", str(plugin)]) == 0  # clean tree: nothing to do
    assert "CLEAN" in capsys.readouterr().out
    assert guard.main(["apply", "--plugin-dir", str(plugin)]) == 0
    assert guard.main(["restore", "--plugin-dir", str(plugin)]) == 0
    assert "RESTORED" in capsys.readouterr().out
    for name, text in original.items():
        assert (plugin / name).read_text(encoding="utf-8") == text, name
    assert guard.main(["check", "--plugin-dir", str(plugin)]) == 1


def test_non_str_content_is_handed_back_untouched():
    """A multimodal ``content`` is a list; the provider's own isinstance check handles it, and so must we."""
    _, strip = _rule()
    blocks = [{"type": "text", "text": "I always want the CSV as a code block"}]
    assert strip(blocks) == blocks
    assert strip(None) is None


@pytest.mark.parametrize("text", [
    '[Replying to: "WMBR 082426"]  [Thread context — prior messages in this thread (not yet in '
    "conversation history):] [Puie] I always want the CSV as a code block",
    '[Replying to: "x"] I prefer charts without pie',
    '[Replying to: "New Assistant Thread"] I always use EST',
    "[thread parent] [Puie] I always want the CSV as a code block",
    "[thread context — prior messages] some earlier preference",
])
def test_scaffolding_is_detected(text):
    scaffolding_in, _ = _rule()
    assert scaffolding_in(text)


@pytest.mark.parametrize("text", [
    "I always want the CSV as a code block",
    "[Puie] I always want the CSV as a code block",  # the label is dropped, the sentence is kept
    "",
    None,
])
def test_plain_statements_carry_no_scaffolding(text):
    scaffolding_in, _ = _rule()
    assert scaffolding_in(text) is None


def test_author_labels_are_stripped_repeatedly():
    _, strip = _rule()
    assert strip("[Puie] [Agung] I prefer EST") == "I prefer EST"
    assert strip("I prefer EST") == "I prefer EST"
    # A bracketed phrase longer than a name is not a label: 25 characters at most after "[X"
    assert strip("[Thread context — prior messages] x").startswith("[Thread context")


def test_cli_runs_from_a_shell(tmp_path):
    plugin = build_plugin(tmp_path)
    done = subprocess.run([sys.executable, str(SCRIPT), "apply", "--plugin-dir", str(plugin)],
                          capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "PATCHED" in done.stdout
