# Issue #19 — Environment variables silently fail to override YAML config values

- **Ticket**: [osprey-dcs/dp-python-lib#19](https://github.com/osprey-dcs/dp-python-lib/issues/19)
- **Surfaced by**: the configuration recipe for #15 (`doc/cookbook/connecting.md`), which documents the bug
  in a ⚠️ callout with a workaround.
- **Status**: written 2026-09-24 against dp-python-lib `ff3cc3e` and rechecked at `f856b94` (main, after PR #60, which touched none
  of the code or docs cited here), pydantic-settings 2.14.2.  The ticket is
  AI-drafted; its diagnosis holds, and an earlier triage comment on the issue (2026-08-11) confirmed it and
  deferred the fix to after the release.  1.16.0 has since shipped carrying the bug unchanged: `git diff
  rel-1.16.0 main -- src/dp_python_lib/config` is empty.  Five things the ticket and that comment missed are
  recorded under [Background](#background--triage-findings) (T4–T8) and folded into
  [Implementation tasks](#implementation-tasks).  Open questions Q1 and Q2 were resolved 2026-09-24, as
  recommended; Q3 is left to the release cut.

## Overview

`MldpConfig.from_yaml()` passes the file's values as constructor keyword arguments.  pydantic-settings ranks
init kwargs **above** environment variables, so any key written in the YAML file cannot be overridden by its
`MLDP_*` variable, and nothing warns that the variable was ignored.  That reverses levels 2 and 3 of the
priority documented in `README.md`, `CLAUDE.md`, the `load_config()` docstring, and the cookbook.  Every
`MldpClient()` that finds a config file goes through this path.

The fix gives the YAML values their own settings source, ranked below env vars and above defaults, so the
documented order holds with no special cases.  The code change is small.  Most of the work is tests: the
one existing test of this path is mocked at exactly the layer where the bug lives.

**Worth doing.**  The documented contract is broken, the failure is silent, and it hits the one deployment
pattern env vars exist for: build one image, then override its settings per environment.  The fix is contained
to two small modules, and the prototype below shows it works.

## Background / triage findings

- **T1 — The diagnosis is correct; line references have drifted.**  `from_yaml()` is now
  `config.py:67-114` and the kwargs call is `config.py:107` (the ticket says 82-109 / 109; `aafe6b5` added the
  empty-file handling since).  `load_config()` calls it at `loader.py:107`.  Reproduced independently:

  | YAML `ingestion:` | env | resolves to | correct? |
  |---|---|---|---|
  | `host: localhost` | `MLDP_INGESTION_HOST=prod.example.com` | `localhost` | ❌ |
  | (no `port`) | `MLDP_INGESTION_PORT=443` | `443` | ✅ |

- **T2 — The ticket's suggested fix leaves out the hard part.**  Its snippet returns
  `YamlSource(settings_cls)` from `settings_customise_sources`, but that hook is a classmethod with no access
  to the file path, and `from_yaml(path)` is how the path arrives.  pydantic-settings' built-in
  `YamlConfigSettingsSource` cannot be used as-is either: it maps top-level YAML keys to fields, while our
  YAML is nested (`ingestion: {host: ...}`) and our fields are flat (`ingestion_host`).  The plumbing is
  settled in [D1](#design-decisions) and was prototyped against pydantic-settings 2.14.2:

  | case | result |
  |---|---|
  | key in YAML + env var set | env wins ✅ |
  | key in YAML, no env var | YAML beats default ✅ |
  | key absent from YAML | default, or env if set ✅ |
  | plain `MldpConfig()` (no `from_yaml`) | no YAML leaks in ✅ |
  | explicit `MldpConfig(ingestion_host=...)` + env | explicit wins ✅ |
  | lower-case `mldp_ingestion_host` | honored (case-insensitive) ✅ |

- **T3 — `load_config(config_object=...)` rebuilds the object from init kwargs** (`loader.py:86-99`), so env
  vars cannot touch it.  That is **correct**, because explicit parameters are level 1.  But the comment
  (`"...that will pick up environment variables"`) and the INFO log (`"...with environment variable
  overrides"`) both say the opposite.  The triage comment already flagged this.  One addition: that
  rebuild copies all nine fields explicitly, so it produces the same values as returning the object itself
  (see [D3](#design-decisions)).

- **T4 — `MldpClient(config=...)` never reaches `load_config()`.**  `mldp_client.py:47-52` calls
  `load_config()` only when `config is None`.  An explicit `config=` is used exactly as given.  So the
  `config_object` branch only matters to code that calls `load_config()` directly.  Level-1 behavior for
  `MldpClient` users comes from how they built the object, not from the loader.  In particular,
  `MldpClient(config=MldpConfig.from_yaml(path))` picks up the fix automatically.

- **T5 — The fix silently changes behavior for some existing deployments.**  Neither the ticket nor the
  triage comment says so.  A deployment that sets an `MLDP_*` variable **and** a different value for
  the same key in YAML connects to the YAML value today and to the env value after the fix.  That is what the
  docs always promised, but it happens with no error, so it belongs in `NEXT.md` as a silent behavior change.
  CLAUDE.md asks for breaking releases to lead with an upgrade checklist.

- **T6 — A tracked `mldp-config.yaml` at the repo root sets every key.**  Discovery step 4 (beside the
  nearest `pyproject.toml`) finds it from anywhere in a checkout.  So today, anyone running from a checkout
  cannot override **any** setting with `MLDP_*`.  For the tests this means two things.  Any new test that goes
  through discovery will pick up this file.  And a developer shell with `MLDP_*` exported (for example, left
  over from pointing integration tests at a remote ecosystem) will leak into tests that assume defaults.
  The new tests pass an explicit `config_file` and clear ambient `MLDP_*` variables.

- **T7 — The test gap goes beyond the two tests the triage comment named.**
  `test_config.py:303` `test_load_config_from_yaml` patches `from_yaml` away entirely, as noted.
  `test_mldp_client.py:197` `test_config_from_yaml_file` is the only real-file test that goes end to end, and
  it sets no env vars.  No test anywhere combines a real YAML file with `MLDP_*`.  No test pins the
  `config_object` precedence either: `test_load_config_with_explicit_object` sets no env var, so it cannot
  tell level 1 apart from level 2.

- **T8 — The cookbook callout is more than one block.**  Line numbers are current as of `f856b94`.  Besides
  the callout (`connecting.md:155-177`) and the ToC's "**and a bug to be aware of**" (`:14`), the heading
  sentence "The *intended* order" (`:148`) and the paragraph right after the callout
  (`:179-180`, "This also applies to the auto-load path…") both presuppose the bug.  `README.md` needs no
  change.  `CLAUDE.md` needs no correction, but it gains the invariant (task 6).

## Design decisions

- **D1 — YAML values reach pydantic through a ContextVar-backed settings source.**  `from_yaml()` keeps its
  current job: read, validate the shape, flatten, log, and wrap errors.  Instead of `cls(**flat_data)`, it sets
  a module-level `ContextVar[dict | None]` to `flat_data`, calls `cls()`, and resets the var in `finally`.  A
  small private `_YamlValuesSource(PydanticBaseSettingsSource)` returns the var's contents, or `{}` when it is
  unset.  `settings_customise_sources` returns
  `(init_settings, env_settings, dotenv_settings, file_secret_settings, _YamlValuesSource(settings_cls))`.
  That is pydantic's default order with YAML added last, just above field defaults.  (dotenv and secrets are
  not configured, so their position does not matter, and keeping them avoids an unexplained removal.)
  - *Rejected: restructure `MldpConfig` into nested models so the built-in `YamlConfigSettingsSource` applies.*
    It changes the public `ingestion_host`-style fields.  It would also need `env_nested_delimiter="_"`, which
    cannot tell `MLDP_INGESTION_USE_TLS` apart from `MLDP_INGESTION_USE` + `TLS`.
  - *Rejected: filter `flat_data` down to keys with no matching env var before calling `cls(**...)`.*  That
    re-implements pydantic's env lookup (prefix, case folding, dotenv) by hand, and would drift from it.
  - *Rejected: a dynamic subclass carrying the path in `model_config`.*  `type(result)` would no longer be
  `MldpConfig`.
  - Why a ContextVar and not a class attribute: it is safe across threads and asyncio tasks, and `reset()` in
    `finally` means a failed `from_yaml()` cannot leak YAML values into a later plain `MldpConfig()`.
- **D2 — Validation follows the merged result.**  pydantic merges sources before it validates.  So an invalid
  YAML value that an env var overrides (for example `port: abc` with `MLDP_INGESTION_PORT=443`) no longer
  raises, because the bad value is never used.  Accepted as correct.  An invalid YAML value that is *not*
  overridden still raises the existing `ValueError("Error loading configuration from …")`.
- **D3 — `load_config(config_object=...)` returns the object as-is.**  This matches the rebuild's values (T3)
  and makes level 1 obvious from the code.  The misleading comment and log line go away with it.  Callers
  get the same object back rather than a copy.  Nothing in the repo depends on that, and a config is not
  mutated after construction.  *Rejected: keep the rebuild and fix only the comment and log*; it adds nine
  lines that do nothing but copy values.  Settled by [Q1](#open-questions).
- **D4 — Per-key semantics are documented, not changed.**  For a `MldpConfig(ingestion_host="x")` built by
  the caller, the fields they passed are level 1.  Fields they left out took env or defaults when the object was
  built, which is ordinary pydantic-settings behavior and matches the docs.  The cookbook gets one sentence
  on this, because people may assume "explicit config object" means "env ignored entirely".

## Implementation tasks

1. **`src/dp_python_lib/config/config.py`**
   - Add a module-level `_yaml_values: ContextVar[dict[str, Any] | None]` (default `None`) and
     `_YamlValuesSource(PydanticBaseSettingsSource)`.  `get_field_value` is required by the ABC but unused; it
     returns `(None, field_name, False)`.  `__call__` returns a copy of the var's dict.
   - Add `MldpConfig.settings_customise_sources` with full type annotations (per D1), plus a comment that
     states the priority and why YAML must never be passed as init kwargs.
   - In `from_yaml()`, replace `return cls(**flat_data)` with the set / `cls()` / reset-in-`finally` sequence.
     Leave the `FileNotFoundError` → `cls()` fallback and the error wrapping alone.  Update the docstring:
     `MLDP_*` variables override values from the file.
2. **`src/dp_python_lib/config/loader.py`** — Make the `config_object` branch `return config_object`
   (per D3; Q1 resolved).  Use the log message `"Using explicit config object"` and a comment saying explicit
   parameters are level 1.  Fix the stale comments at `:106` / `:109` if they no longer read true.
3. **`tests/unit/test_config.py`**
   - A helper (context manager or `setUp`) that removes every `MLDP_*` variable from `os.environ` for the
     duration of a test (T6).
   - Delete the mocked `test_load_config_from_yaml` and replace it with real-file tests that drive
     `load_config(config_file=<tmp>)` through the full T2 matrix: env beats YAML, YAML beats default, an
     absent key falls back to default or env, and bool/int coercion from env over YAML (`use_tls`, `port`).
   - The same matrix through `MldpConfig.from_yaml()` directly, since it is public.
   - `MLDP_CONFIG_FILE` selecting the file **and** an `MLDP_*` override applying to it.
   - `load_config(config_object=MldpConfig(ingestion_host="explicit"))` with `MLDP_INGESTION_HOST` set
     resolves to `"explicit"`, which pins level 1 (T7).
   - After `from_yaml()` fails on a file with an invalid value, a plain `MldpConfig()` gets defaults.  This
     proves the ContextVar reset works.
   - D2: an invalid YAML value that an env var overrides loads without error.
4. **`tests/unit/test_mldp_client.py`** — Next to `test_config_from_yaml_file`, add an end-to-end case:
   real YAML plus `MLDP_INGESTION_HOST`, then assert that `grpc.insecure_channel` was called with the env host.
   This covers the path the ticket's Impact section describes.
5. **`doc/cookbook/connecting.md`** — Remove the callout (`:155-177`) and the ToC pointer (`:14`).
   "The intended order" becomes "The order".  Rewrite `:179-180` to say that the auto-discovered file is
   still overridden by env vars.  Add D4's one sentence under level 1.  Run
   `.dev/tools/check-cookbook-snippets.py`.
6. **`CLAUDE.md`** — Under *Configuration Implementation*, record the invariant: YAML values enter through
   `_YamlValuesSource`, ranked below env.  File-derived values must never be passed as init kwargs, because
   init kwargs are pydantic-settings' top priority.  `load_config(config_object=)` returns the object as-is.
7. **`doc/release-notes/NEXT.md`** — Add a section "Environment variables override the config file (#19)"
   that states the silent behavior change from T5 plainly.  If the release is cut as breaking, this goes on
   the upgrade checklist under silent changes.  Add it to the Contents list.
8. Quality gates: `pytest tests/unit/`, `ruff check .`, `ruff format --check .`, `mypy src/` (the
   `settings_customise_sources` override must match the base signature exactly).

## Out of scope

- Logging which YAML keys an env var overrode (decided against in Q2).
- Validating unknown YAML keys or sections (`ingestion: {hots: ...}` is silently ignored today).  This is a
  separate usability issue; file it if wanted.
- `.env` / secrets-dir support: not configured today, and the source order leaves room for it.
- Any change to discovery order (`find_config_file`), which already behaves as documented.

## Dependencies and sequencing

- No upstream dependency: dp-grpc and dp-service are not involved.
- Independent of the typed-stubs prep (PR #60, merged) and of #16/#17.  It touches
  only `config/`, its tests, `connecting.md`, `CLAUDE.md`, and `NEXT.md`.  Branch from `main`.
- The ticket asks that the cookbook callout be removed *when* this is fixed, so do it in the same PR (task 5).

## Open questions

- **Q1 — `config_object`: return as-is, or keep the rebuild?**  Returning as-is (D3) gives the same values,
  simpler code, and self-evident level-1 semantics.  The only observable difference is object identity.
  *Recommendation: return as-is.*  **Resolved 2026-09-24: return as-is**, per D3 and task 2.
- **Q2 — Log when an env var overrides a YAML value?**  This would answer the ticket's complaint about
  silence, but once fixed, the override *is* the documented behavior.  pydantic-settings does not expose
  which source a value came from.  Detecting an override means either comparing values after coercion,
  which gives false positives when YAML `"9001"` becomes `9001`, or re-implementing env lookup, which D1
  rejects.  *Recommendation: no.*  **Resolved 2026-09-24: no logging.**  The cookbook and `NEXT.md` state
  the order instead.
- **Q3 — Does this alone make the next release breaking?**  It restores documented behavior, but for T5's
  deployments it changes where the client connects.  The version is decided at the cut, not here (per
  `NEXT.md`'s own rule).  The plan's job is to make sure the `NEXT.md` entry is written as a silent behavior
  change, so whoever cuts the release can decide.  *No decision needed now.*
