#!/usr/bin/env python3
"""IHM local patch: keep holographic auto-extraction from storing conversation scaffolding.

WHY THIS EXISTS
---------------
Upstream `cca83848ec` moved the holographic memory provider out of core into the community
catalog plugin `itpartypattaya/hermes-plugin-holographic`. The fork's in-tree copy carried two
cherry-picked fixes that the catalog plugin does not:

    d4797d7115  stop auto-extraction storing conversation scaffolding
    2fa5763d10  reject [thread parent]/[New message] scaffolding

Without them, `_auto_extract_facts` stores whatever the Slack adapter prepended to a person's
message. Measured on the live store 2026-09-19: 53 of 160 facts held "[Thread context", 83 held
"[Replying to:", and an ambiguous recall question was answered from an unrelated conversation.
`auto_extract: true` on all nine profile homes, so every session end keeps adding rows.

The guard belongs upstream (the plugin is maintained by itpartypattaya), and the code below is
written so it can be contributed there verbatim: `diff` prints a unified diff that applies to a
clean clone with `git apply`. Until that PR lands we own a local patch, because the alternative
- putting the provider back in-tree - loses to bundled-provider precedence and shadows the
catalog install (see plugins/memory/__init__.py).

USAGE
-----
    # dry run: show what would change
    python scripts/holographic_scaffolding_guard.py diff --plugin-dir ~/.hermes/plugins/holographic

    # the patch as it should be contributed upstream (sentinels removed, applies to a clean clone)
    python scripts/holographic_scaffolding_guard.py diff --pr --plugin-dir <clean clone>

    # apply to the installed plugin of one or more profile homes (idempotent)
    python scripts/holographic_scaffolding_guard.py apply --home ~/.hermes --home ~/.hermes/profiles/tech_agent

    # is it still applied? exit 1 if a `hermes plugins update` wiped it
    python scripts/holographic_scaffolding_guard.py check --home ~/.hermes ...

    # remove the guard again, restoring the catalog file byte-for-byte, before an update
    python scripts/holographic_scaffolding_guard.py restore --home ~/.hermes ...

BEFORE A `hermes plugins update`, run `restore`: a locally modified tracked file makes the plugin's
own git pull refuse ("local changes would be overwritten by merge"), and the plugin's staging step
copies those local changes forward. `restore` → update → `apply` is the clean path. Re-apply after
the first install of every profile home at cutover (SYNC-PLAN.md, cutover step 0).
Exit codes: 0 = done (applied, already applied, or restored), 1 = not patched (check), 2 = refused
(upstream code changed: re-derive the anchors, do not force — or an older version of this guard is
in the file, which needs a fresh plugin clone).
"""
from __future__ import annotations

import argparse
import difflib
import py_compile
import re
import sys
import tempfile
from pathlib import Path

GUARD_VERSION = 2
GUARD_ID = f"IHM-GUARD: holographic-scaffolding-guard v{GUARD_VERSION}"
SENTINEL = "# " + GUARD_ID
# Any guard already in the file, whatever version: an older block must not be mistaken for "patched"
# (the anchors it consumed no longer match, so the next apply would refuse with a confusing message).
_ANY_GUARD = re.compile(r"#\s*IHM-GUARD: holographic-scaffolding-guard v(\d+)")
# The catalog versions whose source these anchors were verified against, byte-exactly: 0.26.5
# (f75c636a28, the live pin) and 0.26.4 (6fe7d424cd, the pin in the tree's plugin-catalog yaml, so
# the version `hermes plugins install holographic` picks up at cutover). Another version is not
# refused outright: anchors are matched byte-exactly, so an unrelated version fails the match and is
# reported instead of silently half-patching.
KNOWN_VERSIONS = ("0.26.4", "0.26.5")

# --- screen.py: the rule itself, next to the other extraction rules -----------------------------
SCREEN_ANCHOR = '''def _extract_floor(text: str) -> int:
    return _EXTRACT_MIN_CHARS_CJK if _RE_CJK.search(text) else _EXTRACT_MIN_CHARS
'''

