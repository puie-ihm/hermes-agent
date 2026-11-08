# Upstream sync: v0.19.0 fork → v0.23.0

Status: **branch prepared, NOT cut over.** Live install untouched.

## Facts (observed 2026-11-08, ~11:45–12:10 ICT)

- Installed: `v0.19.0 (2026.7.20)` + 36 fork commits, HEAD `382bbee748`
  (rollback tag `rollback/pre-v0.23.0-20261108`).
- Upstream latest release: `v0.23.0` (published 2026-11-05, tag `df1774294a`).
  Upstream `main` additionally has ~47k commits past the v0.19.0 merge base
  `3ef6bbd201` — the project commits hundreds of times a day.
- `hermes --version` prints "Up to date" because it compares against
  `origin/main` (our fork), never against upstream. Misleading on forks.

## Scale

This is **not** a version bump; it is an architectural rewrite:

```
agent/                      423 files changed, +129,368 / -80,563
whole merge                 17,375 added / 4,287 modified / 1,011 deleted files
conflicted files            23 (~88 conflict hunks)
```

Upstream introduced the facade + siblings layout (`gateway/run.py` → `run_*.py`,
`hermes_state.py` → `hermes_state_*.py`, `tools/mcp_tool.py` → siblings, etc.),
and moved holographic memory out of core into a community catalog plugin
(`cca83848ec chore(memory): holographic leaves core for its community catalog plugin`).

## Feature gap: fork-only behaviour that upstream does NOT have

Checked by `git grep` against the v0.23.0 tree. None of these exist upstream;
a clean cutover would silently drop them.

| Fork feature | Evidence at v0.23.0 | Files |
|---|---|---|
| `[[silent]]` / `[[react:emoji]]` sentinel parsing | 0 hits | `plugins/platforms/slack/adapter.py` |
| `thread_followup_mode` (agent decides reply/react/silent) | 0 hits | slack adapter + config |
| Jev triage for follow-ups (`agent/jev_triage.py`, 321 lines) | 0 hits | agent/, slack adapter |
| Kanban `restrict_to_own_tasks` (per-profile visibility) | 0 hits | hermes_cli/kanban*, tools/kanban_tools.py |
| Kanban `assignee_allowlist` | 0 hits | hermes_cli/kanban*, tools/kanban_tools.py |
| Kanban `notify_text` (notify without message text) | 0 hits | hermes_cli/kanban*, gateway |
| Kanban notifier redaction / artifact upload gate | absent | gateway, hermes_cli/kanban_db.py |
| Kanban trusted `origin_user` propagation | partial (4 hits vs our 6) | hermes_cli/kanban*, gateway |
| `MessageDeduplicator` 24h TTL + ack-before-work | reimplemented differently (23 hits) | slack adapter |
| opencode-go session header / message-name fix | replace by upstream equivalents | agent/opencode_affinity.py, agent/auxiliary_client.py |
| minimax-oauth auxiliary branch | present upstream (38 hits) — likely obsolete | agent/auxiliary_client.py |
| Per-platform DM allowlist | 0 hits (`PLATFORM_DM_ALLOWED_USERS` absent in both) | gateway/ |
| Holographic memory in core | removed upstream → catalog plugin | plugins/memory/ |
| Holographic scaffolding rejection (`_is_storable_memory_text` / `_strip_leading_author_prefix`, fork cherry-picks `d4797d7115` + `2fa5763d10`) | absent from catalog plugin v0.26.5 | was `plugins/memory/holographic/__init__.py` |

The 36 fork commits are a **feature fork**, not a patch queue: many are labelled
"cherry-pick from <sha>" where that sha never landed on upstream main
(verified: `452a46edf`, `5f8dbdde3` are not ancestors of `upstream/main`).

## Prepared state

- Worktree: `/home/ec2-user/worktrees/hermes-sync`, branch `sync/v0.23.0`
  (created from live `main` @ `382bbee748`, merged `v0.23.0`).
- All 23 conflicts currently resolved to **upstream's** side, so the tree is a
  clean v0.23.0 + our non-conflicting additions. This is the porting base.
- Live install `/home/ec2-user/repositories/hermes-agent` (branch `main`) is
  **unchanged and serving traffic**.

## Port order (each step: port → test → commit)

1. **Provider correctness first** (agent goes offline if wrong): `agent/opencode_affinity.py`,
   `agent/auxiliary_client.py`, `agent/chat_completion_helpers.py`,
   `hermes_cli/config.py` provider keys. Test: `tests/agent/test_opencode_session_affinity.py`,
   `tests/agent/test_auxiliary_opencode_routing.py`, `tests/hermes_cli/test_opencode_go_*`.
