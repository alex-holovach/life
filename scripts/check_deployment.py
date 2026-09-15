"""Disposable, isolated Compose acceptance test. Never injects into or stops the Life stack."""
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import threading
import urllib.error
import urllib.request
import uuid
ROOT=Path(__file__).resolve().parents[1]

def free_port():
    with socket.socket() as sock: sock.bind(("127.0.0.1",0)); return sock.getsockname()[1]


def main():
    os.umask(0o077)
    (ROOT/"work").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="acceptance-",dir=ROOT/"work") as temp:
        folder=Path(temp)/"life"
        shutil.copytree(ROOT,folder,ignore=shutil.ignore_patterns("work","data","secrets","backups",".runtime",".env",".build",".swiftpm",".venv","__pycache__",".pytest_cache","*.xcodeproj","build",".git"))
        gateway,prometheus=free_port(),free_port()
        while gateway==prometheus: prometheus=free_port()
        project="life-validation-"+uuid.uuid4().hex[:8]
        logpath=ROOT/"work/deployment-check.log"
        with logpath.open("w") as log:
            def run(*args, check=True):
                return subprocess.run(args,cwd=folder,stdout=log,stderr=subprocess.STDOUT,check=check)
            def dc(*args, check=True): return run("docker","compose",*args,check=check)
            def wait_ready():
                deadline=time.monotonic()+120
                while time.monotonic()<deadline:
                    try:
                        if fetch("/health").get("ok"): return
                    except (OSError, ValueError): pass
                    time.sleep(1)
                raise RuntimeError("Validation gateway never became ready")
            def fetch(path, body=None, auth=False, port=None):
                headers={"Content-Type":"application/json"}
                if auth: headers["Authorization"]="Bearer "+(folder/"secrets/ingest_token").read_text().strip()
                request=urllib.request.Request(f"http://127.0.0.1:{port or gateway}"+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
                with urllib.request.urlopen(request,timeout=10) as response:
                    raw=response.read()
                    return json.loads(raw) if raw else response.status
            def count_sent(count):
                deadline=time.monotonic()+90
                while time.monotonic()<deadline:
                    if fetch("/v1/status",auth=True)["samples"].get("sent",0)==count: return
                    time.sleep(1)
                raise RuntimeError("Validation outbox did not drain")
            run("./life","setup")
            with (folder/".env").open("a") as env:
                env.write(f"COMPOSE_PROJECT_NAME={project}\nGATEWAY_PORT={gateway}\nPROMETHEUS_PORT={prometheus}\nPUBLIC_URL=http://127.0.0.1:{gateway}\n")
            try:
                run("./life","start");wait_ready()
                # Git replaces files atomically. The gateway must reload the new
                # inode without silently retaining its original bind-mounted file.
                config = folder/"gateway/default.conf"
                original = config.read_text()
                replacement = config.with_suffix(".next")
                try:
                    replacement.write_text(original.replace("    listen 8080;", "    listen 8080;\n    location = /reload-check { return 204; }"))
                    replacement.replace(config)
                    dc("exec", "-T", "gateway", "nginx", "-t")
                    dc("exec", "-T", "gateway", "nginx", "-s", "reload")
                    deadline = time.monotonic()+5
                    while True:
                        try:
                            assert fetch("/reload-check") == 204
                            break
                        except (OSError, ValueError, AssertionError):
                            if time.monotonic() >= deadline: raise
                            time.sleep(.1)
                finally:
                    replacement.write_text(original); replacement.replace(config)
                    dc("exec", "-T", "gateway", "nginx", "-s", "reload")
                try: fetch("/v1/temperature")
                except urllib.error.HTTPError as error: assert error.code==401
                else: raise AssertionError("Unauthenticated temperature query was accepted")
                assert fetch("/v1/temperature",auth=True)["state"]=="awaiting_reference"
                run("python3", "scripts/check_grafana_origin.py", "--url", f"http://127.0.0.1:{gateway}")
                now=int(time.time())
                def record(at,value):
                    return dict(id=uuid.uuid4().hex,deviceId="validation-strap",sessionId="synthetic",receivedAt=at,
                                sampleAt=at,source="live_hr",rawBase64="AA==",hrBpm=value,quality="live_receipt_timestamp",decoderVersion="synthetic-test")
                first=record(now-60,71)
                try: fetch("/v1/batches",{"captures":[first]})
                except urllib.error.HTTPError as error: assert error.code==401
                else: raise AssertionError("Unauthenticated ingestion was accepted")
                assert fetch("/v1/batches",{"captures":[first]},auth=True)==204
                count_sent(1)
                dc("stop","prometheus")
                earlier=record(now-600,65)
                assert fetch("/v1/batches",{"captures":[earlier]},auth=True)==204
                assert fetch("/v1/status",auth=True)["samples"].get("pending")==1
                # Crash the validation API and worker, exercising their Docker restart policies.
                for service in ("api","worker"):
                    dc("exec","-T",service,"/app/.venv/bin/python","-c","import os,signal; child=int(open('/proc/1/task/1/children').read().split()[0]); os.kill(child,signal.SIGKILL)",check=False)
                time.sleep(2);wait_ready()
                for service in ("api","worker"):
                    container=subprocess.check_output(["docker","compose","ps","-q",service],cwd=folder,text=True).strip()
                    assert int(subprocess.check_output(["docker","inspect","--format","{{.RestartCount}}",container],text=True)) >= 1, service+" did not restart"
                assert fetch("/v1/status",auth=True)["captured_records"]==2
                run("./life","restart");wait_ready();count_sent(2)
                assert fetch("/v1/batches",{"captures":[first,earlier]},auth=True)==204
                assert fetch("/v1/status",auth=True)["captured_records"]==2
                from urllib.parse import urlencode
                result=fetch("/api/v1/query?"+urlencode({"query":'whoop_heart_rate_bpm{device="strap"}[1h]',"time":now}),port=prometheus)
                assert result["data"]["result"][0]["values"]==[[now-600,"65"],[now-60,"71"]]
                failures=[]; probes=[]; stop_probe=threading.Event()
                def probe_backup():
                    while not stop_probe.is_set():
                        for route in ("/health", "/api/health"):
                            try: fetch(route); probes.append(route)
                            except Exception as error: failures.append((route,str(error)))
                        stop_probe.wait(0.1)
                probe=threading.Thread(target=probe_backup); probe.start()
                try: run("./life","backup")
                finally: stop_probe.set(); probe.join()
                assert probes and not failures, "Backup interrupted availability: "+str(failures[:3])
                wait_ready();count_sent(2)
                archive,=list((folder/"backups").glob("*.tar.gz"))
                assert archive.stat().st_mode & 0o777==0o600
                restored=Path(temp)/"restore"
                with tarfile.open(archive) as tar: tar.extractall(restored,filter="data")
                with sqlite3.connect(restored/"data/ingest/ingest.sqlite") as db:
                    assert db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
                    assert db.execute("SELECT count(*) FROM captures").fetchone()[0]==2
                with sqlite3.connect(restored/"data/grafana/grafana.db") as db:
                    assert db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
                assert json.loads((restored/"backup.json").read_text())["method"]=="online"
                assert not list((folder/"data/prometheus/snapshots").iterdir()), "Temporary snapshot leaked"
                # Restore ALL stores only in this disposable validation installation.
                dc("stop", "--timeout", "5")
                shutil.rmtree(folder/"data")
                shutil.copytree(restored/"data", folder/"data")
                run("./life","start");wait_ready();count_sent(2)
                result=fetch("/api/v1/query?"+urlencode({"query":'whoop_heart_rate_bpm{device="strap"}[1h]',"time":now}),port=prometheus)
                assert result["data"]["result"][0]["values"]==[[now-600,"65"],[now-60,"71"]], "Prometheus snapshot lost samples"
                report={"passed":True,"checked":["gateway authentication","atomic gateway configuration replacement and reload","temperature endpoint authentication","Grafana session-cookie queries and origin validation","HTTP acknowledgement during Prometheus outage","API/worker crash recovery","full-stack restart","original timestamps and delayed backfill","duplicate replay","online backup availability and full SQLite/Prometheus restore"],"time":time.time()}
                (ROOT/"work/deployment-check.json").write_text(json.dumps(report,indent=2)+"\n")
                print("PASS: isolated PC deployment, outage/crash recovery, restart, timestamped backfill, deduplication, and backup restore.",flush=True)
            finally:
                dc("down","--remove-orphans",check=False)

if __name__=="__main__": main()
