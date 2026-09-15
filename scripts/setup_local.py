"""Create private credentials and data directories without printing or replacing secrets."""
import os
import secrets
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

def setup():
    os.umask(0o077)
    for name in ("secrets", "data/ingest", "data/prometheus", "data/grafana", "backups", ".runtime"):
        (ROOT / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("ingest_token", "grafana_password"):
        path = ROOT / "secrets" / name
        try: fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError: continue
        with os.fdopen(fd, "w") as output:
            output.write(secrets.token_hex(32) + "\n")
    env = ROOT / ".env"
    if not env.exists():
        env.write_text(f"LIFE_UID={os.getuid()}\nLIFE_GID={os.getgid()}\nRETENTION_DAYS=365\nBACKLOG_DAYS=30\nPUBLIC_URL=http://localhost:8788\nGRAFANA_ADMIN_USER=admin\n")
    print("Life folders and private credentials are ready. Existing values were preserved.")

if __name__ == "__main__": setup()
