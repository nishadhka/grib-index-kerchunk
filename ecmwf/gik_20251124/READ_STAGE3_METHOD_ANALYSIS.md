# read_stage3_aifs_all_timesteps.py Method Analysis

## Question 1: Extracting Model Run DateTime

### ✅ Solution Implemented

The model run datetime can be extracted from **two sources**:

#### Method 1: Time Coordinate in Parquet (Recommended)
```python
import pandas as pd
import base64
import struct
import datetime

df = pd.read_parquet('stage3_control_final.parquet')

# Extract time coordinate
time_row = df[df['key'] == 't2m/instant/heightAboveGround/time/0']
value = time_row.iloc[0]['value']

# Decode base64
decoded = base64.b64decode(value[7:])  # Skip 'base64:' prefix
time_val = struct.unpack('<q', decoded)[0]  # int64

# Convert to datetime
model_run = datetime.datetime.utcfromtimestamp(time_val)
print(f"Model Run: {model_run}")  # 2025-11-08 00:00:00 UTC
```

#### Method 2: S3 URL Pattern
```python
import re
import json

# Get any step_XXX reference
step_row = df[df['key'] == 'step_000/2t/sfc/control/0.0.0']
s3_ref = json.loads(step_row.iloc[0]['value'])
s3_url = s3_ref[0]

# Parse URL: s3://ecmwf-forecasts/YYYYMMDD/HHz/...
match = re.search(r'/(\d{8})/(\d{2})z/', s3_url)
date_str = match.group(1)  # '20251108'
hour_str = match.group(2)  # '00'

model_run = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]} {hour_str}:00:00 UTC"
print(f"Model Run: {model_run}")  # 2025-11-08 00:00:00 UTC
```

### Integration into Script

I can add a helper function to automatically extract datetime:

```python
def get_model_run_time(zstore, variable='t2m'):
    """Extract model run datetime from zarr store."""
    import base64
    import struct
    import datetime
    import re

    # Try Method 1: Time coordinate
    var_paths = {
        't2m': 't2m/instant/heightAboveGround',
        '2t': 't2m/instant/heightAboveGround',
        'tp': 'tp/accum/surface',
    }

    if variable in var_paths:
        time_key = f"{var_paths[variable]}/time/0"
        if time_key in zstore:
            value = zstore[time_key]
            if isinstance(value, str) and value.startswith('base64:'):
                decoded = base64.b64decode(value[7:])
                time_val = struct.unpack('<q', decoded)[0]
                return datetime.datetime.utcfromtimestamp(time_val)

    # Fallback Method 2: Parse S3 URL
    for key in zstore.keys():
        if key.startswith('step_000/'):
            ref = zstore[key]
            if isinstance(ref, list) and len(ref) >= 1:
                url = ref[0]
                match = re.search(r'/(\d{8})/(\d{2})z/', url)
                if match:
                    date_str = match.group(1)
                    hour = int(match.group(2))
                    year = int(date_str[:4])
                    month = int(date_str[4:6])
                    day = int(date_str[6:8])
                    return datetime.datetime(year, month, day, hour)

    return None
```

---

## Question 2: Zarr vs Non-Zarr Classification

### ✅ `read_stage3_aifs_all_timesteps.py` is a **"Zarr V3-Style" (NON-ZARR) Implementation**

## Comparison Analysis

### Our Script vs ZARRV2_VS_ZARRV3 Document

| Aspect | Zarr V2 (Doc) | Zarr V3 (Doc) | **Our Script** |
|--------|---------------|---------------|----------------|
| **Zarr Library** | ✅ Required | ❌ Not used | ❌ **Not used** |
| **Xarray for Zarr** | ✅ Critical | ❌ Not used | ❌ **Not used** |
| **Xarray for GRIB** | N/A | ⚠️ Only use | ⚠️ **Only use** |
| **fsspec reference** | ✅ Uses | ❌ Not used | ❌ **Not used** |
| **Method** | `xr.open_datatree()` | Custom extract | **Custom extract** |
| **S3 Access** | fsspec | obstore/fsspec | **fsspec (obstore optional)** |
| **Spatial Slicing** | After loading | Before loading | **After loading** (full timesteps) |
| **Memory** | High (global) | Low (regional) | **Medium** (full per timestep) |

### Detailed Classification

