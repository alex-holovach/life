"""Exercise original timestamps/backfill against a fresh, disposable real Prometheus."""
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.request
ROOT=Path(__file__).resolve().parents[1]
executable=os.environ.get("PROMETHEUS_BIN")
if not executable: raise SystemExit("Set PROMETHEUS_BIN to a verified Prometheus binary.")
with tempfile.TemporaryDirectory(prefix="life-prometheus-") as temp:
    directory=Path(temp)
    config=directory/"prometheus.yml"
    config.write_text("global:\n  scrape_interval: 15s\nstorage:\n  tsdb:\n    out_of_order_time_window: 30d\nscrape_configs: []\n")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
    base=f"http://127.0.0.1:{port}"
    with (directory/"server.log").open("w+") as log:
        server=subprocess.Popen([executable,"--config.file="+str(config),"--storage.tsdb.path="+str(directory/"tsdb"),
            "--web.listen-address=127.0.0.1:"+str(port),"--web.enable-remote-write-receiver","--storage.tsdb.retention.time=365d"],stdout=log,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+30
            while True:
                try:
                    urllib.request.urlopen(base+"/-/ready",timeout=1).close();break
                except Exception:
                    if server.poll() is not None or time.monotonic()>deadline:
                        log.seek(0);raise RuntimeError(log.read())
                    time.sleep(.2)
            environment=os.environ|{"PROMETHEUS_TEST_URL":base,"PROMETHEUS_WRITE_URL":base+"/api/v1/write"}
            subprocess.run(["uv","run","--locked","python","-m","pytest","-q"],cwd=ROOT/"backend",env=environment,check=True)
        finally:
            server.terminate()
            try: server.wait(timeout=15)
            except subprocess.TimeoutExpired: server.kill();server.wait()
print("PASS: real Prometheus accepted original timestamps, late backfill, and replay after a simulated sender crash.")
