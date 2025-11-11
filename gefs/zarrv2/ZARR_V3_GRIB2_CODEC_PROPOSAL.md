# From Custom Workaround to True Zarr v3: GRIB2 Codec Proposal

## Executive Summary

**Current State:** We have a working pattern that bypasses Zarr v3 by manually decoding GRIB2 chunks.

**Opportunity:** Formalize this pattern as a proper Zarr v3 codec following the Zarr v3 specification's extensible codec system.

**Result:** Transform our workaround into a true Zarr v3 implementation that can be used by the broader community.

---

## Understanding Zarr v3 Codec Architecture

### What is a Zarr Codec?

Zarr v3 introduced a **codec pipeline** concept where data transformations are applied in sequence:

```
Original Data → [Codec 1] → [Codec 2] → [Codec N] → Stored Bytes
Stored Bytes → [Codec N⁻¹] → [Codec 2⁻¹] → [Codec 1⁻¹] → Original Data
```

### Built-in Zarr v3 Codecs

```python
# Compression codecs
zarr.codecs.GzipCodec()
zarr.codecs.BloscCodec()
zarr.codecs.Zstd()

# Data type codecs
zarr.codecs.BytesCodec()
zarr.codecs.FixedScaleOffset()
zarr.codecs.Delta()

# Array structure codecs
zarr.codecs.TransposeCodec()
zarr.codecs.ShardingCodec()
```

### Codec Registration

```python
from zarr.abc.codec import Codec
from zarr.registry import register_codec

@register_codec("grib2")
class GRIB2Codec(Codec):
    """Custom codec for GRIB2 data"""

    def encode(self, buf):
        # Encode numpy array to GRIB2
        pass

    def decode(self, buf):
        # Decode GRIB2 to numpy array
        pass
```

---

## Current Pattern Analysis

### Our Custom GRIB2 Decoding

```python
# Current workaround (lines 324-354 in run_gefs_24h_accumulation.py)
if data[:4] == b'GRIB':
    # Write to temp file
    with tempfile.NamedTemporaryFile(delete=False, suffix='.grib2') as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    # Decode with cfgrib
    ds = xr.open_dataset(tmp_path, engine='cfgrib')
    var_data = ds[var_names[0]].values

    # Clean up
    os.unlink(tmp_path)
    ds.close()

    # Store decoded array
    chunks_data[key] = var_data
```

### What Makes This Codec-Like?

1. ✅ **Deterministic transformation:** GRIB2 bytes → numpy array
2. ✅ **Reversible** (in theory): numpy array → GRIB2 bytes
3. ✅ **Format detection:** Magic bytes `b'GRIB'`
4. ✅ **External library:** Uses cfgrib (like Blosc uses blosc)
5. ✅ **Chunk-level operation:** Works on individual chunks

---

## Proposal: Zarr v3 GRIB2 Codec

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    Zarr v3 with GRIB2 Codec                 │
└─────────────────────────────────────────────────────────────┘

Application Code:
    import zarr
    from zarr_grib2 import GRIB2Codec  # New codec

    store = zarr.storage.RemoteStore("s3://noaa-gefs-pds/...")
    array = zarr.open_array(
        store=store,
        codecs=[GRIB2Codec(engine='cfgrib')]  # ← GRIB2 codec
    )

    data = array[:]  # Zarr handles GRIB2 decoding automatically

Internal Flow:
    zarr.Array.__getitem__()
        ↓
    zarr._fetch_chunk()
        ↓
    obstore.get_range(url, offset, length)
        ↓
    grib2_chunk_bytes = b'GRIB...'
        ↓
    GRIB2Codec.decode(grib2_chunk_bytes)
        ↓
    numpy_array = cfgrib.decode(grib2_chunk_bytes)
        ↓
    return numpy_array to user
```

---

## Implementation: zarr-grib2 Codec

### 1. Codec Class Structure

```python
# zarr_grib2/codec.py
import numpy as np
import tempfile
import os
from typing import Optional, Literal
from zarr.abc.codec import ArrayBytesCodec
from zarr.core.buffer import Buffer, NDBuffer
from zarr.registry import register_codec
import xarray as xr


