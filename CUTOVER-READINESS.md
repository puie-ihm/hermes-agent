# Cutover readiness — sync/v0.23.0

Audited 2026-11-09 (run 3) against `/home/ec2-user/worktrees/hermes-sync` on `sync/v0.23.0`,
live install untouched at `382bbee748`, rollback tag `rollback/pre-v0.23.0-20261108` in place.
The branch moved under this audit — the sibling card `t_4cea71f2` landed the scaffolding-guard
commits (`1b74b02215`, `59174eb9c6`) while the suite ran; each row names the commit its evidence
was taken at, and the full suite + targeted suites ran on a tree whose only diff from HEAD is
`SYNC-PLAN.md` and this file.

Every row below is either GREEN with the command and its output, or NOT GREEN /
BLOCKED with the exact reason. Nothing here was inferred from a previous run's
summary — the config diff, the fact count and the suite results were re-executed
in this run.

## Verdict table

| # | Check | Verdict | Evidence |
|---|---|---|---|
| 1 | Full `pytest tests/` green or documented pre-existing fails | **NOT GREEN** | see §1 — collection aborts on 4 modules in this venv; see the run table for the executed body |
| 2 | Sentinel / dedup / triage suites | **GREEN** | 143 passed in 54.4s — `test_jev_triage`, `test_slack_thread_followup`, `test_slack_mention`, `test_slack_dedup_ttl`, `test_slack_decision_sentinels_e2e`, `test_slack_reply_expected`, `test_slack_ignore_other_user_mentions`, `test_slack_mpim_session_key`, `test_slack_channel_session_scope` |
| 3 | Kanban governance suites | **NOT GREEN (test fixture, behaviour retained)** | 141 passed, 1 skipped, 2 failed — both in `tests/hermes_cli/test_kanban_db.py`, both `RuntimeError: cannot create restart-safe systemd scope for Kanban worker: the claimed task has no current run id`. The behaviour they cover still exists (`hermes_cli/kanban_db_dispatch.py:2902` sets `HERMES_KANBAN_DB`, `:2919` scrubs Hermes-owned `PYTHONPATH`); upstream now requires a claimed run before spawn, so the fixture must claim one. Live-traffic risk: none identified, coverage unverified until the fixture is updated. |
| 4 | Holographic fact store loads the live 177 facts on the **post-migration** config | **GREEN** | throwaway home, live `config.yaml` migrated `_config_version 23 → 52`, catalog plugin installed, provider `available ✓`; `find_provider_dir('holographic')` → `<HERMES_HOME>/plugins/holographic` (the catalog install, not a bundled path); `SELECT COUNT(*) FROM facts` = **177** = general 53 + project 13 + tool 11 + user_pref 100 |
| 5 | Holographic scaffolding guard | **GREEN** | shipped by the sibling card, commits `1b74b02215` + `59174eb9c6` (`scripts/holographic_scaffolding_guard.py`, `scripts/verify_holographic_scaffolding_guard.py`, test); re-run here: `tests/scripts/test_holographic_scaffolding_guard.py` → **20 passed in 1.08s**. The 6-fixture PASS-patched / 4-FAIL-unpatched matrix is claimed by `1b74b02215` and was **not** re-executed in this run. |
| 6 | One live opencode-go model call | **BLOCKED** | provider/model resolution works (`Gateway log: Model context warmed: deepseek-v4.1-flash -> 1000000 tokens (detected)`), but a headless turn cannot complete in a throwaway `HERMES_HOME`: the CLI brokers the turn through the TUI gateway, whose runtime env under `<HERMES_HOME>/installs/<hash>/environments/<id>/venv` auto-provisions with a broken `pydantic` (`ImportError: cannot import name 'ArgsKwargs' from 'pydantic_core._pydantic_core'`, then `ModuleNotFoundError: No module named 'pydantic'`), so the WS upgrade returns 500 → `Gateway connection failed; no local fallback`. Not attempted against the live home (gateway is serving). See §6 for what cutover must do about it. |
| 7 | Gateway restart + Slack DM round-trip | **NOT TESTED BY DESIGN** | requires restarting the live gateway; out of scope for this card and forbidden by its hard rules. This is the one row that can only be closed after approval. |
| 8 | `hermes config migrate` silent key drops | **DONE, one item needs sign-off** | see §8. 10 keys removed, identical set in root and in profile homes; `security.tirith_*` is a capability loss → OPEN ITEM |
| 9 | Cron jobs survive | **GREEN (premise stale)** | 4 real jobs in the live `cron/jobs.json` parse and schedule correctly with v0.23.0 code: QuinStreet `0 13 * * 1` → 2026-11-09T13:00+07:00, Monthly Revenue `0 14 2 * *` → 2026-12-02T14:00+07:00, watchdog `every 20m`, CAPI Omniad (disabled, once). The three jobs the card names — RPL 09:00 BKK, Acceptance, WBRR — do not exist in the live store (also absent pre-migration; "RPL" only appears inside the Monthly Revenue prompt). Nothing to load. |
| 10 | Bundled-shadows-catalog trap | **GREEN on the branch, GATED at cutover** | branch HEAD has no `plugins/memory/holographic/` (checked `git ls-tree -r HEAD plugins/memory/` — only `byterover`, `retaindb`, `__init__.py`, `config_schema.py`, `query_rewrite.py`); live tree still ships the core provider because it is v0.19.0, and the merge deletes it; no `holographic` dir under `~/.hermes/plugins/` or any of the 9 profile homes, so cutover step 0 must install it everywhere |

