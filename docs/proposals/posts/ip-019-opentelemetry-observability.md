---
draft: false
date: 2026-11-06
authors:
  - jdubec
categories:
  - Operations
tags:
  - observability
  - opentelemetry
  - logging
  - grafana
  - deployment
---

# IP-019: OpenTelemetry Observability for the Elvira LGTM Stack

The catalog's telemetry is rebuilt on OpenTelemetry. It now fits the Grafana stack Elvira runs in
production: Alloy, then Tempo, Mimir, Loki and Pyroscope. Every process configures the SDK after it
forks and finds the node's Alloy agent without per-host config. It also lines up with what Alloy,
Beyla and Tempo already discover on their own: one service name, JSON logs with `level` and
`trace_id`, and CLIENT spans for the service graph. Logfire, Elastic APM and Sentry are gone;
OpenTelemetry is the only telemetry.

<!-- more -->

## Status

**Status**: Implemented
**Last Updated**: 2026-11-06
**Implementation**: Complete on `develop`, not committed. Verified locally against the built image
(see [Verification](#verification)). Not yet deployed to `evilflowers-dev` or production.

## Problem Statement

Production runs the catalog as the Compose service `evilflowers` (projects `evilflowers-prod` and
`evilflowers-dev`) on the bridge network `elvira`. One container runs 4 gunicorn gevent workers,
a Celery prefork worker, Celery beat and `manage.py celery_event_handler`, all under supervisord.
The node's only telemetry entry point is a Grafana Alloy agent on the host network. It receives
OTLP on `:4318`/`:4317`, tails every container's stdout/stderr and runs Beyla and Pyroscope eBPF.

What the repository had before this proposal did not fit that stack:

- **Three vendors, none of them the stack.** Sentry (`SENTRY_DSN`), Elastic APM
  (`ELASTIC_APM_SERVICE_NAME`) and Logfire (`LOGFIRE_TOKEN`), each switched on by its own variable
  in `settings/base.py`.
- **Initialisation in the wrong process.** `logfire.configure()` and its instrumentation ran at
  settings import. That happens in the gunicorn master and the Celery main process before they
  fork, and before gevent monkey-patches the workers. Exporter threads do not survive a fork, and
  their locks were created un-patched. `instrument_django()` ran in `CoreConfig.ready()`.
- **Invisible logs.** Celery worker, beat and the event handler wrote to `/var/log/*.log` inside
  the container (`conf/supervisor.conf`), which Alloy never reads. `compose.yml` mounted
  `./logs:/var/log` to work around it locally. Gunicorn's own lines and Celery's were free text,
  so Alloy's `stage.json` extracted neither `level` nor `trace_id`.
- **No collector discovery.** Pointing an exporter at Alloy meant hand-writing the bridge gateway
  per host: `172.28.0.1` on `elvira`, `172.38.0.1` on `fiit`.
- **Packaging by accident.** The only thing that put `opentelemetry-*` into the image was
  `--with logfire` (OTel 1.35/0.56b0, through logfire 3.25). `requirements.sh` and
  `.github/workflows/build.yml` each exported their own set of groups.

## Proposed Solution

One module, `evil_flowers_catalog/telemetry.py`, with an idempotent `setup(role)`. Each process
calls it once, after its last fork. It reads only the standard `OTEL_*` environment. The single
addition is finding the collector when no endpoint is configured.

### Where `setup()` runs

| Process | Call site | Why there |
|---|---|---|
| gunicorn worker | `evil_flowers_catalog/wsgi.py`, before `get_wsgi_application()` | First project code a worker runs after fork and after gevent patching. Read in the installed gunicorn 25.3.0: `GeventWorker.init_process()` calls `self.patch()` (`monkey.patch_all()`), then `hub.reinit()`, then `Worker.init_process()`, which ends in `self.load_wsgi()`. `preload_app` is off, so `load_wsgi()` is the import of `wsgi.py`. The `BatchSpanProcessor` and `PeriodicExportingMetricReader` threads and their `Condition`s are therefore green, in the process that uses them. `DjangoInstrumentor().instrument()` edits `settings.MIDDLEWARE`; the chain is built by `get_wsgi_application()`, so it must come before. `django.setup()` runs first so the setup log lines already use `LOGGING`. |
| Celery prefork child | `worker_process_init` signal | Sent by `celery/concurrency/prefork.py` in each child after fork. Flushed by `worker_process_shutdown`, because children leave through `os._exit()`, which skips `atexit`. |
| Celery main worker process | `worker_init` signal | Discovery only, before the pool forks, so the children inherit the endpoint (or the verdict that there is none). With a non-forking pool (`solo`, `threads`) it also sets up, since tasks run there. |
| Celery beat | `beat_init` signal | Sent from `beat.Service.start()` after Celery installed its own SIGTERM→`SystemExit` handler, so `atexit` flushes. |
| `celery_event_handler` | start of `handle()` | Never forks. Leaves SIGTERM at the default, so `telemetry.exit_on_sigterm()` turns it into `SystemExit` and `atexit` flushes. |

Gunicorn and Celery own SIGTERM, so `setup()` never installs a handler for them. Gunicorn flushes
through the `worker_exit` hook in `conf/gunicorn.conf.py`, which runs in the child. The
settings-import-time Logfire block and the `ready()` hook are removed.

### Resource

`service.name` = `evilflowers` (the Compose service, so traces, Loki's `service_name` and
Pyroscope's `service_name` agree), `service.version` = `VERSION`, `service.instance.id` =
`<hostname>-<pid>`, `evilflowers.process.role` = `gunicorn` | `celery-worker` | `celery-beat` |
`event-handler`. The detectors are `process,os,host,containerid`; the last one gives `container.id`,
so traces correlate with cAdvisor and Docker logs.

`Resource.create(attributes)` merges code attributes *over* the environment detector. Defaults
for keys the operator already put in `OTEL_SERVICE_NAME` / `OTEL_RESOURCE_ATTRIBUTES` are
therefore left out, so the environment always wins.

### Providers

- `TracerProvider` with `BatchSpanProcessor` and the OTLP/HTTP span exporter. Sampler and
  propagators are the SDK defaults: `parentbased_always_on` and `tracecontext,baggage`.
- `MeterProvider` with `PeriodicExportingMetricReader` and the OTLP/HTTP metric exporter.
  Temporality is cumulative (the exporter default, which Mimir expects). The exemplar filter is
  the SDK default `trace_based`, so histograms carry trace exemplars.
- No log exporter. Logs reach Loki through stdout; exporting them too would duplicate every line.
- `OTEL_TRACES_EXPORTER` / `OTEL_METRICS_EXPORTER` accept `otlp` (default), `console` and `none`.
  Without an endpoint `otlp` is dropped, because the exporter would otherwise retry against
  `localhost:4318` forever. Console exporters still work, which is how verification runs without a
  collector.

### Off switch

Telemetry is off, and the process behaves as before, when `OTEL_SDK_DISABLED=true`, or when no
endpoint is configured or discovered and no console exporter was asked for. The application code
calls only the OpenTelemetry API (a base dependency), which is a no-op without a provider.

### Instrumentation

All from `opentelemetry-python-contrib`, all on one release train:

- **Django**: SERVER span `"{method} {route}"`, `http.server.request.duration` in seconds with
  `http.route` (`OTEL_SEMCONV_STABILITY_OPT_IN=http`). Django route strings have no leading
  slash, so the span is `GET api/v1/catalogs/<uuid:catalog_id>/entries/<uuid:entry_id>`, not
  `GET /api/v1/...`. A request that resolves no route is named after its method alone.
- **Healthcheck**: `/api/v1/status` is excluded (`OTEL_PYTHON_DJANGO_EXCLUDED_URLS`), because the
  Docker HEALTHCHECK hits it every 30 s. Verification showed that excluding the URL only drops
  the server span. The view's four `COUNT(*)` queries were still exported, each as a root trace
  of its own. `SuppressExcludedUrlsMiddleware`, which `instrument_django()` inserts outermost,
  runs excluded URLs under `suppress_instrumentation()`, so the healthcheck produces no
  telemetry at all.
- **Event handler**: each Celery event `celery_event_handler` records gets one INTERNAL span,
  `celery.event task-*`, so its `SELECT`/`UPDATE`s form one trace instead of root spans.
- **psycopg (v3), redis**: CLIENT spans. `OTEL_SEMCONV_STABILITY_OPT_IN` also carries `database`.
  The `http` value alone leaves dbapi and redis on the old `db.system` / `net.peer.name` names,
  and Tempo's service graph keys virtual nodes on `db.system.name` / `server.address` (read in
  `opentelemetry/instrumentation/dbapi/__init__.py` and `_semconv.py`, 0.66b2).
- **Celery**: PRODUCER in the sender and CONSUMER in the worker; the context travels in the task
  headers. Instrumented in every role, because the gunicorn worker is the producer.
- **requests**: Readium LCP/LSD, Dataverse, the search and text services. `url.full` goes through
  `redact_url`, so the `user:password@` in `EVILFLOWERS_READIUM_LCPSV_URL` is not exported.
- **kafka-python**: only when `EVILFLOWERS_EVENT_BROKER_EXECUTOR` is the Kafka executor. The
  package lives in the `kafka` group.
- **Header capture**: an allow-list only, `mcp-protocol-version,accept-language` on server
  requests. A sanitise list (`authorization`, `cookie`, `*token*`, …) guards against an operator
  widening it.
- **Process metrics** (`opentelemetry-instrumentation-system-metrics`): only `process.cpu.time`,
  `process.memory.usage`, `process.thread.count`, `process.open_file_descriptor.count` and
  `cpython.gc.collections`. cAdvisor already has container CPU and memory, and Pyroscope has CPU
  profiles, but neither can split one container into the gunicorn workers, the Celery children
  and beat. That split is what these add, together with GC. All `system.*` metrics stay off.

Every default above is applied with `os.environ.setdefault`, so an explicit operator value wins.

### Hand-made spans and metrics

- **`apps/mcp`**: one INTERNAL span per JSON-RPC message, `"{mcp.method.name} {gen_ai.tool.name}"`,
  and the `mcp.server.operation.duration` histogram. Attributes: `mcp.method.name`,
  `gen_ai.tool.name`, and `error.type`, which is the JSON-RPC error code, or `tool_error` for a
  result with `isError`. Method and tool names come from the client, so names that are not
  registered become `_OTHER`.
- **`apps/assistant/llm.py`**: a CLIENT span `chat {model}` per model call, carrying
  `gen_ai.operation.name`, `gen_ai.provider.name=ollama`, `gen_ai.request.model`,
  `gen_ai.response.model`, `gen_ai.usage.input_tokens` / `output_tokens`, `server.address` /
  `server.port` and `error.type`. It also records the `gen_ai.client.operation.duration` and
  `gen_ai.client.token.usage` histograms, with the semantic-convention bucket boundaries. No
  prompt, completion or document text goes into attributes. The HTTP span underneath is
  suppressed, so the model call appears once in the service graph.
- **Streaming**: the assistant's turn runs while its `StreamingHttpResponse` body streams. That
  happens after the Django middleware ended the server span and detached its context in
  `process_response` (read in `otel_middleware.py`). The view wraps the generator in
  `telemetry.ContextBoundIterator`, which attaches the request's context around each `next()`
  only, so the model call, its tool calls and their queries stay in the request's trace.
- **Domain metrics** (`evil_flowers_catalog/domain_metrics.py`), each recorded on transaction
  commit, all attributes closed sets:
  - `evilflowers.readium.license.transitions{evilflowers.readium.transition, evilflowers.readium.expiry_path}`:
    issued, returned, renewed, revoked, cancelled, expired. For an expiry it also records whether
    the 5-minute sweep or the borrow path expired the loan.
  - `evilflowers.extraction.runs{evilflowers.extraction.outcome}` and
    `evilflowers.extraction.run.duration`.
  - `evilflowers.acquisitions.served{mime, via, delivery}`, counted where bytes are actually
    handed out, never at the redirect.
  - **Not added: OPDS feed render time per feed kind.** Every OPDS view is its own URL route, so
    `http.server.request.duration{http.route}` already answers it one to one.

`apps/api/middleware/exceptions.py` is last in `MIDDLEWARE`, so for
`ProblemDetailException`/`ValidationException` it returns the response itself, and Django never
calls the OTel middleware's `process_exception`. It remains the single place that records handled
exceptions on the server span. Since Q4 it records the exception event every time, but sets
status ERROR only for a 5xx or an exception it does not convert, so a 404 or an expired token is
not an error in Tempo's span metrics
([Q4](#q4-handled-4xx-exceptions-mark-the-server-span-error-medium)).

### Logs Alloy already understands

- `evil_flowers_catalog/log_format.py`, standard library only. `JsonFormatter` writes one object
  per line with `time` (ISO 8601 UTC, milliseconds, `Z`), `level` (upper case), `logger`, `msg`,
  `trace_id` / `span_id` from the current span when it is valid, `exc` for tracebacks, and every
  `extra=` field. An extra field never overwrites those keys. `TextFormatter` is the readable
  alternative.
- `EVILFLOWERS_LOG_FORMAT=json` (default) or `text`. Selected by env, not by `DEBUG`, because
  `evilflowers-dev` may run with development settings and its logs still go to Loki.
- The unused `syslog` handler is removed.
- Celery: a receiver on `setup_logging` stops Celery from replacing the root handlers with its
  plain-text format, so worker and beat log through `settings.LOGGING`.
- `conf/supervisor.conf`: all four programs write to `/dev/fd/1` / `/dev/fd/2` with
  `*_logfile_maxbytes=0`. Supervisord's own log file is `/dev/null`. With `nodaemon=true` it
  already echoes its lines ("exited: celery_worker … not expected") to stdout, and a first
  attempt with `logfile=/dev/stdout` printed every line twice. Those lines are plain text
  (supervisord has no JSON format). Alloy's `stage.json` extracts nothing from them, so they
  carry no `level`.
- Celery runs with `--quiet`, so the multi-line worker and beat banners are gone. The
  `setup_logging` receiver also calls `logging.captureWarnings(True)`, as Django's own logging
  setup does, so warnings such as Celery's root `SecurityWarning` are JSON too.
- The only other plain-text output is `manage.py migrate` in `conf/entrypoint.sh`, before
  supervisord starts.
- **Gunicorn access log: off** (it already was, since nothing set `accesslog`; now explicit).
  The server span, `http.server.request.duration` and Tempo's span metrics carry method, route,
  status and latency. Gunicorn's own error log ("Booting worker", worker timeouts) is JSON through
  `logconfig_dict`. The formatter is loaded by file path, because importing
  `evil_flowers_catalog.log_format` would run the package `__init__`. That would import Celery and
  the Django settings into the master, before gevent patches the workers.

### Autodiscovery on the network

**(a) Zero-config endpoint.** When no `OTEL_EXPORTER_OTLP_*ENDPOINT` is set and
`OTEL_EXPORTER_OTLP_AUTODISCOVER` is not `false`:

1. The host is `HOST_IP` if set (on Kubernetes, the downward API `status.hostIP`), else the IPv4
   default gateway from `/proc/net/route`.
2. One TCP connect to `<host>:4318`, 300 ms timeout.
3. On success, `OTEL_EXPORTER_OTLP_ENDPOINT=http://<host>:4318` (and protocol `http/protobuf`)
   goes into `os.environ` before any exporter is built, and one INFO line names it.
4. On failure, telemetry stays off with one INFO line and no retries.

The same image therefore works on `elvira`, on `fiit` and on any future bridge with no Compose
edit. A marker variable stops a forked child from probing again.

On Kubernetes the pod's default gateway is the CNI (Cilium) router, not the node. That is why
`HOST_IP` comes before the gateway, and an explicit endpoint before both. The downward API is the
Kubernetes way.

Gunicorn workers probe one each (4 probes, in parallel, at most 300 ms each). The master cannot
probe for them: it must not import the project before the workers are patched.

**(b) Fit the collector's own discovery.** No collector change is needed, because:

- `OTEL_SERVICE_NAME` defaults to `evilflowers`, the Compose service name that Loki's
  `service_name` and Pyroscope use.
- The JSON log keys are exactly `level` and `trace_id`.
- Postgres, Redis and HTTP calls produce CLIENT spans with `db.system.name` / `server.address`.
- The process exports OTel itself, so Beyla should skip it (see [Q1](#q1-how-does-beyla-decide-a-process-is-already-instrumented-critical)).

The operator-facing version of this contract is [docs/observability.md](../../observability.md).

### Packaging and config

- Poetry group `otel`: `opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-http` 1.45.2;
  `opentelemetry-instrumentation`, `-django`, `-psycopg`, `-redis`, `-celery`, `-requests`,
  `-system-metrics` and `opentelemetry-resource-detector-containerid` 0.66b2. Exact pins: the
  `1.x` and `0.yb0` halves must move together.
- `opentelemetry-api` 1.45.2 is a base dependency, because the application code uses the API.
- `opentelemetry-instrumentation-kafka-python` 0.66b2 sits in the `kafka` group.
- The `logfire` and `apm` groups (elastic-apm, sentry-sdk, psutil) are removed. `psutil` still
  arrives through the system-metrics instrumentation. `poetry.lock` was regenerated with the
  Poetry that wrote it (2.1.3). The only versions that changed are the OTel packages and the
  removed Logfire, Elastic and Sentry dependencies.
- `requirements.sh` exports `--with otel` instead of `--with logfire`. So does
  `.github/workflows/build.yml`, which builds the production image; it also exported
  `--with logfire` and `--with apm`, and both are gone.
- `compose.yml`: commented `OTEL_*` lines replace the `LOGFIRE_*` block, the `./logs:/var/log`
  mount is gone, and an `otel-lgtm` service (`grafana/otel-lgtm:0.39.0`) runs under
  `profiles: [observability]`.
- `.env.example` documents the `OTEL_*` variables, `OTEL_EXPORTER_OTLP_AUTODISCOVER`, `HOST_IP`,
  the semconv opt-in and `EVILFLOWERS_LOG_FORMAT`.
- `compose.services.yml`: the `apm-server` service is removed, together with its committed
  `secret_token` (see [Q3](#q3-the-committed-apm-server-secret_token-low)). Elasticsearch and
  Kibana stay, because the search service uses Elasticsearch.

### Release train and Django 6

Checked on PyPI on 2026-11-06: the current train is **1.45.2 / 0.66b2** (released 2026-10-29).
Every package used here declares Python 3.10–3.14, so Python 3.13 (the Docker image) is covered.

`opentelemetry-instrumentation-django` 0.66b2 does **not declare Django 6**. It has no
`Framework :: Django` classifier at all, and its `instruments` extra says only `django>=2.0`, so
nothing excludes 6.0 either. Verified rather than assumed: Django 6.0.7 with 0.66b2 in the built
image produces SERVER spans with `http.route` and `http.server.request.duration` (see
[Verification](#verification)). The other instrumented libraries are declared in range:
`celery<6.0,>=4.0` (5.6.3), `redis>=2.6` (6.4.0), `psycopg>=3.1.0` (3.3.4),
`kafka-python<4.0,>=2.0` (2.3.2).

## Implementation Plan

### Phase 1: Telemetry core

- [x] `evil_flowers_catalog/telemetry.py`: `setup(role)`, resource, providers, instrumentations,
      `shutdown()`, `exit_on_sigterm()`
- [x] Collector discovery: `/proc/net/route` parser, TCP probe, `HOST_IP`, marker for children
- [x] Call sites: `wsgi.py`, Celery `worker_init` / `worker_process_init` /
      `worker_process_shutdown` / `beat_init`, `celery_event_handler`
- [x] Gunicorn `worker_exit` flush
- [x] Remove the Logfire settings block and the `CoreConfig.ready()` hook

### Phase 2: Logs

- [x] `log_format.py` (`JsonFormatter`, `TextFormatter`), `EVILFLOWERS_LOG_FORMAT`
- [x] `LOGGING` rewritten, `syslog` handler removed
- [x] Celery `setup_logging` receiver
- [x] `supervisor.conf` to stdout/stderr, supervisord included
- [x] Gunicorn: access log off, error log JSON

### Phase 3: Hand-made telemetry

- [x] MCP span and `mcp.server.operation.duration`
- [x] Assistant LLM CLIENT span and `gen_ai.client.*` histograms
- [x] `ContextBoundIterator` for the assistant's streaming response
- [x] Domain metrics: license transitions, extraction runs, acquisitions served

### Phase 4: Packaging, config, docs

- [x] Poetry `otel` group, lock, `requirements.sh`, `build.yml`
- [x] `compose.yml`, `.env.example`, `compose.services.yml`
- [x] `docs/observability.md`, the catalog-wiki settings page and home page
- [x] Unit tests (`apps/core/tests/test_telemetry.py`)

### Phase 5: Rollout (operator)

- [ ] Operator follow-ups below
- [ ] Deploy to `evilflowers-dev`, check Tempo, Loki and Mimir, then production

## Technical Details

### Files

| File | Change |
|---|---|
| `evil_flowers_catalog/telemetry.py` | new |
| `evil_flowers_catalog/log_format.py` | new |
| `evil_flowers_catalog/domain_metrics.py` | new |
| `evil_flowers_catalog/wsgi.py` | `django.setup()`, `telemetry.setup("gunicorn")`, `instrument_django()` |
| `evil_flowers_catalog/celery.py` | logging and telemetry signals |
| `evil_flowers_catalog/settings/base.py` | `LOGGING`; Logfire, Elastic APM and Sentry removed |
| `apps/core/errors.py` | `to_sentry` removed |
| `apps/api/middleware/exceptions.py` | ERROR status only for 5xx / unconverted exceptions (Q4) |
| `apps/core/apps.py` | Logfire `ready()` hook removed |
| `apps/tasks/management/commands/celery_event_handler.py` | `setup("event-handler")`, SIGTERM |
| `apps/mcp/server.py` | span and histogram around `handle()` |
| `apps/assistant/llm.py`, `apps/assistant/views.py` | GenAI span and metrics; context-bound stream |
| `apps/readium/services/license_service.py`, `apps/readium/tasks.py`, `apps/extraction/services.py`, `apps/files/views.py` | domain metrics |
| `conf/gunicorn.conf.py`, `conf/supervisor.conf` | logging, `worker_exit` |
| `pyproject.toml`, `poetry.lock`, `requirements.sh`, `.github/workflows/build.yml` | packaging |
| `compose.yml`, `compose.services.yml`, `.env.example` | config |
| `docs/observability.md`, `docs/catalog-wiki/*` | docs |

### `service.instance.id` and the SDK's own detector

SDK 1.45 always prepends a `service_instance` detector that gives each process a random UUID.
Its providers re-detect it after a fork and overwrite any explicit value. Here the id is
`<hostname>-<pid>`, which matches `ps` output and the Pyroscope process label. It is safe because
`setup()` runs after the last fork of every process.

## Alternatives Considered

- **`opentelemetry-instrument` / `opentelemetry-distro` auto-instrumentation.** It initialises in
  the process that execs Python, which is the gunicorn master and the Celery main process, i.e.
  before the fork, which is the original problem. Making it fork-safe needs the same hooks
  anyway, and collector discovery would still be custom code.
- **Keep Logfire and point it at Alloy.** Logfire's `configure()` can send OTLP elsewhere, but it
  brings its own SDK wrapper, its own pinned OTel train and its own console log format. It adds
  a vendor layer between the app and a stack that speaks plain OTLP.
- **Export logs over OTLP as well.** Rejected: Alloy already reads stdout, so Loki would get every
  line twice. stdout also keeps working when the collector is down.
- **Discover in the gunicorn master once.** Rejected: the master would have to import the project
  package, which imports Celery, the Django settings and `ssl` before gevent patches the workers.
- **Tail sampling.** Not needed at this volume (`parentbased_always_on`).

## Trade-offs and Risks

- **Four probes per gunicorn start.** Each worker probes once, so a laptop without a collector
  logs one "no collector" line per worker (4) plus one for the Celery main process, beat and the
  event handler.
- **`setup()` order is load-bearing.** A future `preload_app = True`, or an import of the project
  from `gunicorn.conf.py`, would move initialisation back before the fork. Both are commented at
  the call sites.
- **Beyla double-tracing** if its "already instrumented" detection does not see this process
  ([Q1](#q1-how-does-beyla-decide-a-process-is-already-instrumented-critical)).
- **Pinned beta contrib packages.** The `0.yb0` instrumentations are beta. Upgrades must move the
  whole train together, and Django 6 support is verified rather than declared.
- **Healthcheck exclusion is a regex.** `api/v1/status` would also exclude any future route that
  contains that string.

## Success Criteria

- [x] With no endpoint and autodiscovery off, gunicorn, the Celery worker and beat start, nothing
      is exported and there are no exporter errors.
- [x] With console exporters: a SERVER span with `http.route`, psycopg and redis CLIENT children,
      and a Celery PRODUCER (gunicorn) and CONSUMER (worker) in the same trace.
- [x] `http.server.request.duration` has exemplars.
- [x] Each gunicorn worker has its own `service.instance.id`.
- [x] 20 concurrent requests: no span has a parent from another request.
- [x] Every application log line is JSON with `level`, and inside a span has the span's
      `trace_id`. Supervisord's own lines and the entrypoint's `migrate` output stay plain text.
- [x] Autodiscovery finds a collector at the bridge gateway, and stays off with one line per
      process tree when there is none.
- [ ] On `evilflowers-dev`: traces in Tempo, the service graph shows Postgres/Redis/readium, Loki
      lines link to traces, Mimir has `http_server_request_duration_seconds` with exemplars.

## Future Considerations

- The sibling containers (`readium`, `evilflowers-lcpencrypt-worker`, the OCR/text/search
  services) come from other repositories. Applying the same contract there (service name, JSON
  logs, OTLP to the gateway) would complete the service graph.
- Dashboards and alerts on the domain metrics (licenses issued per day, extraction failure rate).
- If Grafana's MCP/GenAI views settle on more attributes (`gen_ai.conversation.id`,
  `mcp.session.id`), add those that stay bounded.

## Verification

Run on 2026-11-06 against the image built from this tree (`docker build`, requirements exported
with CI's groups at the time: `s3 pdf apm docker kafka otel`), Python 3.13.18, Django 6.0.7, OTel
1.45.2/0.66b2. The stack was a throwaway Compose project with the image, `pgvector/pgvector:pg16`
and `redis:7`, without the dataverse include or the ML services. The full supervisord process
set ran: 4 gunicorn gevent workers, a Celery prefork worker (10 children), beat and the event
handler.

| Check | Result |
|---|---|
| **Off** (`OTEL_EXPORTER_OTLP_AUTODISCOVER=false`, no endpoint) | All four programs started; `/api/v1/status` 200; a request served. No telemetry line, no exporter error, no traceback. |
| **Autodiscovery, no collector** | 7 lines `no collector answered at http://172.19.0.1:4318; telemetry is off`: 4 gunicorn workers, the Celery main process, beat and the event handler. The 10 Celery children inherited the verdict and stayed silent. The gateway parsed from `/proc/net/route` was correct. |
| **Autodiscovery with a collector** (`grafana/otel-lgtm:0.39.0` publishing 4318) | Every process logged `discovered collector at http://172.19.0.1:4318`, then 16 × `OpenTelemetry on`. Traces arrived in Tempo under `service.name=evilflowers`. In Prometheus, `http_server_request_duration_seconds_bucket` had one series per `service_instance_id` (`<host>-42` … `-45`), `job="elvira/evilflowers"`, `service_namespace="elvira"`, `deployment_environment_name="development"`, and **trace exemplars** on the buckets. |
| **Console exporters**: SERVER span | `POST api/v1/catalogs/<uuid:catalog_id>/entries/<uuid:entry_id>` with `http.route` and `http.response.status_code=201`. |
| psycopg / redis CLIENT children | Children of the SERVER span with `db.system.name=postgresql`, `db.namespace`, `server.address=db`, `server.port=5432`; redis with `db.system.name=redis`, `server.address=redis`. |
| Celery PRODUCER → CONSUMER | `apply_async/apps.core.tasks.store_table_of_contents` (PRODUCER, gunicorn pid 44) and `run/apps.core.tasks.store_table_of_contents` (CONSUMER, Celery child pid 161) share trace `4025456e…`. The worker's psycopg spans are children of the CONSUMER. Beat → worker the same. |
| `http.server.request.duration` exemplars | The point for the upload carried an exemplar with the upload span's trace id. `db.client.operation.duration` (from the `database` opt-in) also had exemplars. |
| `service.instance.id` per process | 4 distinct gunicorn ids, one per Celery child (10), beat and the event handler. The resource also carries `container.id`, `host.*`, `os.*`, `process.*`, `service.version=0.14.0` and the role. |
| **gevent**: 20 concurrent requests | 23 SERVER spans in 23 distinct traces. 0 SERVER spans with a local parent, 0 child spans whose parent is in another trace, 0 orphans. Each of the 21 identical GETs had exactly 14 children. |
| Logs | Every application, gunicorn and Celery line was valid JSON with `level`. The 7 lines emitted inside spans all carried a `trace_id` matching an exported trace. |
| MCP | A batch of `search_entries`, `whoami` and `no/such` gave INTERNAL spans `tools/call search_entries`, `tools/call whoami` and `_OTHER` (`error.type=-32601`, status ERROR) under `POST mcp/v1`. `mcp.server.operation.duration` had the same attributes. |
| Assistant LLM (fake streaming `/api/chat` in the container) | CLIENT `chat qwen2.5:7b` parented to the caller, with `gen_ai.usage.input_tokens=42`, `output_tokens=7` and `server.address`. No HTTP span underneath; the prompt text appears nowhere in the export. An unreachable endpoint gave status ERROR, `error.type=ConnectionError`. Both `gen_ai.client.*` histograms were recorded. |
| `evilflowers.acquisitions.served` | One download → `{mime=application/pdf, via=acquisition, delivery=file}` = 1. |
| Healthcheck suppression (after the fix) | 10 `/api/v1/status` calls produced no trace in Tempo. A control request in the same window did. |
| Unit tests | `apps.core.tests.test_telemetry`: 33 tests OK. |
| Existing tests of the touched apps | `apps.core apps.mcp apps.assistant apps.readium apps.extraction apps.files apps.api apps.opds apps.tasks`: 483 tests OK. A first run without `SECURED_VIEW_JWK` had 15 JWT errors unrelated to this change. |

**Found and fixed during verification**: supervisord lines printed twice; Celery banners and
warnings as free text; the healthcheck's queries exported as root traces.

**Not verified**:

- The assistant streaming through `ContextBoundIterator` inside gunicorn, end to end. It is
  covered by a unit test only, because there is no Ollama here.
- `evilflowers.readium.license.transitions` and `evilflowers.extraction.*`. There is no LCP
  server or extraction services here; they share the code path of the acquisitions counter,
  which was verified.
- Kafka instrumentation (the Kafka executor was not configured).
- The `celery.event task-*` span in `celery_event_handler`. It was added by analogy with the
  healthcheck finding, not observed: the worker ran with task events off (no `-E`), so the event
  handler received no events.
- The `HOST_IP` / Kubernetes path (unit test only).
- Beyla's behaviour (Q1).
- The production Alloy pipeline: Loki `stage.json`, Tempo's service graph and span metrics.
- SIGTERM flush timing for each process.

**Known residual noise**: a few root CLIENT spans per process at start (`PING`, `SADD`, `LLEN`,
`PUBLISH`: broker connection setup outside any request or task), and beat's scheduled
`apply_async/*` PRODUCER traces, which are correct.

## Operator Follow-ups

Nothing here was applied. These live outside this repository (Ansible-managed Compose, Alloy
config).

1. **Production and dev Compose (`evilflowers` service):**
   ```yaml
   OTEL_SERVICE_NAME: evilflowers
   OTEL_RESOURCE_ATTRIBUTES: "service.namespace=elvira,deployment.environment.name=production"  # development on evilflowers-dev
   EVILFLOWERS_LOG_FORMAT: json
   ```
   Optional: `OTEL_EXPORTER_OTLP_ENDPOINT: "http://172.28.0.1:4318"` to skip the probe. The
   `OTEL_SERVICE_NAME` / `OTEL_RESOURCE_ATTRIBUTES` lines are required even with
   autodiscovery: they make the process recognisable as instrumented from its exec-time
   environment (Q1).
2. **Remove any `/var/log` bind mount** for the `evilflowers` service: nothing writes there now.
3. **Drop `LOGFIRE_*`, `ELASTIC_APM_*` and `SENTRY_DSN`** from the deployment environment; none
   is read any more (Q2).
4. **Beyla:** after deploy, confirm the `evilflowers` process no longer appears in Beyla's
   `beyla_*` / `http_server_*` series with Beyla's own `service_name`. If it does, exclude it in
   `beyla.ebpf` discovery (Q1).
5. **Loki level mapping:** the formatter emits Python's level names (`WARNING`, `CRITICAL`).
   Confirm Alloy's normalisation maps `WARNING`→`WARN` and `CRITICAL`→`ERROR`. If it does not,
   add those two cases to the `stage.template` / `stage.replace` that normalises `level`.
6. **Firewall:** OTLP 4318 is allowed from the Docker networks. Nothing to change for `elvira`
   and `fiit`; any new bridge network needs the same rule.

## References

- OpenTelemetry semantic conventions: HTTP, database, GenAI, MCP
- `opentelemetry-python` 1.45.2 / `opentelemetry-python-contrib` 0.66b2 on PyPI
- gunicorn 25.3.0 `workers/ggevent.py`, `workers/base.py`, `arbiter.py`
- Celery 5.6.3 `concurrency/prefork.py`, `beat.py`, `apps/beat.py`
- [docs/observability.md](../../observability.md)

## Review Questions

**Status**: ✅ Answered (2026-11-06; all resolved with the recommended option)
**Review Date**: 2026-11-06
**Reviewer**: Claude AI

The following questions had to be answered before the rollout phase:

---

### Q1: How does Beyla decide a process is already instrumented? 🔴 Critical

**Issue**: `beyla.ebpf` runs with `exclude_otel_instrumented_services` (default on). Whether that
check keys on the process's `OTEL_*` environment or on OTLP traffic it observes is **unverified**.
With autodiscovery the container may have no `OTEL_*` variables at all (the endpoint is set
in-process, after start).

**Context**: One instrumenter per request. If Beyla keeps tracing `evilflowers`, every request
appears twice in Tempo and twice in span metrics, under two service names. On Linux,
`/proc/<pid>/environ` shows the environment at exec time; the endpoint this app writes into
`os.environ` after start (and its `setdefault`s) never appear there. So if Beyla keys on
`OTEL_*` variables, a deployment that relies on autodiscovery alone will *not* look
instrumented. Only variables set in the Compose file will.

**Question**: How should we make sure Beyla skips the catalog?

**Options**:
- [x] **A**: Operator sets `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` in the Compose file
      (follow-up 1), so both detection methods see it. Verify on `evilflowers-dev` (follow-up 4).
      (recommended)
- [ ] **B**: Exclude the `evilflowers` service explicitly in Alloy's Beyla discovery, independent
      of detection.
- [ ] **C**: Both.

**Answer**:
```
A — resolved with the recommendation (jdubec, 2026-11-06).
```

**Resolution**:
```
No code change. Operator follow-up 1 makes the OTEL_* variables in the Compose file mandatory
rather than optional, so /proc/<pid>/environ shows them whatever Beyla keys on. Follow-up 4
(check on evilflowers-dev that Beyla emits no series for the process) stays the acceptance test;
if it fails, fall back to B.
```

---

### Q2: Sentry — remove, or keep as an optional error-reporting extra? ⚠️ Medium

**Issue**: Sentry (`SENTRY_DSN`, `apm` group, `ProblemDetailException(to_sentry=True)` in
`apps/core/errors.py`) is kept, but its tracing is now explicitly off (`traces_sample_rate=None`)
so it cannot start transactions on top of OTel. Logfire and Elastic APM were removed outright.
Logfire was replaced one to one. Elastic APM's `TracingMiddleware` would be a second tracer by
construction, and `ELASTIC_APM_*` was only ever in `.env.example`.

**Context**: Sentry adds issue grouping and e-mail alerts that Grafana does not replicate out of
the box. If no deployment sets `SENTRY_DSN`, it is dead code and a dependency.

**Question**: Is `SENTRY_DSN` set in any deployment?

**Options**:
- [ ] **A**: Keep Sentry as an optional, errors-only extra. (recommended if any deployment uses
      it)
- [x] **B**: Remove Sentry, `to_sentry` and the `apm` group, and alert on errors from Loki/Tempo
      instead. (recommended if no deployment uses it)
- [ ] **C**: Restore Elastic APM as an extra as well. Not recommended: it is a second tracer.

**Answer**:
```
B — remove Sentry, Elastic APM and the rest; OpenTelemetry is the only telemetry (jdubec,
2026-11-06).
```

**Resolution**:
```
Removed: the Sentry block in settings/base.py, the `to_sentry` parameter of
ProblemDetailException (no caller passed it), the `apm` Poetry group (sentry-sdk, psutil; psutil
still arrives through opentelemetry-instrumentation-system-metrics), `--with apm` in
.github/workflows/build.yml, SENTRY_DSN in .env.example and the wiki. poetry.lock re-locked:
sentry-sdk is the only further package removed. Errors are found in Tempo (status ERROR, see Q4)
and Loki (level=ERROR with trace_id). Elasticsearch and Kibana in compose.services.yml and
docs/elk stay: they back the search service, not observability.
```

---

### Q3: The committed APM server `secret_token` ℹ️ Low

**Issue**: `compose.services.yml` committed `apm-server.secret_token=ba2fd8d1-…` (and
`ELASTICSEARCH_PASSWORD=admin`). The `apm-server` service is removed, but the token stays in git
history.

**Context**: Harmless if that APM server only ever ran on a laptop. A secret if the same token
was reused on a shared APM server.

**Question**: Was this token used on any shared server?

**Options**:
- [x] **A**: Local only; nothing to do. (recommended if true)
- [ ] **B**: Rotate it on that server, and consider rewriting history.

**Answer**:
```
A — resolved with the recommendation (jdubec, 2026-11-06).
```

**Resolution**:
```
No action beyond removing the apm-server service. Assumed local-only: in this repository the
token appears only in the local development ecosystem file (compose.services.yml). That was not
checked against the deployments. No history rewrite. Should the token turn up on a shared server,
rotate it there.
```

---

### Q4: Handled 4xx exceptions mark the server span ERROR ⚠️ Medium

**Issue**: `ExceptionMiddleware.process_exception` (originally kept unchanged, as required) called
`span.set_status(ERROR)` for every exception, including `ProblemDetailException`s that become
404, 401, 403 or 422 responses. HTTP semantic conventions leave 4xx SERVER spans unset.

**Context**: Tempo's span metrics count `status_code=STATUS_CODE_ERROR`, so every "entry not
found" and every expired token is an error on the service graph and in error-rate panels. That
inflates the error rate of a public OPDS catalog that crawlers probe.

**Question**: Should the middleware still record the exception event, but set ERROR only for 5xx
and unhandled exceptions?

**Options**:
- [x] **A**: Yes. Record the event always, set ERROR only when `status >= 500` or the exception is
      not a `ProblemDetailException`. One-line change. (recommended)
- [ ] **B**: Keep as is.

**Answer**:
```
A — resolved with the recommendation (jdubec, 2026-11-06).
```

**Resolution**:
```
apps/api/middleware/exceptions.py: record_exception() always; set_status(ERROR) only when the
exception is not a ProblemDetailException or its status is >= 500. It stays the single place
that records handled exceptions on the server span. Covered by
ExceptionMiddlewareSpanStatusTests (404 → UNSET with an exception event, 502 → ERROR,
ValueError → ERROR).
```

---

### Q5: `WARNING`/`CRITICAL` vs Alloy's level normalisation ℹ️ Low

**Issue**: The brief says `level` is normalised to ERROR/WARN/INFO/DEBUG by Alloy, and the
formatter emits Python's names. `WARNING` and `CRITICAL` depend on that normalisation.

**Question**: Emit `WARN`/`ERROR` from the app instead, so the result does not depend on the
Alloy config?

**Options**:
- [x] **A**: Keep Python's names; operator checks the Alloy mapping (follow-up 5). (recommended:
      Python's names are what every other Python service emits)
- [ ] **B**: Map in the formatter.

**Answer**:
```
A — resolved with the recommendation (jdubec, 2026-11-06).
```

**Resolution**:
```
No code change. The formatter keeps Python's level names; operator follow-up 5 checks that Alloy
maps WARNING→WARN and CRITICAL→ERROR.
```

---

### Q6: `service.instance.id` = `<hostname>-<pid>` vs the SDK's UUID ℹ️ Low

**Issue**: SDK 1.45 already gives every process a unique, fork-aware UUID (`service_instance`
detector). This proposal overrides it with `<hostname>-<pid>`, as the brief asked.

**Context**: A PID is reused when supervisord restarts a crashed worker, so a restarted process
can continue the old series. The counter reset is handled by Mimir's `rate()`. A UUID never
collides but means nothing to an operator.

**Question**: Keep `<hostname>-<pid>`?

**Options**:
- [x] **A**: Keep `<hostname>-<pid>`. (recommended: readable, and matches the brief and the sister
      app)
- [ ] **B**: Use the SDK's UUID.

**Answer**:
```
A — resolved with the recommendation (jdubec, 2026-11-06).
```

**Resolution**:
```
No code change; build_resource() keeps <hostname>-<pid>.
```

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-11-06 | jdubec | Initial version with implementation; Status Implemented |
| 2026-11-06 | jdubec | Added Review Questions section |
| 2026-11-06 | jdubec | Verification results; healthcheck suppression and supervisord/Celery log fixes found while verifying; event-handler span added (unverified); Q1 context on `/proc/<pid>/environ` |
| 2026-11-06 | jdubec | Review Questions answered (all recommended options; Q2 = remove Sentry). Sentry, `to_sentry` and the `apm` group removed; ExceptionMiddleware sets ERROR only for 5xx (Q4); Compose `OTEL_*` lines required (Q1) |