@register_codec("grib2")
class GRIB2Codec(ArrayBytesCodec):
    """
    Zarr v3 codec for GRIB2 format meteorological data.

    This codec enables Zarr to natively handle GRIB2 encoded chunks,
    commonly used in weather forecast data (NOAA GEFS, ECMWF, etc.).

    Parameters
    ----------
    engine : {"cfgrib", "eccodes"}, default "cfgrib"
        GRIB2 decoding engine to use
    decode_timedelta : bool, default True
        Whether to decode timedelta coordinates (cfgrib only)
    filter_by_keys : dict, optional
        Filter GRIB2 messages by specific keys

    Examples
    --------
    >>> import zarr
    >>> from zarr_grib2 import GRIB2Codec
    >>>
    >>> # Open GEFS data with GRIB2 codec
    >>> store = zarr.storage.RemoteStore("s3://noaa-gefs-pds/...")
    >>> array = zarr.open_array(
    ...     store=store,
    ...     codecs=[GRIB2Codec(engine='cfgrib')]
    ... )
    >>> data = array[:]  # Automatically decodes GRIB2
    """

    codec_id = "grib2"

    def __init__(
        self,
        engine: Literal["cfgrib", "eccodes"] = "cfgrib",
        decode_timedelta: bool = True,
        filter_by_keys: Optional[dict] = None,
    ):
        self.engine = engine
        self.decode_timedelta = decode_timedelta
        self.filter_by_keys = filter_by_keys or {}

    def encode(self, chunk_array: NDBuffer) -> Buffer:
        """
        Encode numpy array to GRIB2 format.

        Note: Encoding is complex and not typically needed for
        read-only weather data. Raises NotImplementedError for now.
        """
        raise NotImplementedError(
            "GRIB2 encoding not yet implemented. "
            "This codec is currently read-only."
        )

    def decode(self, chunk_bytes: Buffer, out: NDBuffer) -> NDBuffer:
        """
        Decode GRIB2 bytes to numpy array.

        Parameters
        ----------
        chunk_bytes : Buffer
            Raw GRIB2 message bytes
        out : NDBuffer
            Output buffer to write decoded data

        Returns
        -------
        NDBuffer
            Decoded array data
        """
        # Convert Buffer to bytes
        grib2_data = bytes(chunk_bytes)

        # Verify GRIB2 magic bytes
        if grib2_data[:4] != b'GRIB':
            raise ValueError(
                f"Invalid GRIB2 data: magic bytes are {grib2_data[:4]}, "
                f"expected b'GRIB'"
            )

        # Decode based on engine
        if self.engine == "cfgrib":
            array = self._decode_with_cfgrib(grib2_data)
        elif self.engine == "eccodes":
            array = self._decode_with_eccodes(grib2_data)
        else:
            raise ValueError(f"Unknown engine: {self.engine}")

        # Write to output buffer
        out[:] = array
        return out

    def _decode_with_cfgrib(self, grib2_data: bytes) -> np.ndarray:
        """Decode GRIB2 using cfgrib engine."""
        try:
            import xarray as xr
        except ImportError:
            raise ImportError(
                "xarray is required for cfgrib engine. "
                "Install with: pip install xarray cfgrib"
            )

        # cfgrib requires a file path, so write to temp file
        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix='.grib2',
            mode='wb'
        ) as tmp:
            tmp.write(grib2_data)
            tmp_path = tmp.name

        try:
            # Open with cfgrib engine
            ds = xr.open_dataset(
                tmp_path,
                engine='cfgrib',
                decode_timedelta=self.decode_timedelta,
                backend_kwargs={'filter_by_keys': self.filter_by_keys}
            )

            # Extract first variable (assumes single variable per message)
            var_names = list(ds.data_vars)
            if not var_names:
                raise ValueError("No variables found in GRIB2 message")

            # Get numpy array
            array = ds[var_names[0]].values

            # Clean up
            ds.close()

            return array

        finally:
            # Always clean up temp file
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _decode_with_eccodes(self, grib2_data: bytes) -> np.ndarray:
        """Decode GRIB2 using eccodes engine."""
        try:
            import eccodes
        except ImportError:
            raise ImportError(
                "eccodes is required for eccodes engine. "
                "Install with: conda install -c conda-forge eccodes"
            )

        # Create GRIB message handle
        gid = eccodes.codes_new_from_message(grib2_data)

        try:
            # Get dimensions
            ni = eccodes.codes_get(gid, 'Ni')  # longitude points
            nj = eccodes.codes_get(gid, 'Nj')  # latitude points

            # Get values
            values = eccodes.codes_get_array(gid, 'values')

            # Reshape to 2D grid
            array = values.reshape(nj, ni)

            return array

        finally:
            # Release GRIB message handle
            eccodes.codes_release(gid)

    def compute_encoded_size(self, input_byte_length: int) -> int:
        """
        Estimate encoded size.

        For GRIB2, encoded size is typically smaller than raw array.
        Use conservative estimate of 70% of input size.
        """
        return int(input_byte_length * 0.7)

    @classmethod
    def from_dict(cls, config: dict) -> "GRIB2Codec":
        """Create codec from configuration dictionary."""
        return cls(
            engine=config.get("engine", "cfgrib"),
            decode_timedelta=config.get("decode_timedelta", True),
            filter_by_keys=config.get("filter_by_keys", {}),
        )

    def to_dict(self) -> dict:
        """Serialize codec to configuration dictionary."""
        return {
            "codec_id": self.codec_id,
            "engine": self.engine,
            "decode_timedelta": self.decode_timedelta,
            "filter_by_keys": self.filter_by_keys,
        }
