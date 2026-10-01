# Release Notes — next release (unreleased)

**This is the working draft for the next release. It is not a release note yet.**

Sections accumulate here as tickets land, so the content is written while it is fresh and gets
reviewed in the PR that causes it.  A ticket that changes anything a user of the library, or of its
release artifacts, would notice adds its section here in that same PR.  At release time this file is
renamed to `doc/release-notes/rel-<version>.md` and finished — see **Cutting the release** at the
bottom.

**The version of the upcoming release is deliberately not named anywhere in this file**, in its
filename or in its prose.  `release.yml` resolves the notes path strictly from the tag
(`doc/release-notes/${GITHUB_REF_NAME}.md`), so a file committed under a guessed version is both
stranded and a failed release-notes check on the tag that does ship.  Past versions are named
freely where they are the point — "since 1.16.0" is a durable fact about what shipped, not a guess
about what is about to.  `.dev/tools/check-release-notes.py` enforces the parts that would carry the
new tag: this file may not contain a verification section, a `sigstore verify identity` command, a
signing identity, or a Full Changelog line.  Those are written at the cut.  Naming them in prose
is fine, as the checklist below does, but put the name in backticks: the checker treats an unquoted
`--cert-identity` followed by a word, or a line beginning with the verify command, as the real thing.

Links here are absolute `https://github.com/osprey-dcs/<repo>/blob/main/...` URLs: never relative,
and never a `rel-*` tag, which would guess the version.  The same checker confirms that each link
into this repo names a file and heading that exist, so a PR that renames a heading linked from here
fails CI until the link is fixed.  The links move to the release tag at the cut (step 6 below).

Nothing here should assert what *else* the release contains, either: that is knowable only once
the release is cut, and a stale claim in a file that already looks finished is not something the
person cutting the release has any reason to re-read.

## Contents