2. **Gateway plumbing**: `gateway/platforms/base.py`, `gateway/authz_mixin.py`,
   `gateway/run.py` (now a facade — patch the sibling that production reads),
   `gateway/kanban_watchers.py`, `hermes_cli/profiles.py`.
3. **Slack behaviour**: sentinels, dedup TTL, mention/triage rules,
   `thread_followup_mode`, MPIM session key, socket-mode early ack.
   Test: `tests/gateway/test_session.py`, `tests/plugins/...` slack suites.
4. **Kanban governance**: visibility scoping, assignee allowlist, notifier
   redaction, origin_user, notify_text. Test: `tests/tools/test_kanban_tools.py`,
   `tests/hermes_cli/test_kanban_db.py`, `tests/tools/test_kanban_redaction.py`.
5. **Jev triage feature**: port `agent/jev_triage.py` + wiring (it is fork-only,
   ~321 lines; may need redesign against the new turn loop).
   **DONE** — the module and its 23 tests survived the merge byte-identical; only
   the call site was lost with the `_channel_gate_allows` rewrite. Re-attached as
   `_agent_followup_gate()` (see the commit for why a separate gate, not an
   inline branch). 15 new tests execute the gate.
6. **Holographic memory**: install the catalog plugin, verify the existing
   fact store still loads (176 facts), confirm `fact_store` tool still registers.
   **DONE** — catalog install (`plugin-catalog/holographic.yaml`, live pin
   v0.26.5 / `f75c636a28`) verified in an isolated home: provider loads,
   `fact_store` + `fact_feedback` register, and the existing store reads back
   **177** facts (the card's 176 was a point-in-time count; one fact was added
   2026-11-08). No in-tree `plugins/memory/holographic/` may exist on the
   branch: a bundled dir wins over the user-installed plugin
   (`plugins/memory/__init__.py` precedence) and a partial copy shadows the
   catalog install while failing to import its `store` submodule.
   **Open gap (verified):** the catalog plugin does not carry the fork's
   `_is_storable_memory_text` / `_strip_leading_author_prefix` guards —
   `_auto_extract_facts` stores Slack scaffolding verbatim (feeding it the
   SCAFFOLDED fixture added a fact containing `[Replying to: …]` /
   `[Thread context …]`; the live store already holds several such rows). The
   fork's `tests/plugins/memory/test_holographic_auto_extract.py` was deleted
   here — the module it imports left core, so it could only error at
   collection; upstream v0.23.0 does not carry it either.
7. **Config migration**: `hermes config migrate`, diff the resulting config for
   silent key drops; verify cron jobs (RPL 09:00 BKK, Acceptance, WBRR) still load.

## Cutover (only after the branch is green)

0. **Holographic plugin, per profile home — do this BEFORE the first start.**
   Every profile home configures `memory.provider: holographic` and none ships
   the provider (it left core upstream), so a start on v0.23.0 finds no provider
   until the catalog plugin is installed. `hermes update` / agent init will
   migrate the home it runs under, but NOT the 8 sibling profile homes — install
   it into each one:

   ```
   for p in default tech_agent buyer_ops data_analyst ext_data_analyst \
            affiliate_mgr media_buyer legal_reviewer front_external; do
       hermes -p "$p" plugins install holographic     # idempotent per home
   done
   ```

   All of them share one store (`~/.hermes/memory_store.db`), so the install is
   code-only; there is nothing to migrate. Confirmed absent today:
   `ls ~/.hermes/plugins/holographic` and the same under every profile home.
   Verify after each: `hermes -p <profile> memory status` shows the provider
   loaded and the fact count.

1. Freeze: confirm no long agent run in flight.
2. `git checkout main && git merge --ff-only sync/v0.23.0` in the live tree.
3. Update deps in the live venv (`pip install -e .`), run `hermes config migrate`.
4. `systemctl --user restart hermes-gateway`; verify: `hermes --version`,
   gateway status, Slack socket connected, one DM round-trip.
5. Rollback if any check fails: `git reset --hard rollback/pre-v0.23.0-20261108`,
   reinstall deps, restart. Budget ~3 min of bot downtime per attempt.

## Verification checklist

- [ ] `python -m pytest tests/ -o 'addopts=' -q` green (or documented pre-existing fails)
- [x] sentinel/dedup/triage tests green
- [x] kanban governance tests green
- [x] holographic fact store loads, `fact_store` reachable (isolated home; the
  live-home install is cutover step 0)
- [ ] opencode-go request path works (one live model call)
- [ ] gateway restart + Slack DM round-trip
