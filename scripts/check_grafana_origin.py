"""Exercise dashboard queries with browser session cookies through the real gateway."""
import argparse
import http.cookiejar
import json
from pathlib import Path
import urllib.error
import urllib.request
from setup_local import ROOT


def check(url, password_file, username="admin"):
    url = url.rstrip("/")
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def post(path, payload, origin):
        request = urllib.request.Request(url + path, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json", "Origin": origin})
        try:
            with client.open(request, timeout=15) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    status, _ = post("/login", {"user": username, "password": password_file.read_text().strip()}, url)
    assert status == 200, f"Grafana session login returned HTTP {status}"
    # Basic authentication alone skips Grafana's cookie-based CSRF validation.
    query = {"from": "now-5m", "to": "now", "queries": [{
        "refId": "A", "datasource": {"uid": "life-prometheus", "type": "prometheus"},
        "expr": 'up{job="life"}', "instant": True, "range": False,
        "intervalMs": 15000, "maxDataPoints": 100,
    }]}
    status, result = post("/api/ds/query", query, url)
    assert status == 200, f"Same-origin dashboard query returned HTTP {status}: {result}"
    assert result["results"]["A"]["status"] == 200, "Prometheus query failed"
    for origin in ("https://untrusted.example", "http://grafana:3000"):
        status, result = post("/api/ds/query", query, origin)
        assert status == 403 and "origin not allowed" in result, "Mismatched origin was accepted"
    print("PASS: browser-session dashboard query succeeds; mismatched origins remain blocked.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Trusted Life gateway URL")
    parser.add_argument("--password-file", type=Path, default=ROOT / "secrets/grafana_password")
    parser.add_argument("--username", default="admin")
    args = parser.parse_args()
    check(args.url, args.password_file, args.username)