- [Release pages are the notes file, verbatim (#56)](#release-pages-are-the-notes-file-verbatim-issue-56)
- [Type checking in CI (#30)](#type-checking-in-ci-issue-30)
- [Ready for typed gRPC stubs (#61)](#ready-for-typed-grpc-stubs-issue-61)
- [Environment variables override the config file (#19)](#environment-variables-override-the-config-file-issue-19)
- [Detecting open-ended activations (#26)](#detecting-open-ended-activations-issue-26)
- [Ingesting data (#17)](#ingesting-data-issue-17)
- [Querying whole buckets (#16)](#querying-whole-buckets-issue-16)
- [Cutting the release](#cutting-the-release)

---

## Release pages are the notes file, verbatim (Issue #56)

The GitHub release page is now exactly `doc/release-notes/rel-<version>.md`, with nothing added by
the release workflow.  Previously the workflow appended artifact verification instructions and
GitHub's generated commit list after the notes.  That arrangement broke the first time anyone
republished a page from its notes file: rel-1.16.0's page silently lost its verification
instructions that way.  With the file as the whole page, republishing is lossless.

What changes for someone downloading a release:

- **Signature verification covers all three files.**  The instructions previously verified only the
  wheel, although the sdist and `SHA256SUMS` have always been signed too.  They now verify all three
  in one call, and the new [`README.env`](https://github.com/osprey-dcs/dp-python-lib/blob/main/README.env)
  is the full reference for what each artifact is and how to check it.  rel-1.16.0's page has been
  republished with the corrected instructions.
- **The commit list is replaced by a Full Changelog link**, a compare view between the two release
  tags.  The notes themselves are organized by ticket and link their PRs.

The release workflow checks that the published page matches the notes file after every release,
and CI checks every notes file's verification section for the right signing identity, since a stale
tag copied from the previous release makes `sigstore verify` reject every genuine artifact.

## Type checking in CI (Issue #30)

`mypy src/` is now clean and runs in CI on every PR, so type errors in the library fail review
rather than reaching a release.  The generated gRPC stubs are excluded until dp-grpc ships typed
ones ([osprey-dcs/dp-grpc#158](https://github.com/osprey-dcs/dp-grpc/issues/158)).  Nothing about
the library's behavior changes.

Two annotations became more accurate along the way:

- **`MldpClient.annotation` and `MldpClient.query` are annotated `X | None`**, which is what they
  have always been: they are `None` when the client is given only an ingestion channel.  The package
  does not yet ship a `py.typed` marker, so your own mypy runs are unaffected.  An editor that infers
  types from library source may now point out that they can be `None`; narrow once with
  `assert client.annotation is not None` if yours does.
- **`timestamp_list()` accepts any sequence** of timestamps (a tuple, say), not only a `list`.

## Ready for typed gRPC stubs (Issue #61)

dp-grpc is adding `.pyi` type stubs, generated by
[mypy-protobuf](https://github.com/nipunn1313/mypy-protobuf), to the gRPC stubs it syncs into
`src/dp_python_lib/grpc/`
([osprey-dcs/dp-grpc#158](https://github.com/osprey-dcs/dp-grpc/issues/158)).  The library's own
code now type-checks cleanly against those stubs, so a sync that brings them needs no change here.
Nothing about the library's behavior changes.

- **The `[codegen]` extra now includes `mypy-protobuf`.**  If you regenerate the stubs yourself, it
  supplies the `--mypy_out` / `--mypy_grpc_out` protoc plugins; without it you get the `.py` files
  but no `.pyi`.
- **The `[dev]` extra now includes `types-grpcio`**, so `grpc` is type-checked rather than ignored.

## Environment variables override the config file (Issue #19)

**Silent behavior change.**  An `MLDP_*` environment variable now overrides the same setting in the
YAML configuration file, as the documentation has always said it would.  Through 1.16.0 the file
won whenever it contained the key, and the environment variable was ignored without a warning.

Check before upgrading: **if you set an `MLDP_*` variable and your config file sets a *different*
value for the same key, the client will now connect using the environment variable's value.**  No
error is raised; the connection simply goes somewhere else.  This includes the `mldp-config.yaml`
that is discovered automatically in the working directory or project root.  To keep the old
behavior, unset the variable.

Two smaller consequences of the same fix:

- **An invalid value in the file no longer raises if an environment variable overrides it**
  (`port: abc` with `MLDP_INGESTION_PORT=443` now loads, using 443).  An invalid value that is
  actually used still raises the same `ValueError`.
- **An `MLDP_*` variable set to the empty string now counts as unset**, falling through to the file
  or the default.  Previously an empty `MLDP_INGESTION_HOST=` was lost to the file when the file set
  the key, but blanked the host when it did not, and an empty port or `use_tls` the file did not set
  raised.  Without this, the fix above would have let an empty variable, for example a docker
  compose `${VAR}` whose source is unset, override a working file with a blank host.
- **`load_config(config_object=...)` returns the object you passed** rather than a copy with the
  same values.  An explicit object was already level 1, above environment variables; only its
  identity changes.

See [#19](https://github.com/osprey-dcs/dp-python-lib/issues/19) and the configuration priority
section of [`doc/cookbook/connecting.md`](https://github.com/osprey-dcs/dp-python-lib/blob/main/doc/cookbook/connecting.md#configuration-priority).

## Detecting open-ended activations (Issue #26)

Two new helpers, exported from `dp_python_lib.client`, read an open-ended configuration activation
back (one saved without `end_time`, meaning "still in effect"):

- **`activation_is_open(activation)`** reports whether an activation has no end time.
- **`activation_end_time(activation)`** returns the end time as a `common.Timestamp`, or `None`
  for an open activation.

Both work on an activation from any read path: get, query, iterate, or
`get_active_configurations()`.  They exist because reading `activation.endTime` directly on an open
record does not fail.  It returns a zero `Timestamp`, 1970-01-01, which is truthy and not `None`,
so no ordinary check notices.  Passing that value back as `end_time=` in a re-save gets the save
rejected for an end time before the start; `end_time=activation_end_time(current)` carries it
forward correctly.

This is additive; nothing needs to change on upgrade.  **If you copied the "Every interval a
configuration was in effect" recipe** from
[`doc/cookbook/machine-configuration.md`](https://github.com/osprey-dcs/dp-python-lib/blob/main/doc/cookbook/machine-configuration.md),
it printed `0` as the end of an open interval; the recipe now uses `activation_end_time()`.  The
cookbook also gains a recipe for finding the activation to close at a changeover when you do
not have its id.

See [#26](https://github.com/osprey-dcs/dp-python-lib/issues/26).

## Ingesting data (Issue #17)

The library can now put data into MLDP, not just read it.  `client.ingestion_client`, which
previously offered only `register_provider()`, now covers data ingestion and request status
(`subscribeData()` is not wrapped yet):

- **`ingest_data()`** sends one request.  **`ingest_data_stream()`** sends many on one call and
  returns a single summary.  **`iter_ingest_data_bidi_stream()`** yields each request's ack or
  reject as it arrives.  To stop a bidi stream early, close its iterator, most simply with
  `contextlib.closing`: that cancels the call.  A plain `break` does not, while the iterator is still
  referenced, and gRPC keeps sending the remaining requests in the background.
- **`await_request_statuses()`** and **`query_request_status()`** report whether an ingestion
  actually landed.  This matters because **an ack means only that a request passed validation.**
  The server queues the data and writes it afterwards, so a request can be acked and still fail.
  An unknown provider id fails that way, and so does ingesting the same PV with the same first
  timestamp twice.  The request-status document, written once the data is stored, is the only
  confirmation.  Capture the time before sending and pass it as `since`.
- **`IngestDataRequestParams`** takes a `common.DataFrame` built with the existing `data_frame`
  builders, or with `data_frame_from_pandas()`.  Its request id defaults to a generated uuid, since
  the server does not enforce unique ids.
- **`data_frame.split_data_frame()`** cuts a large frame into chunks that fit the server's inbound
  message limit (4,096,000 bytes by default, about 500,000 doubles) and its one-day bucket span.
  **`chunked_request_params()`** turns the chunks into requests with correlated ids, lazily, ready
  for `ingest_data_stream()`.  No limit is assumed: pass `SERVER_DEFAULT_MAX_MESSAGE_BYTES` to
  target a default server.
- **New column builders** for the kinds that had none: `double_array_column()` and its float, int32,
  int64, and bool siblings (samples as nested lists or NumPy arrays), `image_column()`,
  `struct_column()`, and `serialized_column()`.
- **`RegisterProviderApiResult` gains `provider_id` and `is_new_provider`**, so the id no longer
  has to be dug out of `.response.registrationResult`.
- **`from_epoch_nanos()`**, the inverse of `to_epoch_nanos()`, is exported.

Two behaviors worth knowing:

- **A rejected request inside `ingest_data_stream()` does not raise.**  The result has `is_error`
  set and lists `rejected_request_ids`; the other requests were accepted.
- **If your own request generator raises during a stream, you get your exception back**, not
  gRPC's generic "Exception iterating requests!".  Requests sent before it may or may not have
  reached the server, so check their status.

**Behavior change: `data_frame()` rejects hand-built columns it used to pass.**  If you build
column messages from the protobuf types directly and pass them to `data_frame()`, it now raises
`ValueError` for an `EnumColumn` without an `enumId`, an array column with more than three
dimensions, an `ImageColumn` without a complete image descriptor, a `StructColumn` without a
`schemaId`, and a `SerializedDataColumn` without an `encoding`.  The server rejects all of these
anyway, so nothing that used to be accepted end to end is lost.  `enum_column()` likewise now
rejects a whitespace-only `enum_id`.

Non-scalar columns (arrays, images, structs) can be ingested, but not yet read back through the
query API, which returns scalar columns only; reading them back is
[#16](https://github.com/osprey-dcs/dp-python-lib/issues/16).

A new cookbook recipe, [Ingesting data](https://github.com/osprey-dcs/dp-python-lib/blob/main/doc/cookbook/ingestion.md), walks through registering,
ingesting, confirming, chunking, and streaming.  It was run end to end against a live MLDP stack, as
were the query and sample-status recipes over the data it stores, and the integration tests now
ingest their own data through the library and wait for its SUCCESS status.

**Upgrade item — an error at call time: `QueryParams` requires a `pv_selector`.**  It used to accept
`config_criteria` alone, but the server rejects every such query (`querySpec.pvSelector must be
specified`), so `QueryParams` now refuses it itself.  `pv_selector` no longer has a default, so
leaving it out raises `TypeError` (missing argument), which a type checker also reports; passing
`None` or a `PvSelector` with no form set raises `ValueError`.  Nothing that worked is lost.  To
query every PV under a configuration, select them explicitly with `PvQuery.pattern(".*")`
alongside the `config_criteria`.

Re-running the older recipes against real data also corrected two things they claimed:

- **Sample query results carry no column metadata.**  The server's sample path populates none, so
  `to_dataframe()` leaves `df.attrs` empty; read the catalogue with `get_pv_metadata()` instead.
- **With dense sample-status labeling, name the codes in `SampleStatusFilter.include()`.**  A model
  that scores every sample gives every sample a status, including its "normal" code, so an
  `include()` without `status_codes` keeps them all.

See [#17](https://github.com/osprey-dcs/dp-python-lib/issues/17) and the Ingestion API section of
[`CLAUDE.md`](https://github.com/osprey-dcs/dp-python-lib/blob/main/CLAUDE.md).

## Querying whole buckets (Issue #16)

`client.query` now wraps the bucket-oriented v2 query, `queryBuckets` / `queryBucketsStream`.  Where
`query_samples()` returns an aligned, trimmed table of scalar columns, a bucket query returns the
archive's stored units: each `DataBucket` holds one PV's column, in the type it was ingested as, over
that bucket's own time axis.  It is the way to read back array, image, struct, and serialized columns,
and the first query path that returns the column metadata (provenance included) stored with the data.

- **`query_buckets()`, `iter_query_buckets()`, and `iter_query_buckets_stream()`** take the same
  `QueryParams` as the sample methods and behave the same way: one page, every page (raising
  `RuntimeError` on a page error), or the server stream (raising on a mid-stream error).
  `QueryBucketsApiResult` exposes `data_buckets` and `next_page_token`.
- **A new module, `bucket_conversions`**, reads them.  In plain Python: `bucket_column()`,
  `bucket_values()`, `bucket_timestamps()` (integer nanoseconds), `bucket_to_data_frame()` (a bucket
  as a one-column `common.DataFrame`, so the existing `data_frame_conversions` readers apply), and
  `buckets_by_pv()`.  With the `[analysis]` extra: `buckets_to_dataframes()`, which returns one pandas
  DataFrame per PV, and `query_buckets_to_dataframes()`, which runs the whole query first (with a
  `max_buckets` cap).  `QueryBucketsApiResult.to_dataframes()` converts one page.

Behaviors worth knowing:

- **Buckets come back whole.**  The server returns every bucket that overlaps `[begin, end)`
  without trimming it, so a bucket at either edge carries samples outside the range.  Trimming is
  opt-in: `trim_bucket()`, `time_range=` on the conversions, or `trim=True` on
  `query_buckets_to_dataframes()`.  It is exact and half-open, like `query_samples()`.  It does not
  remove samples that fall in a gap between configuration intervals when `config_criteria` is used;
  use `query_samples()` when that matters.
- **`limit` counts buckets on this path, not rows**, and a page can also end early, with a page
  token, once it reaches the server's message-size budget.  The server silently caps the limit at its
  configured maximum (100,000 by default).
- **A `QueryParams` with a `sample_status_filter` is refused** with a `ValueError` before any call
  is made.  The server rejects status filtering on bucket queries, and dropping the filter silently
  would return data the caller believed was filtered.
- **A serialized column is passed through, not decoded.**  It comes back only if it was ingested
  that way.  `bucket_column()` returns it with its `encoding` and `payload`; the value readers and
  pandas conversions raise rather than drop it.
- **A PV's buckets are kept as stored.**  Overlapping buckets from separate ingests are all kept,
  so a per-PV frame's index can repeat instants.  A PV whose buckets differ in column type or
  structure (say, ingested as double and later as int32) raises, naming both buckets.  Per-bucket
  provider and column metadata is in `df.attrs["buckets"]`.

See [#16](https://github.com/osprey-dcs/dp-python-lib/issues/16) and `plan/tickets/16/plan.md`.

## Installing

```bash
pip install dp_python_lib-*.whl
```

---

## Cutting the release

When the version is known and the release is being cut:

1. **`git mv doc/release-notes/NEXT.md doc/release-notes/rel-<version>.md`.**  The filename must
   match the tag exactly; `release.yml` fails the run before the build if it does not.
2. **Retitle** the H1 to `# dp-python-lib <version> Release Notes` and replace this file's preamble
   with a "Changes since rel-<previous>" summary — written now, when the full contents of the
   release are actually known.  If the stubs were resynced, link dp-grpc's notes for the same
   release, as rel-1.16.0's opening does.
3. **Decide whether the release is breaking**, and say so in the opening if it is.  A breaking
   release gets an **"Upgrading from <previous>"** section as the first section after Contents,
   folding in the per-ticket upgrade items above.  Call out silent behavior changes separately from
   outright errors, per CLAUDE.md: a change that alters results without raising is the one a reader
   most needs up front.  Python has no compile step, so "outright errors" here means ones raised at
   import or call time.
4. **Add the `## Verifying these artifacts` section** immediately above `## Installing`, copied from the previous release's notes with
   the tag changed: `sha256sum -c SHA256SUMS`, then `sigstore verify identity` over the wheel,
   sdist, and `SHA256SUMS` with `--cert-identity` ending `release.yml@refs/tags/rel-<version>`, and
   the pointer to `README.env`.
5. **End with the Full Changelog line**, after `## Installing`:
   `**Full Changelog**: https://github.com/osprey-dcs/dp-python-lib/compare/rel-<previous>...rel-<version>`.
6. **Repoint every `blob/main/...` link to `blob/rel-<version>/...`**, including links into the
   other osprey-dcs repos, which release in lockstep.  This file is published as the release body
   via `body_path`, where a relative link 404s and a `main` link drifts as the repo moves on; pinned
   to the tag it keeps describing the content this release actually shipped.  Don't hunt for them
   by eye: step 8 lists every one you missed, and any stale `rel-*` tag copied from older notes.
7. **Delete this "Cutting the release" section** and update Contents.
8. **Run `python .dev/tools/check-release-notes.py`** and fix everything it lists; CI runs it on the
   PR too.  For the new file it fails on a relative link; a link into any osprey-dcs repo not
   pinned to `rel-<version>`; a path or `#anchor` into this repo that is missing from the tree being
   tagged, or that points at a duplicated heading; a leftover `rel-<version>`, `<version>`, or
   `<previous>`; a missing verification section; a stale tag in the identity or the changelog
   link; or a verify command that skips a file.  The rules are in the script's docstring
   (osprey-dcs/data-platform#98).
9. **Start a fresh `NEXT.md`** for the following cycle.  Steps 1, 2, and 7 have moved, rewritten, and
   deleted the text it needs, so recover it from `main`:
   `git show main:doc/release-notes/NEXT.md > doc/release-notes/NEXT.md`, then delete every ticket
   section and empty Contents down to the "Cutting the release" entry.  Keep the preamble,
   `## Installing`, and this checklist.
10. **Merge, then push the `rel-<version>` tag.**  The notes must be on the tagged commit.
