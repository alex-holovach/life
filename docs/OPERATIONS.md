# Operating Life

## Layout and commands

The Mac source is `~/projects/life`; the PC installation is
`~/life`, reachable using `ssh YOUR-SERVER` (your configured SSH host).
The previous Mac folder was renamed from `whoop-local`; its private probe data moved with it.
Source is versioned in the private repository `your private Git remote`.
Machine data, credentials, backups and generated builds are excluded from Git.

Run commands from the installation directory. `./life start` starts all configured services;
`status` shows service health and archive/outbox counts; `logs [service]` follows logs;
`restart` restarts the stack; `stop` preserves data; `check` validates Compose configuration;
`test` runs backend checks where uv is installed. `./life expose` configures private Tailscale
Serve HTTPS and refuses to replace unrelated routes or enable public Funnel.

Services: API, remote-write worker, analysis, Prometheus, Grafana, and gateway. Only the gateway at
127.0.0.1:8788 and Prometheus at 127.0.0.1:9090 bind host ports. Tailscale Serve publishes the
gateway at <https://YOUR-TAILSCALE-HOST>. The gateway exposes authenticated batch/status
routes and Grafana; it does not expose the operational `/metrics` endpoint. Grafana has its
own login. API/worker/Prometheus/Grafana run with the installation user's UID/GID.

One-time PC prerequisites are in `scripts/bootstrap_pc.sh`: add the installation user to Docker, enable Docker
at boot, and set the installation user as the Tailscale operator. It prompts for sudo interactively, never for
an SSH password. A new SSH session is needed after group membership changes.

## Credentials and settings

`secrets/ingest_token` is the iPhone ingestion credential. `secrets/grafana_password` initializes
the Grafana administrator password. Files are mode 0600 in a mode 0700 directory and are
mounted as Docker secrets. Changing the initialization file does not rotate an already-created
Grafana account password; use Grafana's account administration for that.

`.env` stores nonsecret machine settings: LIFE_UID, LIFE_GID, PUBLIC_URL, RETENTION_DAYS, and
BACKLOG_DAYS. Defaults are 365 and 30 days. Optional COMPOSE_PROJECT_NAME, GATEWAY_PORT, GRAFANA_ADMIN_USER, and
PROMETHEUS_PORT allow an isolated installation. Retention must be at least the backlog window.
After changing settings, use `./life start`. The command renders `.runtime/prometheus.yml`.

The iPhone endpoint is the exact, nonredirecting HTTPS `/v1/batches` URL. Background URLSession
may follow redirects despite redirect delegate hooks; keep it pointed at this trusted endpoint.
Credentials are stored in the iPhone Keychain after first unlock and are not included in backups
of other devices. Phone SQLite files use protection available after the first unlock.

## Backups and restore

`./life backup` creates live snapshots without stopping services. It snapshots the ingestion
SQLite database first, links its immutable referenced capture archives, snapshots Grafana's
SQLite database, then requests a Prometheus snapshot including its head. Prometheus 3.14's
snapshot omits recent out-of-order head data, so the backup also builds supplemental TSDB
blocks from the durable numeric records marked sent within retention. Identical overlapping
samples preserve their original values and timestamps and are merged by Prometheus.
The full restore test checks both ordinary and delayed samples.

The Prometheus admin API is enabled only on its localhost/Compose-network listener and is not
routed through the gateway. Temporary snapshots are removed after backup; concurrent backups
are rejected. New integration data directories require an explicit snapshot handler.
These are sequential store snapshots, not a global instantaneous snapshot: later Prometheus
samples may be ahead of the earlier ingestion snapshot. Pending samples can safely replay
with identical timestamps and values after restore. No live WAL/SQLite files are copied directly.

Backups are private .tar.gz files under `backups/` and include credentials and personal telemetry. Keep a protected copy off the PC to
survive disk loss. Automatic off-PC backup has not been configured.

To restore: stop Life; preserve the existing installation/data separately; extract the selected
backup into a separate directory; verify `data/ingest/ingest.sqlite` with SQLite's
`PRAGMA integrity_check`; restore the data/config/secrets into an installation using this source
version; check ownership and .env UID/GID; then use `./life start` and `./life status`.
Do not extract an archive over a live database. The acceptance test exercises extraction and
SQLite integrity and a full Prometheus restore with delayed samples in a disposable directory. Full disaster recovery on a replacement PC remains
an operator drill.

## Updates and export failures

Use `git pull --ff-only` from a clean checkout, preserving data, secrets, .env, and backups; run
`./life start`. Docker uses pinned service image versions and a locked Python dependency file.
The command fingerprints mounted configuration so an update recreates services and loads
the new settings; unchanged configuration does not force recreation.
The worker and API share a local image built from `backend/`. The gateway resolves container DNS
again periodically so service recreation does not leave a stale upstream address.

The Grafana location explicitly preserves Host, X-Forwarded-Proto and X-Forwarded-For
alongside its WebSocket headers. Nginx does not inherit a parent's `proxy_set_header`
directives when the location defines its own. Losing Host makes browser cookie sessions
fail with `origin not allowed`, even while Basic-auth API checks succeed. Keep Grafana's
CSRF validation enabled. After a proxy update, run:

```sh
python3 scripts/check_grafana_origin.py --url https://YOUR-TAILSCALE-HOST
```

This logs in using the private password file and tests a real session-cookie dashboard query,
plus rejection of unrelated origins. It never prints the password or session cookie.
Use `./life start` to apply updates: replacing a bind-mounted file can leave a running
container reading the old inode, so reloading Nginx alone may not load the changed file.

An HTTP 204 from `/v1/batches` means the raw archive was fsynced and its SQLite transaction
committed. A separate worker sends samples with original Unix-millisecond timestamps using
Prometheus remote write. Network errors, 429, and 5xx responses remain queued with backoff.
Permanent rejections stay marked `rejected`; because remote write can partially accept a batch,
these outcomes require inspection. No permanent rejection is silently retried or retimestamped.

`historical_import` means the sample is outside the configured late-upload window. `conflicts`
means another value already occupies that series/timestamp. The raw inputs remain available.
There is no automated historical import or correction policy yet. Preserve and review these
records before introducing a versioned calculation or controlled import procedure.

Prometheus has five alert rules for service availability, stale data, recharge, blocked/conflicting
export, and a long-lived pending queue. No external notification destination is configured.
Raw capture archives and phone records have no automatic pruning; check disk space periodically.

References: [Docker restart behavior](https://docs.docker.com/engine/containers/start-containers-automatically/),
[Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve),
[remote write](https://prometheus.io/docs/specs/prw/remote_write_spec/),
[Prometheus storage configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#tsdb).
Proxy references: [Nginx header inheritance](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_set_header),
[Grafana CSRF checks](https://github.com/grafana/grafana/blob/v13.2.1/pkg/middleware/csrf/csrf.go).

## Scoring and sleep data

See [SCORING.md](SCORING.md) for profile setup, manual sleep logging, freshness, and method limits.
The journal and profile are in the ingestion SQLite database and are included in its backup.
