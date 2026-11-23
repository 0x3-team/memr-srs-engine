"""Load the checkout's engine without admitting the working directory in -I mode."""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
runpy.run_module("memr_srs_engine", run_name="__main__", alter_sys=True)
