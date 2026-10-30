# Where this code comes from

These three files are **verbatim copies** of the GIK 0.1° reader. They are
copied here so that `herbie_vs_gik.py` runs from this folder alone. Do not
edit them here; change them upstream and copy them again.

| | |
|---|---|
| Repository | `cGAN_tutorial` — https://github.com/icpac-igad/cGAN_tutorial |
| Path | `ifs-0p1-gik/` |
| Commit | `bc940871e4327968c0a67a470d6c542c10ab3560` (2026-10-29, "ifs-0p1-gik: SEWAA jurre-6h and mvua-7d products, self-contained, from the public virtual store") |
| Copied | 2026-10-30, with `git show bc94087:ifs-0p1-gik/<file>` |
| Note | On 2026-10-30 this commit existed only in a local checkout; it is to be pushed to icpac-igad/cGAN_tutorial, its permanent home. |

| File | sha256 | Used here for |
|---|---|---|
| `cgan_dag.py` | `313ba554b00d2e5cdbd6ffc6cf95d34ac065b83fbd882a81c21996bc1b51922d` | `read_prefix` (the GIK prefix read, libaec) and `read_virtual` (virtual store, gribberish) |
| `config.py` | `9970873668e684cd4f5b0cba7c3e2fc8e00f4fc1d3aa342fbe146487ad9242bd` | `BOX`, `FIELDS`, `vd_spec` |
| `stores.py` | `4a5976fbef0bacf1b8b084383ad4df6c22d7fe107efb6be789407965346247a3` | `open_realized_dataset` (the realized-store column only) |

`cgan_dag.py` and `stores.py` last changed in `8f950f2`, and `config.py` in
`bc94087`. All three are identical at `bc94087`.

**The full pipeline is still built and tested in `cGAN_tutorial/ifs-0p1-gik`.**
That covers:
- the virtual store;
- realization (`step4_realize.py`, which calls the same `read_prefix`);
- the bit-exact check of every run (`step5_check.py`);
- the SEWAA NetCDF and cGAN inference.

This folder checks one thing only: that the latitude-box subset equals
Herbie's read of the same message.

To check that the copies still match upstream:

```bash
cd gik && for f in cgan_dag.py config.py stores.py; do
  git -C ~/cGAN_tutorial show bc94087:ifs-0p1-gik/$f | cmp - $f && echo "$f identical"; done
```

To test a newer upstream checkout instead of these copies, set
`IFS0P1_GIK=~/cGAN_tutorial/ifs-0p1-gik`.