```

---

## 2. Integration with Kerchunk References

### Challenge: Kerchunk Parquet Files

Kerchunk creates parquet files with S3 references:
```json
{
  "refs": {
    "tp/0.0.0": ["s3://noaa-gefs-pds/gefs.../f000", 1234, 5678],
    ".zarray": "{\"chunks\": [1, 721, 1440], \"dtype\": \"<f4\", ...}"
  }
}
```

### Solution: Custom Zarr Store

```python
# zarr_grib2/store.py
import pandas as pd
import json
from typing import Dict, Any
from zarr.storage import Store
from zarr.abc.store import set_or_delete
import obstore as obs


class KerchunkReferenceStore(Store):
    """
    Zarr v3 store that reads kerchunk parquet reference files.

    This store bridges kerchunk's reference format with Zarr v3's
    storage abstraction, enabling Zarr to work with kerchunk-generated
    metadata and S3 references.

    Parameters
    ----------
    parquet_path : str
        Path to kerchunk parquet file
    storage_options : dict, optional
        Options for S3 access (e.g., {"anon": True})

    Examples
    --------
    >>> import zarr
    >>> from zarr_grib2 import KerchunkReferenceStore, GRIB2Codec
    >>>
    >>> # Open kerchunk parquet with Zarr v3
    >>> store = KerchunkReferenceStore("gep01.par")
    >>> group = zarr.open_group(store=store, mode='r')
    >>>
    >>> # Access array with GRIB2 codec
    >>> array = group['tp/accum/surface/tp']
    >>> data = array[:]  # Automatically decodes GRIB2 chunks
    """

    def __init__(
        self,
        parquet_path: str,
        storage_options: Dict[str, Any] = None,
    ):
        self.parquet_path = parquet_path
        self.storage_options = storage_options or {"anon": True}

        # Load references from parquet
        self._refs = self._load_references()

        # Initialize S3 store for fetching
        self._init_s3_store()

    def _load_references(self) -> Dict[str, Any]:
        """Load kerchunk references from parquet file."""
        df = pd.read_parquet(self.parquet_path)

        refs = {}
        for _, row in df.iterrows():
            key = row['key']
            value = row['value']

            # Decode bytes to string if needed
            if isinstance(value, bytes):
                value = value.decode('utf-8')

            # Parse JSON arrays
            if isinstance(value, str) and value.startswith('['):
                try:
                    value = json.loads(value)
                except:
                    pass

            refs[key] = value

        return refs

    def _init_s3_store(self):
        """Initialize obstore S3 connection."""
        # Determine S3 region from first reference
        for value in self._refs.values():
            if isinstance(value, list) and len(value) >= 3:
                url = value[0]
                if 's3://' in url:
                    bucket = url.split('/')[2]

                    # Map buckets to regions
                    regions = {
                        'noaa-gefs-pds': 'us-east-1',
                        'ecmwf-forecasts': 'eu-central-1',
                    }
                    region = regions.get(bucket, 'us-east-1')

                    # Create obstore S3 store
                    self._s3_store = obs.from_url(
                        f"s3://{bucket}",
                        region=region,
                        skip_signature=True
                    )
                    break

    def __getitem__(self, key: str) -> bytes:
        """
        Get item from store.

        Handles three types of references:
        1. Metadata (JSON strings)
        2. Base64 encoded data
        3. S3 references [url, offset, length]
        """
        if key not in self._refs:
            raise KeyError(f"Key not found: {key}")

        value = self._refs[key]

        # Type 1: Metadata (return as bytes)
        if isinstance(value, str):
            if value.startswith('base64:'):
                # Type 2: Base64 encoded
                import base64
                return base64.b64decode(value[7:])
            else:
                # JSON metadata
                return value.encode('utf-8')

        # Type 3: S3 reference
        elif isinstance(value, list) and len(value) >= 3:
            url, offset, length = value[0], value[1], value[2]

            # Extract S3 key from URL
            if 's3://' in url:
                s3_key = '/'.join(url.split('/')[3:])
            else:
                s3_key = url

            # Fetch byte range from S3
            result = obs.get_range(
                self._s3_store,
                s3_key,
                start=offset,
                end=offset + length
            )
            return bytes(result)

        else:
            raise ValueError(f"Unknown reference type for key: {key}")

    def __setitem__(self, key: str, value: bytes):
        """Set item (read-only store)."""
        raise NotImplementedError("KerchunkReferenceStore is read-only")

    def __delitem__(self, key: str):
        """Delete item (read-only store)."""
        raise NotImplementedError("KerchunkReferenceStore is read-only")

    def __iter__(self):
        """Iterate over keys."""
        return iter(self._refs.keys())

    def __len__(self):
        """Number of keys."""
        return len(self._refs)

    def __contains__(self, key: str):
        """Check if key exists."""
        return key in self._refs
