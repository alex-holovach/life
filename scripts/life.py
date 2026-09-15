#!/usr/bin/env python3
"""Life's single entry point. All paths are relative to this installation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
from urllib.parse import urlparse
from setup_local import ROOT, setup


def settings():
    values = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1); values[key] = value
    return values


def render():
    values = settings()
    backlog = int(values.get("BACKLOG_DAYS", "30")); retention = int(values.get("RETENTION_DAYS", "365"))
    if not 1 <= backlog <= retention <= 3650: raise SystemExit("Require 1 <= BACKLOG_DAYS <= RETENTION_DAYS <= 3650")
    (ROOT / ".runtime/prometheus.yml").write_text(
        "global:\n  scrape_interval: 15s\n  evaluation_interval: 30s\n"
        f"storage:\n  tsdb:\n    out_of_order_time_window: {backlog}d\n"
        "rule_files: [/etc/prometheus/rules.yml]\n"
        "scrape_configs:\n  - job_name: life\n    static_configs:\n      - targets: ['api:8000']\n"
        "  - job_name: prometheus\n    static_configs:\n      - targets: ['localhost:9090']\n")


def compose(*args, capture=False, check=True):
    command = ["docker", "compose", "--project-directory", str(ROOT), "--env-file", str(ROOT / ".env"), "-f", str(ROOT / "compose.yaml")]
    for extra in sorted((ROOT / "integrations").glob("*/compose.yaml")): command += ["-f", str(extra)]
    # Bind-mounted configuration changes must recreate services on `life start`.
    # Compose otherwise sees an unchanged mount path and leaves old config loaded.
    digest = hashlib.sha256()
    for directory in (".runtime", "prometheus", "grafana", "gateway", "integrations"):
        for path in sorted((ROOT / directory).rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes())
    environment = dict(os.environ, LIFE_CONFIG_REVISION=digest.hexdigest())
    return subprocess.run(command + list(args), cwd=ROOT, env=environment, text=True, capture_output=capture, check=check)


def docker_ready():
    if subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        raise SystemExit("Docker is unavailable. On your Linux server run scripts/bootstrap_pc.sh once, then reconnect SSH.")


def request(path, *, private=False):
    headers = {}
    if private: headers["Authorization"] = "Bearer " + (ROOT / "secrets/ingest_token").read_text().strip()
    with urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:" + str(int(settings().get("GATEWAY_PORT", "8788"))) + path, headers=headers), timeout=10) as result:
        return json.load(result)


def status():
    compose("ps")
    try:
        data = request("/v1/status", private=True)
        print(json.dumps({"archived_records": data["captured_records"], "prometheus_samples": data["samples"],
                          "conflicting_samples": data["conflicts"], "last_prometheus_success": data["last_prometheus_success"]}, indent=2))
    except Exception as error: print("Ingestion status unavailable: " + str(error), file=sys.stderr)
    print("Dashboard: " + settings().get("PUBLIC_URL", "http://localhost:8788"))
    print("iPhone endpoint: " + settings().get("PUBLIC_URL", "http://localhost:8788").rstrip("/") + "/v1/batches")


def backup():
    from backup import create_backup
    create_backup(ROOT, settings(), compose)


def expose():
    state = json.loads(subprocess.check_output(["tailscale", "serve", "status", "--json"], text=True))
    # Only add to an empty config or reapply our exact existing single endpoint.
    for host, web in state.get("Web", {}).items():
        if not host.endswith(":443") or web.get("Handlers") != {"/": {"Proxy": "http://127.0.0.1:8788"}}:
            raise SystemExit("Tailscale Serve already has other routes. Review them before changing exposure.")
    if any(state.get("AllowFunnel", {}).values()) or any(port != "443" for port in state.get("TCP", {})):
        raise SystemExit("Existing Tailscale exposure requires review; it was not changed.")
    node = json.loads(subprocess.check_output(["tailscale", "status", "--json"], text=True))
    dns = node["Self"]["DNSName"].rstrip(".")
    if not dns.endswith(".ts.net"): raise SystemExit("Expected a Tailscale DNS name")
    subprocess.run(["tailscale", "serve", "--bg", "--https=443", "http://127.0.0.1:8788"], check=True)
    env = ROOT / ".env"
    lines = [line for line in env.read_text().splitlines() if not line.startswith("PUBLIC_URL=")]
    env.write_text("\n".join(lines + ["PUBLIC_URL=https://" + dns]) + "\n")
    compose("up", "-d", "--wait", "--wait-timeout", "180", "grafana", "gateway")
    print("Private Life URL: https://" + dns)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Start and manage all Life integrations")
    parser.add_argument("command", choices=["setup", "start", "stop", "restart", "status", "logs", "backup", "expose", "check", "test"])
    parser.add_argument("services", nargs="*", help="Optional service names for logs")
    args = parser.parse_args()
    if args.command == "setup": setup(); render(); return
    if args.command == "test":
        subprocess.run(["python3", "scripts/check_backend.py"], cwd=ROOT, check=True); return
    if not (ROOT / ".env").exists(): setup()
    render(); docker_ready()
    if args.command == "start":
        compose("up", "-d", "--build", "--wait", "--wait-timeout", "180"); status()
    elif args.command == "stop": compose("stop", "--timeout", "45")
    elif args.command == "restart":
        compose("restart"); compose("up", "-d", "--wait", "--wait-timeout", "180"); status()
    elif args.command == "status": status()
    elif args.command == "logs": compose("logs", "--tail", "100", "-f", *args.services)
    elif args.command == "backup": backup()
    elif args.command == "expose": expose()
    elif args.command == "check":
        compose("config", "--quiet")
        print("Compose configuration is valid.")

if __name__ == "__main__": main()