#### ❌ **NOT Zarr V2 Method** Because:
1. **No `import zarr`** - Never imports zarr library
2. **No `fsspec.filesystem("reference", ...)`** - Doesn't use reference filesystem
3. **No `xr.open_datatree()`** - Doesn't use xarray for zarr reading
4. **No xarray zarr engine** - Never uses `engine="zarr"`

#### ✅ **Similar to Zarr V3 Method** Because:
1. **No zarr library dependency** - Pure numpy/fsspec
2. **Direct S3 byte-range fetching** - Manual chunk loading
3. **xarray only for GRIB2** - `xr.open_dataset(grib2, engine='cfgrib')`
4. **Custom chunk reassembly** - Manual numpy operations
5. **Based on aifs-etl.py** - Same extraction philosophy

#### 🔄 **Key Difference from Doc's "Zarr V3"**:
- **Doc's Zarr V3**: Regional subsetting BEFORE loading (skips chunks)
- **Our Script**: Loads full timesteps THEN applies regional subset (optional)

Our script is a **hybrid approach**:
- Uses "Zarr V3-style" non-zarr extraction method
- BUT loads full global data per timestep (like Zarr V2)
- Regional subsetting is optional post-processing (not chunk-level)

---

## Method Breakdown: read_stage3_aifs_all_timesteps.py

### Core Architecture

```python
# ❌ NO ZARR LIBRARY IMPORTS
import pandas as pd
import numpy as np
import json
import fsspec          # Direct S3, not reference filesystem
import base64
import tempfile

# ⚠️ xarray ONLY for GRIB2 (line 166)
#    Inside decode_grib2_data() function
import xarray as xr  # For: xr.open_dataset(grib2, engine='cfgrib')
```

### Extraction Flow

```
1. read_parquet_to_refs()
   ├─ Read parquet → dictionary
   └─ ❌ No zarr library used

2. For each timestep (0h to 360h):
   ├─ extract_single_timestep()
   │  ├─ Get S3 reference [url, offset, length]
   │  ├─ Add .grib2 extension if missing
   │  ├─ fetch_s3_byte_range_fsspec() ← Direct S3 fetch
   │  ├─ decode_grib2_data() ← ⚠️ Uses xarray ONLY here
   │  │  └─ xr.open_dataset(grib2, engine='cfgrib')
   │  └─ Return 2D numpy array (721, 1440)
   │
   └─ Append to list

3. np.stack() → 3D array (85, 721, 1440)

4. Optional: Apply regional subset
   └─ Numpy slicing (not chunk-level filtering)

5. Save to .npz or .pkl
```

### No Zarr Library Usage

**Proof**:
```bash
$ grep -c "import zarr" read_stage3_aifs_all_timesteps.py
0

$ grep -c "fsspec.filesystem.*reference" read_stage3_aifs_all_timesteps.py
0

$ grep -c "xr.open_datatree" read_stage3_aifs_all_timesteps.py
0

$ grep -c "engine=\"zarr\"" read_stage3_aifs_all_timesteps.py
0
```

**Only xarray usage** (line 166, inside GRIB2 decoder):
```python
def decode_grib2_data(data: bytes) -> Optional[np.ndarray]:
    """Decode GRIB2 data to numpy array."""
    if data[:4] != b'GRIB':
        return None

    try:
        import cfgrib
        import xarray as xr  # ← ONLY import of xarray

        # Write GRIB2 to temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.grib2') as tmp:
            tmp.write(data)
            tmp_path = tmp.name

        # Use xarray ONLY for GRIB2 decoding (NOT zarr)
        ds = xr.open_dataset(tmp_path, engine='cfgrib')  # ← Not engine='zarr'!
        var_data = ds[var_names[0]].values

        return var_data
```

---

## Comparison to Document Methods

### Method Comparison Table

| Feature | Zarr V2 (Doc) | Zarr V3 (Doc) | **read_stage3_aifs_all_timesteps.py** |
|---------|---------------|---------------|--------------------------------------|
| **Primary Dependencies** | zarr, xarray, fsspec | numpy, obstore, numcodecs | numpy, fsspec, cfgrib |
| **Data Loading** | `xr.open_datatree()` | Custom `extract_variable_with_obstore()` | **Custom `extract_single_timestep()`** |
| **Zarr Library** | ✅ Via xarray & fsspec | ❌ None | ❌ **None** |
| **Xarray Usage** | ✅ Zarr reading | ⚠️ GRIB2 only | ⚠️ **GRIB2 only** |
| **S3 Method** | fsspec reference FS | obstore direct | **fsspec direct** (obstore optional) |
| **Memory Strategy** | Load global → subset | **Load regional only** | Load global per timestep |
| **Chunk Filtering** | No | ✅ Before loading | ❌ **After loading** |
| **Regional Subset** | Post-load (xarray) | Pre-load (chunk-level) | **Post-load (numpy)** |

