# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "eccodes", "imagecodecs", "pandas", "pyarrow"]
# ///
"""One-off: cases written before the dump_cases.py end-of-data fix stop d0
bytes short of the end of the packed data. Append the missing bytes."""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "v5"))
from sync_band5 import get, par_refs, sec5  # noqa: E402

test, out = par_refs(sys.argv[1]), sys.argv[2]
cases = json.load(open(f"{out}/manifest.json"))
for i, c in enumerate(cases):
    key, o, L = test[(c["field"], c["step"])]
    d0 = sec5(get(key, o, o + 2047))["d0"]
    databytes = L - d0
    path = f"{out}/case_{i:03}.bin"
    have = os.path.getsize(path)
    want = databytes - 4 - c["w0"]
    if have < want:
        open(path, "ab").write(get(key, o + d0 + c["w0"] + have, o + d0 + c["w0"] + want - 1))
    c["tail_len"] = want
json.dump(cases, open(f"{out}/manifest.json", "w"))
print("fixed", len(cases))