## 1. Full suite

`python -m pytest tests/ -o 'addopts=' -q` in this venv **collects nothing**: 4 modules
error at import, so pytest aborts (`Interrupted: 4 errors during collection`, 197.6s):

| Module | Cause | Attribution |
|---|---|---|
| `tests/agent/test_ssl_transport_contracts.py` | `ModuleNotFoundError: truststore` | documented venv gap |
| `tests/agent/test_vault_key_storage.py` | `ModuleNotFoundError: keyring` | documented venv gap |
| `tests/hermes_cli/test_vault_key_cli.py` | `ModuleNotFoundError: keyring` | documented venv gap |
| `tests/tools/test_mcp_events.py` | `ImportError: cannot import name 'MCPError'` — venv has **mcp 1.26.0**, v0.23.0 pins **mcp==2.3.0** | venv gap; the same import fails in `/home/ec2-user/worktrees/hermes-baseline` @ `382bbee748`, so it is not a code regression |

The three truststore/keyring modules were unblocked **without touching the live venv**
(`pip install --target /tmp/sync-verify/deps keyring` + `--no-deps truststore`, `PYTHONPATH`
shim, `cryptography` deliberately removed from the shim so the venv's 46.0.7 stays
authoritative): **29 passed in 22.3s**. `pip install -e .` at cutover installs all four.

## 8. Config migration — what it silently changes

Run against copies in a throwaway `HERMES_HOME`, never the live config (`hermes config migrate` exits 0):

**Removed (identical 10-key set in root and in every profile home):**

| Key | Ours | Consequence |
|---|---|---|
| `security.tirith_enabled` / `_path` / `_timeout` / `_fail_open` | `true` / `tirith` / `5` / `true` | **capability loss** — upstream retired the bundled tirith scanner (migration 49→50); the security control disappears, it is not renamed. **OPEN ITEM: operator sign-off (Puie / Ben / Medard)** |
| `model_catalog.ttl_hours` | `24` | renamed to `ttl_minutes` and **not materialised**, so the effective TTL becomes the code default (20 min, was 24 h) — more catalog refreshes, benign |
| `terminal.docker_image` / `_modal_image` / `_singularity_image` / `_daytona_image` | `nikolaik/python-nodejs:python3.11-nodejs20` | falls back to the upstream default `nousresearch/hermes-sandbox:desktop`; the live `terminal.backend` is `local`, so unused today |
| `terminal.vercel_runtime` | `node24` | → upstream default `""` |

**Overwritten with upstream defaults (root config already held the new values; profile homes change):**
`delegation.max_concurrent_children 3→10`, `delegation.max_iterations 50→250`,
`curator.archive_after_days 90→30`, `curator.stale_after_days 30→14`,
`display.background_process_notifications all→concise`, `display.personality kawaii→''`.

**Added:** `connections` into `platform_toolsets.cli`. **Preserved byte-equal:** `slack.thread_followup_mode: agent`,
`slack.strict_mention`, `slack.allow_bots`, `memory.provider: holographic`, `kanban.assignee_allowlist`,
all `kanban.*` governance keys, `providers.opencode-go`, `model.*`, `terminal.backend: local`.
DM allowlists are env vars, not config — untouched.

**Pre-existing warning, benign:** `platform 'cli' references unknown toolset 'messaging'` — v0.23.0
renamed the toolset; nothing is dropped.

## 6. What the model-call block means for cutover

The live venv already has `pydantic 2.13.4`, so this is not a dependency that `pip install -e .`
must add — it is the **TUI gateway's own runtime environment** (`<HERMES_HOME>/installs/.../venv`,
python 3.14) that self-provisions and came up broken in a fresh home. Consequences:

1. After cutover, the first `hermes chat` / gateway start must be allowed to finish provisioning that
   environment (network + ~1 min) before the Slack DM round-trip check — otherwise the first turn
   fails with `Gateway connection failed; no local fallback`.
2. Row 6 must be re-run **against the live home** immediately after the merge, before declaring success.
3. If it still fails live, `hermes doctor --fix` (or deleting `$HERMES_HOME/installs/<hash>` to force a
   clean re-provision) is the repair, not a config rollback.

## Cutover command sequence (operator-approved only)