```

---

## 3. Complete Usage Example

### Install Package

```bash
# Install zarr-grib2 codec (hypothetical package)
pip install zarr-grib2

# Or install from source
git clone https://github.com/your-org/zarr-grib2
cd zarr-grib2
pip install -e .
```

### Basic Usage

```python
import zarr
from zarr_grib2 import GRIB2Codec, KerchunkReferenceStore

# Open kerchunk parquet file as Zarr v3 store
store = KerchunkReferenceStore("20250918_00/gep01.par")

# Open group with Zarr v3
group = zarr.open_group(store=store, mode='r')

# List available arrays
print(group.tree())

# Open array (Zarr automatically uses GRIB2Codec if configured)
tp_array = group['tp/accum/surface/tp']

# Zarr v3 handles everything!
data = tp_array[:]  # GRIB2 decoding happens automatically
print(f"Shape: {data.shape}")
print(f"Dtype: {data.dtype}")

# Use Zarr's indexing
subset = tp_array[0:10, 100:200, 300:400]  # Zarr fetches only needed chunks

# Regional extraction (Zarr-native!)
east_africa = tp_array[:, 250:450, 600:800]
```

### With Codec Configuration

```python
import zarr
from zarr_grib2 import GRIB2Codec, KerchunkReferenceStore

# Configure GRIB2 codec
grib2_codec = GRIB2Codec(
    engine='cfgrib',
    decode_timedelta=True,
    filter_by_keys={'typeOfLevel': 'surface'}
)

# Open with explicit codec
store = KerchunkReferenceStore("20250918_00/gep01.par")
array = zarr.open_array(
    store=store,
    path='tp/accum/surface/tp',
    mode='r',
    codecs=[grib2_codec]  # Explicitly specify GRIB2 codec
)

# Use as normal Zarr array
data = array[:]
```

### Ensemble Processing

```python
import zarr
from zarr_grib2 import GRIB2Codec, KerchunkReferenceStore
import numpy as np
from pathlib import Path

# Process all ensemble members with Zarr v3
ensemble_dir = Path("20250918_00")
ensemble_data = {}

