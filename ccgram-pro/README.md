# ccgram-pro

Workflow layer on top of [ccgram](../). Adds:

- predefined project picker, model + reasoning selector
- batched text + voice input (Send when ready)
- input/output transforms (preamble injection, output silencing + summarization)
- plan/execute mode flow with approve/edit gating
- React + Monaco diff viewer with branch + PR management
- `/pr-fix` review loop against Cursor-bot feedback

Installed as a sibling package that hooks into ccgram via two entry-point
groups (`ccgram.extensions`, `ccgram.miniapp_factory`). The only upstream
change is two small hook dispatch sites in `src/ccgram/bootstrap.py` and
`src/ccgram/main.py`.

## Compatibility

Requires Python 3.14+ (matches ccgram) and a build of ccgram that ships the
two entry-point hooks (`bootstrap.dispatch_extensions` and
`main._resolve_miniapp_factory`). Verify with `ccgram-pro doctor`.

Phase 7 (`/pr-fix` PR review loop) additionally needs the GitHub CLI
(`gh auth login`) on `$PATH`. Earlier phases do not.

## Install

From the repo root:

```bash
uv pip install -e ccgram-pro
ccgram-pro doctor
```

Then run ccgram normally — the layer activates via entry-point dispatch.

## Configuration

State lives under `<CCGRAM_DIR>/layer/` (default `~/.ccgram/layer/`). When
running multiple ccgram instances (`CCGRAM_GROUP_ID` or
`CCGRAM_INSTANCE_NAME` set), the layer further namespaces by group/instance
so concurrent bots don't share state.

### `projects.toml`

Predefined project list shown by the `/project` picker:

```toml
[[project]]
path = "/root/projects/humanprogram/backend"
label = "HP Backend"
default_model = "opus"               # Claude CLI alias: opus | sonnet | haiku
default_reasoning = "extra-high"     # layer label: extra-high | high | medium | low
default_preamble = """
Remember our backend conventions. Run pnpm typecheck after edits.
"""

[[project]]
path = "/root/projects/personal/ccgram"
label = "ccgram (this repo)"
```

### `settings.toml`

Global layer defaults — every key is optional and falls back to the
baked-in value:

```toml
[defaults]
silent_mode = true
batch_mode = true
plan_mode_on_new_session = true
preamble = """
Remember to follow best practices and our current project rules.
Production-ready implementation only — no quick changes or hacks.
"""

[voice]
transcription_note = "Voice may have transcription errors — interpret intent."
flush_grace_seconds = 30

[snapshots]
prune_after_days = 7

[share_tokens]
default_ttl_seconds = 259200  # 3 days
```

## Status

Phase 0 (foundation) shipped. Subsequent phases land independently — each
leaves the bot fully working. See `ccgram-pro doctor` for what's wired up
in the current install.
