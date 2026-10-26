import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sandbox"))          # hgkit, as the sandbox image puts it on PYTHONPATH
