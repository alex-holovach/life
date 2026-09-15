# Life integrations

WHOOP is the first built-in adapter. Ingestion, the archive/outbox, Prometheus, Grafana,
and the top-level `./life` command are shared services. New sources should live in their
own adapter modules under `backend/integrations/`, with explicit validation and bounded
metric names/labels. Do not pass arbitrary metric labels from uploaded packets through.

For a source that needs a separate polling process, add `<name>/compose.yaml` here.
`./life` includes all such compose files (sorted by integration folder name), so
`./life start` starts every configured integration. Each added service should use
`restart: unless-stopped`, persistent data under `data/<name>`, a health check, and secrets
under `secrets/`. No other integrations have been implemented yet.
