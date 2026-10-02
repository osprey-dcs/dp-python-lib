# dp-python-lib Cookbook

Task-oriented, worked examples of using the `dp_python_lib` client API.

The [main README](../../README.md) describes the library's structure and classes.  This cookbook
is the *guide*: each recipe walks through a complete task — cataloguing a device, recording a
configuration change, pulling a shift's worth of data into a DataFrame — in the order you would
actually do it.

Recipes document the **client layer**: `MldpClient`, the feature clients, the criterion builders,
the parameter objects, and the result classes.  For the wire protocol underneath — the protobuf
messages and RPC semantics — see the
[dp-grpc cookbook](https://github.com/osprey-dcs/dp-grpc/tree/main/doc/cookbook), which documents
the same API in Java.

## Recipes

Read in order for a continuous worked example; each recipe stands alone if you already have a
client.

| Recipe | Covers |
|---|---|
| [API conventions](conventions.md) | Patterns every call shares: checking `result_status`, paging with `query_*` vs. `iter_*`, criteria AND/OR rules, full-replace `save_*`, time handling |
| [Creating and connecting a client](connecting.md) | The four ways to build an `MldpClient`, configuration files and environment variables, TLS, logging, and when sub-clients are `None` |
| [Cataloguing PVs](pv-metadata.md) | Recording what a PV *is* — device, area, element type, position — then finding PVs by those properties instead of by name |
| [Recording machine configuration](machine-configuration.md) | Defining configurations, recording when each was active, closing and opening intervals, and answering "what was the machine doing at 18:04?" |
| [Ingesting data](ingestion.md) | Registering a provider, sending frames of samples, confirming they landed, and chunking and streaming data too big for one message |
| [Querying time-series data](query.md) | Retrieving samples by PV name, by metadata, or by machine configuration, and converting results to pandas / NumPy / Excel; reading whole stored buckets, including array, image, and struct columns |
| [Labeling samples](sample-status.md) | Recording per-sample status codes, reading them back, and querying data with flagged samples excluded |
| [DataSets and annotations](datasets-and-annotations.md) | Naming a region of the archive, attaching analysis results with column-level provenance, round-tripping calculations through pandas, and exporting |

## The worked example

The recipes share one continuous example drawn from an accelerator facility, so the data in the
query recipes is the data the earlier recipes create:

- A beam position monitor, `BPMS:GUNB:314`, reporting `:X`, `:Y`, and `:TMIT`, catalogued with the
  properties the accelerator model uses — `DEVICE`, `ELEMENT`, `TYPE`, `AREA`, and the positions
  `Z` and `S`.
- A physics-shift configuration, `cxi-production` (`PATH=CU_HXR`, `E=14.6`, `RATE=10000`,
  `MODE=09`), activated over a shift with `DEST=CXI` and `EXP=CXI_3443`.
- Samples of those signals ingested by provider `bpm-daq` during the shift, at 10 kHz from
  18:04:12 — the first second of them is what the query and labeling recipes read back.
- Queries that retrieve those PVs by name, by *"every monitor in GUNB"*, and by *"whatever ran
  during the CXI shift"*.
- A dataset naming the first hour of that shift, an annotation recording an orbit drift, and a 1 Hz
  RMS calculation attached to it with provenance pointing back at the source PV.

Attribute names and values are the facility's; tag values are illustrative placeholders.

## Conventions used in recipes

- **Recipes note the release an API arrived in, where it matters.**  This package's version tracks
  the dp-grpc version its stubs were generated from, so a server older than your `dp_python_lib`
  will not implement everything documented here.  Where a recipe uses something added in a
  particular release, it says so in the body — the [v2 query API](query.md) needs a `rel-1.15.0` or
  later server, [sample status](sample-status.md) needs `rel-1.16.0` or later, and
  [column provenance](datasets-and-annotations.md#recording-where-the-numbers-came-from) sent
  with [ingested data](ingestion.md#ingesting-a-frame) survives only into `rel-1.16.0` or later.
- Snippets omit imports and client construction except where a recipe is specifically about those
  things.  Each recipe lists the imports its examples assume.
- Examples check `result_status.is_error` before reading a payload.  This is not ceremony: the
  typed accessors return `None` or `[]` on failure, so skipping the check turns an error into
  silently missing data.  See [conventions](conventions.md#checking-results).

## Verifying the examples

Every Python snippet in this directory is mechanically checked — parsed for syntax, then
type-checked against the installed package to catch wrong attribute and method names — and each
recipe is checked for importing every library name its fragments use:

```bash
pip install -e .[dev]
.venv/bin/python .dev/tools/check-cookbook-snippets.py
```

The checker self-tests before each run: if mypy ever stops resolving `dp_python_lib`, it would
report success on every snippet regardless of correctness, so a canary asserts that a known-bad
attribute is still flagged.  A second canary does the same for the import check.

Snippets carry `# cookbook:partial` when they are fragments that assume a client, and
`# cookbook:skip` or `# cookbook:no-mypy` where checking does not apply.  A partial snippet is
type-checked with a shared preamble that supplies `client` and the library's imports, so the
import check is what holds a recipe to its own imports: every name a partial snippet uses that the
preamble imports must be bound somewhere in the same recipe — usually its "Imports used by the
examples" block, which is checked like any other snippet.  Do not mark that block
`# cookbook:skip`; a skipped block binds nothing.
