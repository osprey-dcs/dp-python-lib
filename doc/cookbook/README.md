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
| [Querying time-series data](query.md) | Retrieving samples by PV name, by metadata, or by machine configuration, and converting results to pandas / NumPy / Excel |

## The worked example

The recipes share one continuous example drawn from an accelerator facility, so the data in the
query recipes is the data the earlier recipes create:

- A beam position monitor, `BPMS:GUNB:314`, reporting `:X`, `:Y`, and `:TMIT`, catalogued with the
  properties the accelerator model uses — `DEVICE`, `ELEMENT`, `TYPE`, `AREA`, and the positions
  `Z` and `S`.
- A physics-shift configuration, `cxi-production` (`PATH=CU_HXR`, `E=14.6`, `RATE=10000`,
  `MODE=09`), activated over a shift with `DEST=CXI` and `EXP=CXI_3443`.
- Queries that retrieve those PVs by name, by *"every monitor in GUNB"*, and by *"whatever ran
  during the CXI shift"*.

Attribute names and values are the facility's; tag values are illustrative placeholders.

## Conventions used in recipes

- Each recipe states the release it was **verified against**.  Most of the API is stable since
  dp-grpc `rel-1.14.0`, the current release.  The [v2 query API](query.md) is the exception: it
  comes from unreleased dp-grpc work and **will not work against a `rel-1.14.0` server**.
- Snippets omit imports and client construction except where a recipe is specifically about those
  things.  Each recipe lists the imports its examples assume.
- Examples check `result_status.is_error` before reading a payload.  This is not ceremony: the
  typed accessors return `None` or `[]` on failure, so skipping the check turns an error into
  silently missing data.  See [conventions](conventions.md#checking-results).

## Verifying the examples

Every Python snippet in this directory is mechanically checked — parsed for syntax, then
type-checked against the installed package to catch wrong attribute and method names:

```bash
.venv/bin/python .dev/tools/check-cookbook-snippets.py
```

Snippets carry `# cookbook:partial` when they are fragments that assume a client, and
`# cookbook:skip` or `# cookbook:no-mypy` where checking does not apply.