SCREEN_ADDITION = '''
# Conversation scaffolding the adapters prepend to a person's message: the Slack adapter's thread
# context, reply quote and thread-parent label, the gateway's "[New message]", and a caller-added
# "[Puie] "-style author label. None of it is a fact, and a stored copy makes recall answer
# thread-scoped questions with unrelated conversations (measured 2026-09-19 on a live store: 53 of
# 160 facts held "[Thread context", 83 held "[Replying to:", and a "what did we conclude earlier"
# question was answered with the previous morning's CAPI work because those rows matched). The
# markers are the writers' own strings, from plugins/platforms/slack/adapter.py ("[thread parent] ",
# "[Thread context ...]", '[Replying to: "..."]', "[New Assistant Thread]") and gateway/run.py
# ("[New message]"); matched case-insensitively.
SCAFFOLDING_MARKERS = (
    "[Thread context",
    "[Replying to:",
    "[thread parent]",
    "[New message]",
    "[New Assistant Thread",
)
# A leading author label — "[Puie] ", "[Agung] ", repeated ones included. At most 25 characters
# after "[X", so a bracketed phrase such as "[Thread context ...]" never matches.
_LEADING_AUTHOR_PREFIX = re.compile(r"^\\s*\\[[A-Z][A-Za-z0-9 .'\\-]{1,24}\\]\\s*")


def strip_leading_author_prefix(text):
    """Drop caller-added ``[Puie]``-style author labels (repeated ones included).

    A ``content`` that is not a str - a multimodal block list, which the provider's own
    ``isinstance(content, str)`` check below is there for - is handed back untouched: this guard must
    reject scaffolding, never raise on a message shape it does not model.
    """
    previous = None
    while isinstance(text, str) and previous != text:
        previous = text
        text = _LEADING_AUTHOR_PREFIX.sub("", text, count=1)
    return text.strip() if isinstance(text, str) else text


def scaffolding_in(text: str) -> str | None:
    """The conversation-scaffolding marker *text* carries, or None.

    Scaffolding is rejected, not parsed: the thread-context block is multi-line and the person's
    message follows it, so recovering their part from the stored string is not possible. A false
    negative — one lost preference — is much cheaper than the false positive measured above.
    """
    if not isinstance(text, str) or not text:
        return None
    folded = text.casefold()
    for marker in SCAFFOLDING_MARKERS:
        if marker.casefold() in folded:
            return marker
    return None
'''

# --- __init__.py: import the rule, apply it before the sentence loop ----------------------------
INIT_IMPORT_ANCHOR = """from .screen import (_EXTRACT_MAX_CHARS, _RE_QUESTION, _RE_SENTENCE, _extract_floor, _fact_problem, _not_a_fact,
                     _recall_problem, _role_prompt, _secret_in, _sendable)
"""

INIT_GUARD_ANCHOR = """            if not isinstance(content, str) or len(content) < _extract_floor(content) or _role_prompt(content):
                continue  # a prompt pasted whole: its sentences pass one by one (Codex review), none is the user's
"""


def _with_sentinel(prefix: str, body: str) -> str:
    """*prefix* kept, *body* inserted after it, the sentinel line marking the file as patched."""
    return prefix + SENTINEL + "\n" + body


SCREEN_BLOCK = _with_sentinel(SCREEN_ANCHOR, SCREEN_ADDITION)
INIT_IMPORT_BLOCK = _with_sentinel(INIT_IMPORT_ANCHOR, "from .screen import scaffolding_in, strip_leading_author_prefix\n")
INIT_GUARD_BLOCK = "            " + SENTINEL + "\n" + """            # Author labels are dropped, then the message is refused if it still carries scaffolding: a stored
            # "[Replying to: ...] [Thread context ...] [Puie] I always ..." row is not a fact and matched unrelated
            # recall questions (see screen.SCAFFOLDING_MARKERS). The guard sits before the sentence loop, because
            # the thread-context block spans sentences: a per-sentence screen would keep the person's own part.
            content = strip_leading_author_prefix(content)
            marker = scaffolding_in(content)
            if marker:
                logger.debug("holographic: not auto-extracting a message carrying %r scaffolding", marker)
                continue
""" + INIT_GUARD_ANCHOR

TARGETS = {
    "screen.py": [(SCREEN_ANCHOR, SCREEN_BLOCK)],
    "__init__.py": [(INIT_IMPORT_ANCHOR, INIT_IMPORT_BLOCK), (INIT_GUARD_ANCHOR, INIT_GUARD_BLOCK)],
}


def plugin_dirs(args) -> list[Path]:
    dirs = [Path(p).expanduser() for p in (args.plugin_dir or [])]
    for home in args.home or []:
        dirs.append(Path(home).expanduser() / "plugins" / "holographic")
    return dirs


def read_version(root: Path) -> str:
    try:
        text = (root / "plugin.yaml").read_text(encoding="utf-8")
    except OSError:
        return "?"
    match = re.search(r"^version:\s*[\"']?([^\"'\s]+)", text, re.MULTILINE)
    return match.group(1) if match else "?"


def patched_text(text: str, anchors: list[tuple[str, str]]) -> tuple[str, str]:
    """(new text, state) where state is 'patched' | 'already' | 'stale:vN' | 'nomatch:<anchor head> (found N)'."""
    older = [v for v in _ANY_GUARD.findall(text) if v != str(GUARD_VERSION)]
    if older:
        return text, "stale:v" + ",".join(sorted(set(older)))
    if SENTINEL in text:
        return text, "already"
    out = text
    for anchor, replacement in anchors:
        found = out.count(anchor)
        if found != 1:
            return text, f"nomatch:{anchor.strip().splitlines()[0][:70]} (found {found})"
        out = out.replace(anchor, replacement, 1)
    return out, "patched"