```bash
# 0. Holographic provider into EVERY home BEFORE the first v0.23.0 start
for p in default tech_agent buyer_ops data_analyst ext_data_analyst \
         affiliate_mgr media_buyer legal_reviewer front_external; do
    hermes -p "$p" plugins install holographic --yes-deps --enable
done
python /home/ec2-user/worktrees/hermes-sync/scripts/holographic_scaffolding_guard.py apply \
    --home ~/.hermes $(for p in tech_agent buyer_ops data_analyst ext_data_analyst \
         affiliate_mgr media_buyer legal_reviewer front_external; do echo --home ~/.hermes/profiles/$p; done)
python /home/ec2-user/worktrees/hermes-sync/scripts/verify_holographic_scaffolding_guard.py --home ~/.hermes

# 1. Freeze: no long agent run in flight; snapshot the configs
tar czf ~/cutover-backup-$(date +%Y%m%d-%H%M).tgz ~/.hermes/config.yaml \
    ~/.hermes/profiles/*/config.yaml ~/.hermes/cron/jobs.json ~/.hermes/memory_store.db*

# 2. Merge
cd /home/ec2-user/repositories/hermes-agent
git checkout main && git merge --ff-only sync/v0.23.0

# 3. Deps + config
/home/ec2-user/repositories/hermes-agent/venv/bin/pip install -e .
hermes config migrate          # then re-check: security.tirith_* is gone by design

# 4. Restart + verify (allow the TUI gateway env to provision on first start)
systemctl --user restart hermes-gateway
hermes --version; hermes gateway status; hermes memory status
#   -> Slack socket connected, provider holographic "available", 177 facts
#   -> one DM round-trip, and one `hermes chat -q` answered by opencode-go

# 5. Post-cutover proof
HERMES_HOME=~/.hermes hermes doctor
```

## Rollback sequence

```bash
cd /home/ec2-user/repositories/hermes-agent
git reset --hard rollback/pre-v0.23.0-20261108
/home/ec2-user/repositories/hermes-agent/venv/bin/pip install -e .
rm -rf ~/.hermes/installs            # drop any half-provisioned v0.23.0 runtime env
systemctl --user restart hermes-gateway
hermes gateway status                # expect the v0.19.0 gateway with Slack connected
```

Budget ~3 min of bot downtime per attempt. The memory store is code-only at cutover (the plugin and
the live store share `~/.hermes/memory_store.db`), so rollback does not touch facts — but any fact
written by the v0.23.0 plugin after cutover stays in the DB.

## Open items requiring an operator decision

1. **`security.tirith_*` removal** — cutover drops the tirith scanner with no upstream replacement.
   Accept, or pin the last version that still bundled it.
2. **Live `tech_agent` home is already at `_config_version: 52`** (mtime 2026-11-08 10:52, tirith keys
   already gone) while every other home is at 23. The live install's own max schema version is 33, so
   this home was migrated in place by v0.23.0 worktree code during an earlier step. Restore from a
   snapshot or accept the drift before cutover.
3. **`test_slack_thread_context_allowlist.py` (3 failures)** — a regression introduced on this branch by
   `cad0aaaf0d`, not an upstream gap: DM-thread and wake-probe tagging over-tag an allowlisted insider
   `[unverified]`, and the MPIM case rejects an allowlisted insider as "unauthorized user in channel G1"
   then raises `TypeError: 'NoneType' object is not subscriptable`. Fix before cutover or accept the
   MPIM admission behaviour change explicitly.
4. **Two stale kanban spawn tests** (§3) and one stale sender-prefix assertion
   (`test_shared_group_sender_prefix.py`, fork emitted `[Alice | Slack user <@U123>]`, branch emits
   `[Gateway actor {"platform":"slack","user_id":"U123","display_name":"Alice","slack_mention":"<@U123>"}]`
   from the signed envelope at `gateway/run_inbound.py:1427`, same four fields). Update the assertions —
   the behaviour is present in a new shape.
5. **`test_slack_stale_guard.py` (5 failures)** — DROPPED-OBSOLETE, delete the tests. The fork guard is
   opt-in (`SLACK_STALE_EVENT_TTL_SECONDS`, unset in live `.env` and `config.yaml`, so live traffic never
   took the branch) and v0.23.0's ingress has no wall-clock event-age drop at all, so the 2026-09-20
   incident (four 67–81s-old messages dropped after a restart) cannot recur.

## What is still unverified (blocks a clean "GREEN" cutover)

- Row 1 cannot be closed in this venv: the suite aborts at collection. It needs the four modules
  installed (truststore, keyring, mcp==2.3.0), i.e. a provisioned venv — the shim proves the body of
  the suite runs, not the whole suite.
- Row 6 (live opencode-go turn) and row 7 (gateway restart + DM round-trip) are post-merge checks.
- One branch-local regression (open item 3) and three stale-assertion files (open item 4) mean the
  branch is **not** green today: **NOT GREEN for cutover** until 3 is fixed and 4 is updated.