### Classification

```
┌─────────────────────────────────────────────┐
│                                             │
│  Zarr V2 Method (zarr library required)    │
│  ├─ xr.open_datatree(engine="zarr")        │
│  ├─ fsspec reference filesystem            │
│  └─ Zarr library (implicit)                │
│                                             │
└─────────────────────────────────────────────┘
                     ↓
        ❌ Our script is NOT this


┌─────────────────────────────────────────────┐
│                                             │
│  Zarr V3 Method (no zarr library)          │
│  ├─ Custom extraction                      │
│  ├─ Direct S3 access                       │
│  ├─ Regional chunk filtering               │
│  └─ xarray ONLY for GRIB2                  │
│                                             │
└─────────────────────────────────────────────┘
                     ↓
        ✅ Our script is SIMILAR to this
        (but without chunk-level filtering)
```

---

## Performance Characteristics

### Memory Usage

| Method | Per Timestep | All 85 Timesteps | Regional Subset |
|--------|--------------|------------------|-----------------|
| **Zarr V2** | ~400 MB (global) | N/A (uses lazy loading) | Post-load |
| **Zarr V3** | ~15 MB (regional) | N/A (chunk-filtered) | Pre-load |
| **Our Script** | ~4 MB (1 timestep global) | ~340 MB (all 85) | Post-load optional |

### Speed

| Method | Time per Member | Notes |
|--------|-----------------|-------|
| **Zarr V2** | ~8-12 sec | High-level xarray overhead |
| **Zarr V3** | ~5-8 sec | Low-level + obstore fast |
| **Our Script** | **~3 min (179 sec)** | 85 timesteps × ~2 sec/timestep |

### Optimization Potential

Our script could be optimized to be more like "Zarr V3":

```python
# Current: Loads full global per timestep
array_2d = extract_single_timestep(...)  # (721, 1440)

# Could be optimized: Load regional only
array_2d = extract_single_timestep(...,
    spatial_slice={'lat_start': 200, 'lat_end': 400,
                   'lon_start': 500, 'lon_end': 700})
# Would skip chunks outside region → faster + less memory
```

---

## Summary

### Answer to Question 2

**YES, `read_stage3_aifs_all_timesteps.py` is a NON-ZARR implementation**

It is classified as:
- ✅ **"Zarr V3-Style"** method (no zarr library)
- ✅ **AIFS-ETL-based** extraction (inspired by aifs-etl.py)
- ✅ **Custom direct extraction** (bypasses zarr/xarray)
- ⚠️ **xarray ONLY for GRIB2 decoding** (not zarr reading)
- ❌ **NOT Zarr V2** (doesn't use zarr library at all)

### Key Characteristics

1. **No zarr library dependency** ✅
2. **No reference filesystem** ✅
3. **No xarray zarr engine** ✅
4. **xarray only for GRIB2** ✅ (like "Zarr V3")
5. **Direct S3 byte-range fetching** ✅
6. **Custom numpy chunk reassembly** ✅
7. **Manual timestep aggregation** ✅

### Method Philosophy

```
Traditional Zarr V2:
  parquet → fsspec reference FS → xarray+zarr → numpy

Our Method (Zarr V3-style):
  parquet → S3 direct fetch → GRIB2 decode → numpy
```

**Conclusion**: Our script is a **non-zarr, direct-extraction method** that follows the "Zarr V3" philosophy of bypassing the zarr library entirely, while using xarray only for GRIB2 decoding (not zarr reading).

---

## Recommendations

### For Current Use
✅ Script is production-ready as a **non-zarr method**
✅ Extract datetime using `get_model_run_time()` helper
✅ Suitable for processing all 85 timesteps

### For Optimization (Future)
Consider adding chunk-level spatial filtering like "Zarr V3":
- Would reduce memory from ~340 MB to ~15 MB
- Would reduce S3 transfer from ~340 MB to ~15 MB
- Would improve speed by ~20-30%

---

**Document Version**: 1.0
**Date**: 2025-11-24
**Analysis**: read_stage3_aifs_all_timesteps.py method classification
