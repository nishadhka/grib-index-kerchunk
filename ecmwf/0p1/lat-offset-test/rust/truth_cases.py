# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "eccodes", "imagecodecs", "pandas", "pyarrow"]
# ///
"""True RSI bit offsets (full read) for the cases written by dump_cases.py, in
the cases' own coordinates (bits from the start of case_NNN.bin):
<out>/truth.json. Verification only.

    GIK_CHUNK_SOURCE=gcs uv run rust/truth_cases.py <test par> <out dir>
"""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "v5"))
from sync_band5 import full, par_refs  # noqa: E402

test, out = par_refs(sys.argv[1]), sys.argv[2]
cases = json.load(open(f"{out}/manifest.json"))
truth = []
for c in cases:
    T = full(*test[(c["field"], c["step"])])
    truth.append([int(b) - c["w0"] * 8 for b in T["offs"]])
json.dump(truth, open(f"{out}/truth.json", "w"))
print(len(truth), "cases")
