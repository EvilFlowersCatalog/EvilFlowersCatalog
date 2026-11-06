# Observability

How the catalog emits telemetry, and what it promises the Elvira LGTM stack. The design and its
reasoning are in [IP-019](proposals/posts/ip-019-opentelemetry-observability.md).

## In one paragraph

Each process in the container (4 gunicorn gevent workers, the Celery prefork children, Celery
beat, `manage.py celery_event_handler`) sets up the OpenTelemetry SDK after it has forked. It
exports **traces and metrics over OTLP/HTTP** (`http/protobuf`, port 4318) to the node's Grafana
Alloy agent. It writes **logs as JSON lines to stdout/stderr**, which Alloy already tails, and
never exports them over OTLP. With no collector reachable, telemetry is off and the app behaves as
before.

## Finding the collector

Precedence, first match wins:

1. `OTEL_EXPORTER_OTLP_ENDPOINT` (or the per-signal `_TRACES_` / `_METRICS_` variants).
2. `HOST_IP:4318`, if `HOST_IP` is set. On Kubernetes, set it from the downward API
   (`status.hostIP`). A pod's default gateway is the CNI router, not the node.
3. `<default gateway>:4318`, read from `/proc/net/route`. On a Docker bridge network the
   gateway is the host, where Alloy listens on the host network: `172.28.0.1` on `elvira`,
   `172.38.0.1` on `fiit`.

Options 2 and 3 are probed once, with a 300 ms TCP connect. On success the endpoint is written
into the process environment and one INFO line names it. On failure telemetry stays off, with one
INFO line and no retries. The Celery main process probes before it forks, and its children
inherit the result. Each gunicorn worker probes on its own: the master must not import the
project before gevent patches the workers.

Only OTLP/HTTP (`http/protobuf`) is built. `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` is ignored.

`OTEL_EXPORTER_OTLP_AUTODISCOVER=false` disables options 2 and 3. `OTEL_SDK_DISABLED=true` turns
everything off.

## The contract with the collector's own discovery

Alloy, Tempo, Beyla and Pyroscope find containers without any per-app config. The catalog fits
in without a collector change because:

| Collector side | Catalog side |
|---|---|
| Loki `service_name` = Compose service (`evilflowers`) | `OTEL_SERVICE_NAME` defaults to `evilflowers`. Traces, logs and Pyroscope profiles share one name. |
| `stage.json` reads exactly `level` and `trace_id` | Every log line is one JSON object with `time`, `level` (upper case), `logger`, `msg`, `trace_id`, `span_id` (both inside a span), `exc` (traceback), plus any `extra=` fields. |
| Service graph draws Postgres, Redis and external APIs from CLIENT spans with `db.system.name` / `server.address` | psycopg, redis, requests and the LLM client emit CLIENT spans. `OTEL_SEMCONV_STABILITY_OPT_IN=http,database` (the default) gives them the stable attribute names. |
| Span metrics and exemplars | `http.server.request.duration` (seconds, with `http.route`). Histograms carry trace exemplars (SDK default `trace_based` filter). |
| Beyla skips processes that already export OTel | The process exports OTLP, and the Compose file **must** set `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` (see below). The endpoint the app discovers is set in-process, so it never appears in `/proc/<pid>/environ`. |
| cAdvisor has container CPU and memory | Not duplicated. Only per-process metrics: `process.cpu.time`, `process.memory.usage`, `process.thread.count`, `process.open_file_descriptor.count`, `cpython.gc.collections`. |

### Resource attributes

| Attribute | Value |
|---|---|
| `service.name` | `evilflowers` (env `OTEL_SERVICE_NAME` wins) |
| `service.namespace`, `deployment.environment.name` | from `OTEL_RESOURCE_ATTRIBUTES` |
| `service.version` | `pyproject.toml` version |
| `service.instance.id` | `<hostname>-<pid>`. Each process is its own series in Mimir. |
| `evilflowers.process.role` | `gunicorn`, `celery-worker`, `celery-beat` or `event-handler` |
| `container.id`, `host.*`, `os.*`, `process.*` | resource detectors |

