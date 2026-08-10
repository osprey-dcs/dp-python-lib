# Creating and Connecting a Client

Constructing an `MldpClient`, pointing it at your services, and knowing which sub-clients you
actually got.

> **Verified against:** dp-python-lib 1.15.0.

See [API conventions](conventions.md) for the patterns every call shares once you have a client.

## Contents

- [The four ways to construct a client](#the-four-ways-to-construct-a-client)
- [Model](#model) — one client, three services, three channels
- [Configuration files](#configuration-files)
- [Environment variables](#environment-variables)
- [Configuration priority](#configuration-priority) — **and a bug to be aware of**
- [Sub-clients can be None](#sub-clients-can-be-none)
- [TLS](#tls)
- [Logging](#logging)

## Model

`MldpClient` is a container for three independent service clients, each with its own gRPC
channel:

| Attribute | Service | Default port | Covered by |
|---|---|---|---|
| `client.ingestion_client` | Ingestion | 50051 | — |
| `client.query` | Query | 50052 | [query.md](query.md) |
| `client.annotation` | Annotation | 50053 | [pv-metadata.md](pv-metadata.md), [machine-configuration.md](machine-configuration.md) |

The annotation service backs several feature areas, so `client.annotation` is a facade exposing
feature-scoped clients:

- `client.annotation.pv_metadata`
- `client.annotation.machine_config`

The three services are configured independently — they may live on different hosts, different
ports, and have different TLS settings.

## The four ways to construct a client

### 1. Auto-load (the usual case)

```python
from dp_python_lib.client import MldpClient

client = MldpClient()
```

Searches for a configuration file, applies environment variables, and falls back to defaults.

### 2. A specific configuration file

```python
from dp_python_lib.client import MldpClient

client = MldpClient(config_file="deploy/prod-config.yaml")
```

**A missing file here raises `FileNotFoundError`** — an explicitly named file that does not exist
is treated as an error, not as a reason to fall back to defaults.

### 3. A configuration object

```python
from dp_python_lib.client import MldpClient
from dp_python_lib.config import MldpConfig

config = MldpConfig(
    ingestion_host="ingest.example.com",
    ingestion_port=50051,
    query_host="query.example.com",
    query_port=50052,
    annotation_host="annotation.example.com",
    annotation_port=50053,
)
client = MldpClient(config=config)
```

Note the **flat** field names (`ingestion_host`, not a nested `ingestion=...`).  The grouped
`client.config.ingestion` view is a read-only property built from these.

### 4. Explicit channels

For tests, or when you need channel options the config layer does not expose:

```python
import grpc
from dp_python_lib.client import MldpClient

client = MldpClient(
    ingestion_channel=grpc.insecure_channel("localhost:50051"),
    query_channel=grpc.insecure_channel("localhost:50052"),
    annotation_channel=grpc.insecure_channel("localhost:50053"),
)
```

Read [Sub-clients can be None](#sub-clients-can-be-none) before using this form — passing only
some channels has a consequence that is easy to miss.

## Configuration files

The default file is `mldp-config.yaml`:

```yaml
ingestion:
  host: localhost
  port: 50051
  use_tls: false
query:
  host: localhost
  port: 50052
  use_tls: false
annotation:
  host: localhost
  port: 50053
  use_tls: false
```

Every key is optional; anything you omit falls back to the default for that service.

Discovery order, first match wins:

1. The `config_file` parameter
2. The `MLDP_CONFIG_FILE` environment variable
3. `mldp-config.yaml` in the current working directory
4. `mldp-config.yaml` beside the nearest `pyproject.toml`

Steps 3 and 4 make this **working-directory sensitive** — the same code can pick up a different
file depending on where the process starts.  In a deployment, name the file explicitly.

## Environment variables

Settings follow `MLDP_<SERVICE>_<SETTING>`:

```bash
export MLDP_INGESTION_HOST=ingest.example.com
export MLDP_INGESTION_PORT=443
export MLDP_INGESTION_USE_TLS=true

export MLDP_CONFIG_FILE=/etc/mldp/config.yaml
```

Names are case-insensitive.  `USE_TLS` accepts the usual boolean spellings (`true`/`false`,
`1`/`0`).

## Configuration priority

The intended order, highest first:

1. Explicit constructor parameters (channels, `config=`)
2. Environment variables (`MLDP_*`)
3. The YAML configuration file
4. Built-in defaults

> ### ⚠️ Known bug: YAML silently beats environment variables
>
> **As of 1.15.0, levels 2 and 3 are inverted whenever the key is present in the YAML file.**
> Tracked as [#19](https://github.com/osprey-dcs/dp-python-lib/issues/19).
>
> A setting written in YAML **cannot be overridden** by its `MLDP_*` environment variable.  The
> env var is ignored, silently — no warning, no error:
>
> ```
> # mldp-config.yaml contains:  ingestion: {host: localhost}
> MLDP_INGESTION_HOST=prod.example.com  ->  resolves to "localhost"   (env ignored)
> MLDP_INGESTION_PORT=443               ->  resolves to 443           (works: port absent from YAML)
> ```
>
> The rule is per-key: a key **absent** from the YAML file *is* overridable by its env var; a key
> **present** in the file is not.
>
> Cause: `MldpConfig.from_yaml()` passes YAML values as constructor keyword arguments, and in
> pydantic-settings init kwargs outrank environment variables.
>
> **Until this is fixed**, do not rely on env vars to override a deployed YAML file.  Either keep
> the setting out of the YAML entirely, or point at a different file with `MLDP_CONFIG_FILE` (that
> variable is read before the file is loaded, so it works as documented).

This also applies to the auto-load path, since a `mldp-config.yaml` in the working directory is
picked up automatically — which is how the surprise usually arrives.

## Sub-clients can be None

`client.query` and `client.annotation` are `None` when no corresponding channel was configured:

```python
# cookbook:partial
if client.query is None:
    raise RuntimeError("no query channel configured")

params = QueryParams(begin_time=begin, end_time=end, pv_selector=PV.name_list(["BPMS:GUNB:314:X"]))
result = client.query.query_samples(params)
```

This happens in exactly one situation: **passing `ingestion_channel` explicitly without also
passing the others.**

```python
import grpc
from dp_python_lib.client import MldpClient

client = MldpClient(ingestion_channel=grpc.insecure_channel("localhost:50051"))

# client.query      is None
# client.annotation is None
```

Calling straight through gives `AttributeError: 'NoneType' object has no attribute ...`, which
points at the call site rather than at the missing configuration — an unhelpful place to start
debugging.

The trigger is specifically `ingestion_channel`, because it is what suppresses configuration
loading.  The other forms all end up with a config object, and a config always defines all three
services (every one has built-in defaults), so all three channels get created:

| Constructed with | `ingestion_client` | `query` | `annotation` |
|---|---|---|---|
| `MldpClient()` | ✅ | ✅ | ✅ |
| `config=` / `config_file=` | ✅ | ✅ | ✅ |
| `query_channel=` only | ✅ (from config) | ✅ | ✅ (from config) |
| `ingestion_channel=` only | ✅ | **None** | **None** |

So if you pass `ingestion_channel`, pass the channels you need alongside it.

`client.ingestion_client` is never `None`: an ingestion channel is required, and constructing a
client without one raises `ValueError`.

Note that a channel being present says nothing about the server being reachable — gRPC channels
connect lazily, so an unreachable service surfaces as an error on the first call, not at
construction.

## TLS

Set `use_tls` per service, in YAML or by env var:

```yaml
ingestion:
  host: ingest.example.com
  port: 443
  use_tls: true
```

`use_tls: true` builds a secure channel with the system's default root certificates.  For custom
credentials — a private CA, or mutual TLS — build the channel yourself and pass it explicitly:

```python
import grpc
from dp_python_lib.client import MldpClient

with open("ca.pem", "rb") as f:
    credentials = grpc.ssl_channel_credentials(root_certificates=f.read())

client = MldpClient(
    ingestion_channel=grpc.secure_channel("ingest.example.com:443", credentials),
    query_channel=grpc.secure_channel("query.example.com:443", credentials),
    annotation_channel=grpc.secure_channel("annotation.example.com:443", credentials),
)
```

## Logging

The library logs through the standard `logging` module under the `dp_python_lib.*` hierarchy, but
installs no handler.  Configure logging in **your application**, not in the library:

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
```

Useful logger names for turning the volume up or down per area:

- `dp_python_lib.client.mldp_client` — construction and channel setup
- `dp_python_lib.client.pv_metadata_client` — PV metadata calls
- `dp_python_lib.client.machine_config_client` — configuration calls
- `dp_python_lib.client.query_client` — query calls
- `dp_python_lib.config.loader` — **config file discovery**, useful when the wrong file is being
  picked up

At `DEBUG`, the clients log request construction and response handling; at `INFO`, one line per
API call.

## Also worth knowing

- **Channels are created once, at construction**, and the service stubs are built once from them.
  Reuse a single `MldpClient` rather than constructing one per call.
- `MldpClient` has no `close()`.  To release channels deterministically, keep references to the
  channels you passed in and close them yourself.
- The grouped view (`config.ingestion.host`, `config.query.port`) is a read-only property over the
  flat fields — assigning to it has no effect.