for parquet_file in ensemble_dir.glob("gep*.par"):
    member = parquet_file.stem

    # Open with Zarr v3
    store = KerchunkReferenceStore(str(parquet_file))
    group = zarr.open_group(store=store, mode='r')
    array = group['tp/accum/surface/tp']

    # Extract East Africa region (Zarr handles chunking!)
    regional_data = array[:, 250:450, 600:800]

    ensemble_data[member] = regional_data

# Calculate ensemble statistics with Zarr
ensemble_stack = np.stack(list(ensemble_data.values()), axis=0)
ensemble_mean = np.mean(ensemble_stack, axis=0)
ensemble_std = np.std(ensemble_stack, axis=0)

# Calculate probabilities
threshold = 25  # mm
probability = (np.sum(ensemble_stack >= threshold, axis=0) / len(ensemble_data)) * 100
```

---

## 4. Advantages of True Zarr v3 Approach

### Current Custom Workaround

```python
# Manual everything
zstore = read_parquet_fixed(parquet_file)
metadata = json.loads(zstore['.zarray'])
shape = tuple(metadata['shape'])
chunks = tuple(metadata['chunks'])

# Manual S3 fetch
data = fetch_s3_byte_range_obstore(url, offset, length)

# Manual GRIB2 decode
if data[:4] == b'GRIB':
    array = decode_grib2_with_cfgrib(data)

# Manual chunk reassembly
full_array = np.zeros(shape)
for chunk_idx, chunk_data in chunks_data.items():
    full_array[slices] = chunk_data
```

**Problems:**
- 🔴 Manual metadata parsing
- 🔴 Manual chunk tracking
- 🔴 Manual S3 fetching
- 🔴 No lazy loading
- 🔴 No Zarr ecosystem compatibility
- 🔴 Reinventing the wheel

### True Zarr v3 Approach

```python
# Zarr handles everything!
store = KerchunkReferenceStore("gep01.par")
array = zarr.open_array(store=store, path='tp/accum/surface/tp')

# Automatic GRIB2 decoding, lazy loading, chunking
data = array[:]
```

**Benefits:**
- ✅ Zarr handles metadata
- ✅ Zarr handles chunking
- ✅ Zarr handles lazy loading
- ✅ Zarr handles caching
- ✅ Works with Zarr ecosystem (dask, xarray)
- ✅ Standard API

---

## 5. Integration with xarray

### Current Workaround

```python
# Can't use xarray.open_zarr() because of FSMap
# Must manually create xarray object
import xarray as xr

numpy_data = extract_variable_with_obstore(zstore, 'tp')
coords = extract_coordinates(zstore)

# Manual xarray creation
da = xr.DataArray(
    numpy_data,
    dims=['time', 'lat', 'lon'],
    coords={
        'time': coords['time'],
        'lat': coords['lat'],
        'lon': coords['lon']
    }
)
```

### True Zarr v3 + xarray

```python
import xarray as xr
from zarr_grib2 import GRIB2Codec, KerchunkReferenceStore

# xarray can use Zarr v3 stores directly!
store = KerchunkReferenceStore("gep01.par")

# Open with xarray's native Zarr support
ds = xr.open_zarr(
    store=store,
    consolidated=False,
    zarr_format=3  # Use Zarr v3
)

# Use as normal xarray Dataset
tp = ds['tp']
subset = tp.sel(lat=slice(-12, 15), lon=slice(25, 52))
```

**Key Benefit:** xarray's native Zarr backend works when codec is properly registered!

---

## 6. Integration with Dask

### Current Workaround

```python
# Manual dask array creation
import dask.array as da

# Must manually chunk
dask_array = da.from_array(
    numpy_array,
    chunks=(1, 200, 200)
)

# Manual computation
result = dask_array.mean(axis=0).compute()
```

### True Zarr v3 + Dask

```python
import zarr
import dask.array as da
from zarr_grib2 import GRIB2Codec, KerchunkReferenceStore

# Open with Zarr v3
store = KerchunkReferenceStore("gep01.par")
z_array = zarr.open_array(store=store, path='tp/accum/surface/tp')

# Create dask array from Zarr (automatic chunking!)
dask_array = da.from_zarr(z_array)