Anything in `OTEL_RESOURCE_ATTRIBUTES` overrides the defaults above.

## What the production Compose file should set

The file is managed by Ansible outside this repository; nothing here changes it.

```yaml
services:
  evilflowers:
    environment:
      # Required: Beyla must see the process as instrumented.
      OTEL_SERVICE_NAME: evilflowers
      OTEL_RESOURCE_ATTRIBUTES: "service.namespace=elvira,deployment.environment.name=production"  # development on evilflowers-dev
      # Optional. Without it the gateway is discovered; with it there is no probe.
      OTEL_EXPORTER_OTLP_ENDPOINT: "http://172.28.0.1:4318"
      OTEL_EXPORTER_OTLP_PROTOCOL: http/protobuf
      EVILFLOWERS_LOG_FORMAT: json
    # Drop any ./logs:/var/log mount: nothing writes there any more.
    # Drop LOGFIRE_*, ELASTIC_APM_* and SENTRY_DSN: none is read any more.
```

The image must be built with the `otel` dependency group (`.github/workflows/build.yml` does).

## Signals

### Traces

- SERVER span per request, named method plus Django route, for example
  `GET api/v1/catalogs/<uuid:catalog_id>/entries/<uuid:entry_id>`. Django routes have no
  leading slash. `/api/v1/status` (the Docker HEALTHCHECK) produces no telemetry at all: no
  server span and no query spans.
- CLIENT: psycopg, redis, requests (Readium LCP/LSD, Dataverse, search and text services).
  Credentials in URLs are redacted.
- Celery: PRODUCER in the sender, CONSUMER in the worker, in the same trace (context travels in
  the task headers).
- `apps/mcp`: one INTERNAL span per JSON-RPC message, `"{mcp.method.name} {gen_ai.tool.name}"`.
- `apps/assistant`: one CLIENT span per model call, `chat {model}`, with `gen_ai.*` attributes
  and token usage. No prompt or document text ever goes into attributes.
- Sampling: `parentbased_always_on`. Propagation: `tracecontext,baggage`.

### Metrics

| Name | Unit | Attributes |
|---|---|---|
| `http.server.request.duration` | s | `http.request.method`, `http.route`, `http.response.status_code` |
| `mcp.server.operation.duration` | s | `mcp.method.name`, `gen_ai.tool.name`, `error.type` |
| `gen_ai.client.operation.duration` | s | `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `server.address`, `error.type` |
| `gen_ai.client.token.usage` | {token} | as above + `gen_ai.token.type` |
| `evilflowers.readium.license.transitions` | {license} | `evilflowers.readium.transition` (issued, returned, renewed, revoked, cancelled, expired), `evilflowers.readium.expiry_path` (sweep, on_read) |
| `evilflowers.extraction.runs` | {run} | `evilflowers.extraction.outcome` (done, failed) |
| `evilflowers.extraction.run.duration` | s | `evilflowers.extraction.outcome` |
| `evilflowers.acquisitions.served` | {file} | `evilflowers.acquisition.mime`, `.via` (acquisition, user_acquisition), `.delivery` (file, base64) |
| `process.*`, `cpython.gc.collections` | | per process |

Mimir stores dotted names with underscores and unit suffixes, for example
`evilflowers_readium_license_transitions_total`.

### Logs

JSON on stdout/stderr, as above. The gunicorn access log is off: the server span and
`http.server.request.duration` replace it. Gunicorn's own error log, supervisord and Celery use
the same JSON format. `EVILFLOWERS_LOG_FORMAT=text` gives readable lines for a terminal.

## Local

```sh
docker compose --profile observability up -d otel-lgtm   # Grafana on http://localhost:3000
docker compose up django                                  # discovers <gateway>:4318
```

Or, with no collector at all, uncomment `OTEL_TRACES_EXPORTER: "console"` and
`OTEL_METRICS_EXPORTER: "console"` in `compose.yml` to print spans and metrics to the container
output.