def unpatched_text(text: str, anchors: list[tuple[str, str]]) -> tuple[str, str]:
    """Inverse of :func:`patched_text`: 'clean' (nothing to do) | 'restored' | 'stale:vN' | 'nomatch:…'.

    The guard's own blocks are known byte-for-byte, so reversing them restores the catalog file
    exactly — which is what `hermes plugins update` needs: a locally modified tracked file makes the
    plugin's git pull refuse ("local changes would be overwritten"). Restore, update, apply.
    """
    older = [v for v in _ANY_GUARD.findall(text) if v != str(GUARD_VERSION)]
    if older:
        return text, "stale:v" + ",".join(sorted(set(older)))
    if SENTINEL not in text:
        return text, "clean"
    out = text
    for anchor, replacement in reversed(anchors):
        found = out.count(replacement)
        if found != 1:
            return text, f"nomatch:{replacement.strip().splitlines()[0][:70]} (found {found})"
        out = out.replace(replacement, anchor, 1)
    return out, "restored"


def _drop_sentinels(text: str) -> str:
    """The patch as it should be contributed: the same code without our bookkeeping markers.

    The ``# IHM-GUARD: …`` lines only tell this patcher a file is already patched; upstream has no
    use for them, and a PR carrying them would invite the question of who IHM is.
    """
    return re.sub(r"^[ \t]*#\s*IHM-GUARD: holographic-scaffolding-guard v\d+\n", "", text, flags=re.MULTILINE)


def report_diff(name: str, old: str, new: str) -> None:
    sys.stdout.writelines(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                               fromfile=f"a/{name}", tofile=f"b/{name}"))


def process(root: Path, mode: str, pr: bool = False) -> int:
    if not root.is_dir():
        print(f"FAIL  {root}: no such plugin directory")
        return 2
    version = read_version(root)
    if version not in KNOWN_VERSIONS:
        print(f"WARN  {root}: version {version}, anchors taken from {', '.join(KNOWN_VERSIONS)}; matching anyway")
    transform = unpatched_text if mode == "restore" else patched_text
    done_state, idle_state, verb = ("restored", "clean", "RESTORED") if mode == "restore" else ("patched", "already", "PATCHED")
    states, changed = [], []
    for name, anchors in TARGETS.items():
        path = root / name
        try:
            old = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"FAIL  {path}: {exc}")
            return 2
        new, state = transform(old, anchors)
        if state.startswith("stale:"):
            print(f"FAIL  {path}: carries {state[6:]} of this guard, current is v{GUARD_VERSION}. Reinstall the "
                  f"plugin (`hermes plugins update holographic` re-clones it) and apply again.")
            return 2
        if state.startswith("nomatch"):
            print(f"FAIL  {path}: anchor not found — {state}. Upstream changed this code; re-derive the patch "
                  f"instead of forcing it.")
            return 2
        states.append(f"{name}={state}")
        if state == done_state:
            changed.append((path, old, new))
    if mode == "diff":
        for path, old, new in changed:
            report_diff(path.name, old, _drop_sentinels(new) if pr else new)
        if not changed:
            print(f"ALREADY {root}: {GUARD_ID}")
        return 0
    if mode == "check":
        ok = all(s.endswith("=already") for s in states)
        print(f"{'OK   ' if ok else 'MISS '}{root} (v{version}): {', '.join(states)}")
        return 0 if ok else 1
    for path, _old, new in changed:
        path.write_text(new, encoding="utf-8")
    for path in (root / n for n in TARGETS):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                py_compile.compile(str(path), cfile=str(Path(tmp) / "check.pyc"), doraise=True)
            except py_compile.PyCompileError as exc:
                print(f"FAIL  {path}: {'patched' if mode == 'apply' else 'restored'} file does not compile — {exc.msg.strip()}")
                return 2
    print(f"{verb if changed else 'ALREADY' if mode == 'apply' else 'CLEAN'} {root} (v{version}): {', '.join(states)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Apply/check the IHM conversation-scaffolding guard on the holographic plugin")
    ap.add_argument("mode", choices=("apply", "check", "diff", "restore"))
    ap.add_argument("--home", action="append", default=[], help="HERMES_HOME of a profile; plugin at <home>/plugins/holographic")
    ap.add_argument("--plugin-dir", action="append", default=[], help="plugin directory directly (a clone or a scratch copy)")
    ap.add_argument("--pr", action="store_true",
                    help="diff: omit the IHM-GUARD sentinel lines, i.e. the patch to contribute upstream")
    args = ap.parse_args(argv)
    targets = plugin_dirs(args)
    if not targets:
        ap.error("give at least one --home or --plugin-dir")
    worst = 0
    for root in targets:
        worst = max(worst, process(root, args.mode, args.pr))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