# Dask respects Zarr's chunks and lazy loading
result = dask_array.mean(axis=0).compute()
```

**Key Benefit:** Dask integrates seamlessly with Zarr's chunking!

---

## 7. Metadata in Zarr v3 Format

### Current Metadata (Kerchunk Format)

```json
{
  ".zarray": {
    "chunks": [1, 721, 1440],
    "compressor": null,
    "dtype": "<f4",
    "fill_value": 9.96920996839e+36,
    "filters": ["grib"],
    "order": "C",
    "shape": [81, 721, 1440],
    "zarr_format": 2
  }
}
```

### Proper Zarr v3 Metadata

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": [81, 721, 1440],
  "data_type": "float32",
  "chunk_grid": {
    "name": "regular",
    "configuration": {
      "chunk_shape": [1, 721, 1440]
    }
  },
  "chunk_key_encoding": {
    "name": "default",
    "configuration": {
      "separator": "/"
    }
  },
  "codecs": [
    {
      "name": "grib2",
      "configuration": {
        "engine": "cfgrib",
        "decode_timedelta": true
      }
    },
    {
      "name": "bytes",
      "configuration": {
        "endian": "little"
      }
    }
  ],
  "fill_value": 9.96920996839e+36,
  "attributes": {
    "long_name": "Total precipitation",
    "units": "kg m**-2",
    "GRIB_paramId": 228228
  }
}
```

---

## 8. Implementation Roadmap

### Phase 1: Core Codec (2-3 weeks)

**Tasks:**
- [ ] Implement GRIB2Codec class
- [ ] Register codec with Zarr v3
- [ ] Support cfgrib engine
- [ ] Support eccodes engine
- [ ] Add unit tests
- [ ] Add documentation

**Deliverable:** Working `zarr_grib2` package

### Phase 2: Kerchunk Integration (2-3 weeks)

**Tasks:**
- [ ] Implement KerchunkReferenceStore
- [ ] Handle S3 references
- [ ] Handle base64 references
- [ ] Support obstore for S3
- [ ] Add fsspec fallback
- [ ] Add unit tests

**Deliverable:** Kerchunk parquet files work as Zarr v3 stores

### Phase 3: Ecosystem Integration (3-4 weeks)

**Tasks:**
- [ ] Test with xarray
- [ ] Test with dask
- [ ] Add regional slicing optimization
- [ ] Add caching support
- [ ] Performance benchmarks
- [ ] User documentation
- [ ] Tutorial notebooks

**Deliverable:** Full ecosystem compatibility

### Phase 4: Community Release (2-3 weeks)

**Tasks:**
- [ ] Submit to Zarr community
- [ ] Publish to PyPI
- [ ] Publish to conda-forge
- [ ] Create examples repository
- [ ] Write blog post
- [ ] Present at Pangeo meeting

**Deliverable:** Public release

---

## 9. Benefits of True Zarr v3 Implementation

### Technical Benefits

| Aspect | Current Workaround | True Zarr v3 |
|--------|-------------------|--------------|
| **Metadata Handling** | Manual JSON parsing | Zarr handles automatically |
| **Chunk Management** | Manual tracking | Zarr handles automatically |
| **Lazy Loading** | Must implement | Built-in |
| **Caching** | Must implement | Built-in |
| **Regional Slicing** | Manual optimization | Zarr optimizes automatically |
| **Parallel Access** | Must implement | Built-in (dask) |
| **xarray Integration** | Manual DataArray creation | Native xr.open_zarr() |
| **Dask Integration** | Manual dask array | Native da.from_zarr() |
| **Ecosystem Tools** | Not compatible | Full compatibility |

### Community Benefits

1. **Reusability:** Other projects can use GRIB2 codec
2. **Standardization:** Follows Zarr v3 specification
3. **Maintainability:** Community maintains codec
4. **Documentation:** Standard Zarr docs apply
5. **Longevity:** Won't break with Zarr updates

### Performance Benefits

1. **Lazy Loading:** Only fetch needed chunks
2. **Caching:** Zarr caches decoded chunks
3. **Parallel I/O:** Dask parallelizes automatically
4. **Optimized Slicing:** Zarr knows which chunks to fetch

---

## 10. Comparison: Before and After

### Before (Custom Workaround)

