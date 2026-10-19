from pathlib import Path
import sys


TEST_SUPPORT_ROOT = Path(__file__).resolve().parents[1] / "tests"
sys.path.insert(0, str(TEST_SUPPORT_ROOT))
