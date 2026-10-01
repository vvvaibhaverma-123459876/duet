import sys
from pathlib import Path

# Test helpers shared across test directories (e.g. exeshim).
sys.path.insert(0, str(Path(__file__).resolve().parent))