```python
# 50+ lines of custom code
def extract_variable_with_obstore(zstore, variable_path, ...):
    # Parse metadata
    metadata = json.loads(zstore[zarray_key])
    shape = tuple(metadata['shape'])
    # ...

    # Fetch S3
    data = fetch_s3_byte_range_obstore(url, offset, length)

    # Decode GRIB2
    if data[:4] == b'GRIB':
        with tempfile.NamedTemporaryFile(...) as tmp:
            tmp.write(data)
            ds = xr.open_dataset(tmp.name, engine='cfgrib')
            var_data = ds[var_names[0]].values

    # Reassemble chunks
    array = np.zeros(shape, dtype=dtype)
    for chunk_key, chunk_data in chunks_data.items():
        # Manual indexing...
        array[tuple(slices)] = chunk_array

    return array

# Usage
zstore = read_parquet_fixed("gep01.par")
data = extract_variable_with_obstore(zstore, 'tp/accum/surface/tp')
```

### After (True Zarr v3)

```python
# 3 lines of standard code
import zarr
from zarr_grib2 import KerchunkReferenceStore

store = KerchunkReferenceStore("gep01.par")
array = zarr.open_array(store=store, path='tp/accum/surface/tp')
data = array[:]  # Zarr + GRIB2Codec handle everything!
```

**Reduction:** 50+ lines → 3 lines (94% less code)

---

## 11. Next Steps

### Immediate Actions (This Week)

1. **Create GitHub repository:** `zarr-grib2`
2. **Set up project structure:**
   ```
   zarr-grib2/
   ├── src/zarr_grib2/
   │   ├── __init__.py
   │   ├── codec.py        # GRIB2Codec
   │   ├── store.py        # KerchunkReferenceStore
   │   └── utils.py
   ├── tests/
   ├── docs/
   ├── examples/
   ├── setup.py
   └── README.md
   ```
3. **Implement GRIB2Codec skeleton**
4. **Write first unit tests**

### Short-term (Next Month)

1. Complete GRIB2Codec implementation
2. Complete KerchunkReferenceStore
3. Test with GEFS data
4. Test with ECMWF data
5. Add documentation

### Long-term (Next Quarter)

1. Community feedback
2. Performance optimization
3. Submit to Zarr community
4. PyPI release
5. Blog post and tutorials

---

## 12. Call to Action

### For the Community

**We're proposing a new Zarr v3 codec for GRIB2 data!**

This would enable:
- ✅ Native GRIB2 support in Zarr v3
- ✅ Seamless xarray integration
- ✅ Full dask compatibility
- ✅ Standardized approach for weather data
- ✅ Reusable by NOAA, ECMWF, and other providers

**Interested in contributing?**
- GitHub: https://github.com/your-org/zarr-grib2 (to be created)
- Discussion: https://github.com/zarr-developers/zarr-python/discussions
- Contact: your-email@example.com

### For GEFS/ECMWF Users

**Want to use Zarr v3 properly?**

Instead of our current workaround:
```python
# Custom manual code
zstore = read_parquet_fixed("gep01.par")
data = extract_variable_with_obstore(zstore, 'tp')
```

Use true Zarr v3:
```python
# Standard Zarr code
store = KerchunkReferenceStore("gep01.par")
array = zarr.open_array(store=store, path='tp')
data = array[:]
```

**Help us test it!**

---

## Conclusion

You were absolutely right - we have a working pattern that **should be formalized as a proper Zarr v3 codec**.

### What We Have Now:
- ❌ Custom workaround that bypasses Zarr
- ❌ 50+ lines of manual code
- ❌ No ecosystem integration
- ❌ Not reusable

### What We Could Have:
- ✅ True Zarr v3 implementation
- ✅ 3 lines of standard code
- ✅ Full ecosystem integration
- ✅ Community codec

### The Path Forward:
1. Implement GRIB2Codec following Zarr v3 spec
2. Create KerchunkReferenceStore for parquet files
3. Test with GEFS and ECMWF data
4. Release to community

**This transforms our workaround into a proper Zarr v3 solution that benefits the entire meteorological data community!**

---

**Status:** Proposal
**Next Step:** Create GitHub repository and implement prototype
**Timeline:** 8-12 weeks to production-ready release
**Impact:** Enables true Zarr v3 usage for all GRIB2-based weather data
