#!/usr/bin/env python3
"""End-to-end check of the holographic conversation-scaffolding guard.

Runs the provider's real `_auto_extract_facts` over six fixtures against a *copy* of the live
fact store and asserts what may be stored:

    scaffolded     '[Replying to: "WMBR 082426"]  [Thread context ...] [Puie] I always ...'
                   -> rejected (0 facts added)
    thread_parent  '[thread parent] [Puie] I always ...'          -> rejected
    new_message    '[New message] [Puie] I always ...'            -> stored, label stripped
    author_prefix  '[Puie] I always ...'                          -> stored, label stripped
    pref           'I always want the CSV as a code block'        -> stored, verbatim
    multimodal     content as a block list (an image attachment)  -> no fact, no exception

Without the guard the four scaffolding fixtures each add a fact whose text still carries the
scaffolding (measured 2026-09-19: 53 of 160 live facts held "[Thread context", 83 held
"[Replying to:", and an ambiguous recall question was answered from an unrelated conversation).

    python scripts/verify_holographic_scaffolding_guard.py --plugin-dir ~/.hermes/plugins/holographic
    python scripts/verify_holographic_scaffolding_guard.py --home ~/.hermes/profiles/tech_agent

The live store is only read (SQLite backup API, WAL included); every fixture writes to its own
temporary copy, which is deleted afterwards.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

PREF = "I always want the CSV as a code block"
FIXTURES = {
    # fixture: (input text, must the text be stored?, the exact text to find when stored)
    "scaffolded": ('[Replying to: "WMBR 082426"]  [Thread context — prior messages in this thread '
                   "(not yet in conversation history):] [Puie] " + PREF, False, None),
    "thread_parent": ("[thread parent] [Puie] " + PREF, False, None),
    "new_message": ("[New message] [Puie] " + PREF, True, PREF),
    "author_prefix": ("[Puie] " + PREF, True, PREF),
    "pref": (PREF, True, PREF),
    # Not scaffolding: a multimodal message. The provider's own isinstance(content, str) check drops it; our
    # guard runs first and must hand a non-str back instead of raising (it did raise before v2).
    "multimodal": ([{"type": "text", "text": PREF}], False, None),
}
DEFAULT_STORE = "~/.hermes/memory_store.db"


def copy_store(source: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def run_fixture(plugin_root: Path, db: Path, text: str) -> list[str]:
    """Run the real extractor once; return the stored texts it added."""
    # Import by location: the plugin dir may be an installed "holographic/" package or a clone whose root is
    # the package itself, and putting the dir on sys.path would shadow the package with its hrr submodule.
    if "holographic" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "holographic", plugin_root / "__init__.py", submodule_search_locations=[str(plugin_root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules["holographic"] = module
        spec.loader.exec_module(module)
    holographic = sys.modules["holographic"]

    def contents() -> dict[int, str]:
        con = sqlite3.connect(db)
        try:
            return dict(con.execute("select fact_id, content from facts"))
        finally:
            con.close()

    before = contents()
    provider = holographic.HolographicMemoryProvider(config={"db_path": str(db), "auto_extract": "true"})
    provider.initialize("verify-scaffolding-guard", hermes_home=str(db.parent), agent_context="primary",
                        platform="slack", chat_id="C_VERIFY")
    if provider._store is None:
        raise RuntimeError(provider._init_error or "provider opened no store")
    provider._auto_extract_facts([{"role": "user", "content": text}], provider._store, author={})
    after = contents()
    return [after[k] for k in after.keys() - before.keys()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verify the holographic conversation-scaffolding guard end to end")
    ap.add_argument("--plugin-dir", help="plugin directory (clone or installed copy)")
    ap.add_argument("--home", help="HERMES_HOME of a profile; plugin at <home>/plugins/holographic")
    ap.add_argument("--store", default=DEFAULT_STORE, help=f"fact store to copy (default {DEFAULT_STORE})")
    args = ap.parse_args(argv)
    root = Path(args.plugin_dir).expanduser() if args.plugin_dir else (
        Path(args.home).expanduser() / "plugins" / "holographic" if args.home else None)
    if root is None:
        ap.error("give --plugin-dir or --home")
    root = root.resolve()
    source = Path(os.path.expandvars(args.store)).expanduser()
    print(f"plugin: {root}")
    print(f"store : {source} (copied per fixture; never written)")
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for name, (text, want_stored, want_text) in FIXTURES.items():
            db = Path(tmp) / f"{name}.db"
            copy_store(source, db)
            added = run_fixture(root, db, text)
            if not want_stored:
                ok = not added
                detail = "no fact added" if ok else f"STORED {added!r}"
            else:
                ok = bool(added) and all(a == want_text for a in added)
                detail = f"stored {added!r}" if ok else f"expected {want_text!r}, got {added!r}"
            failures += 0 if ok else 1
            print(f"{'PASS' if ok else 'FAIL'}  {name:14s} {detail}")
    print(f"{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
