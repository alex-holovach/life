"""Synthetic fixtures only. SQLite and archive tests run in isolated temporary directories."""
import subprocess
from pathlib import Path
root = Path(__file__).resolve().parents[1]
subprocess.run(["uv", "run", "--locked", "python", "-m", "pytest", "-q"], cwd=root / "backend", check=True)
