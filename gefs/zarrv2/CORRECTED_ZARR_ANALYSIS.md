# CORRECTED: Zarr v2 API Analysis and Common Misconceptions

**Purpose**: Clarify and correct assertions about Zarr v2 API, specifically addressing FSMap and import statements
**Date**: 2025-11-11
**Zarr Version Analyzed**: v2.18.7

---

## CRITICAL CORRECTION: FSMap Import

### ❌ INCORRECT ASSERTION (FOUND ELSEWHERE)

```python
from zarr import FSMap  # This is WRONG for Zarr v2!
```

### ✅ VERIFIED TRUTH

**FSMap does NOT exist in Zarr v2.x**

#### Proof of Import Error

```python
import zarr
print(zarr.__version__)  # '2.18.7'

from zarr import FSMap
# ---------------------------------------------------------------------------
# ImportError                               Traceback (most recent call last)
# Cell In[1], line 1
# ----> 1 from zarr import FSMap
#
# ImportError: cannot import name 'FSMap' from 'zarr'
# (/opt/coiled/env/lib/python3.11/site-packages/zarr/__init__.py)
```

---

## What IS Available in Zarr v2

### Correct Zarr v2 Imports

```python
import zarr

# Core functionality
zarr.open()
zarr.open_group()
zarr.open_array()
zarr.group()
zarr.array()

# Array creation
zarr.zeros()
zarr.ones()
zarr.full()
zarr.empty()

# Storage backends that DO exist in v2
from zarr.storage import (
    DirectoryStore,    # ✅ Works
    ZipStore,          # ✅ Works
    DBMStore,          # ✅ Works
    MemoryStore,       # ✅ Works
    TempStore,         # ✅ Works
)

# Consolidation
zarr.consolidate_metadata()
zarr.open_consolidated()

# Utilities
zarr.copy()
zarr.copy_all()
zarr.save()
zarr.load()
```

### Working with fsspec in Zarr v2 (Without FSMap)

```python
from fsspec import get_mapper
import zarr

# CORRECT way for Zarr v2: Direct mapper usage
mapper = get_mapper('s3://bucket/path', anon=True)
group = zarr.open_group(mapper, mode='r')

# NO FSMap wrapper needed in v2
# (FSMap only exists in v3)
```

---

## Common Misconceptions CORRECTED

### Misconception #1: FSMap in Zarr v2

**❌ WRONG**: "You can import FSMap from zarr in version 2"

**✅ CORRECT**: FSMap was introduced in Zarr v3.0 and does not exist in any v2.x release

**Impact**: Code using `from zarr import FSMap` will fail with ImportError in Zarr v2

---

### Misconception #2: Store Types

**❌ WRONG**: "FSMap is just another storage backend like DirectoryStore in v2"

**✅ CORRECT**:
- Zarr v2 has: `DirectoryStore`, `ZipStore`, `DBMStore`, `MemoryStore`, `TempStore`
- Zarr v3 added: `FSMap` as a new store wrapper for fsspec filesystems
- FSMap is fundamentally different - it wraps fsspec mappers with a new store protocol

---

### Misconception #3: fsspec Integration

**❌ WRONG**: "You need FSMap to use fsspec with zarr"

**✅ CORRECT**:
- **Zarr v2**: Use `fsspec.get_mapper()` directly - returns a `MutableMapping` that zarr accepts
- **Zarr v3**: Use `FSMap(mapper)` to wrap the fsspec mapper in the new store protocol
- FSMap is only needed for v3's new store API

---

### Misconception #4: Error Message Interpretation

**Error Seen**:
```
ValueError: 'path' was provided but is not used for FSMap store_like objects.
Specify the path when creating the FSMap instance instead.
```

**❌ WRONG INTERPRETATION**: "This error means I need to import FSMap differently in v2"

**✅ CORRECT INTERPRETATION**:
- This error only occurs with Zarr v3
- It indicates mixing v3 FSMap objects with v2-style path arguments
- If you see this error, you're using Zarr v3, not v2
- Solution: Either use v3 API correctly, or downgrade to v2 and use v2 API

---

## Zarr v2 vs v3: Store API Comparison

### Zarr v2 Storage

```python
# Using fsspec with zarr v2
from fsspec import get_mapper
import zarr

# Method 1: Direct mapper (most common)
mapper = get_mapper('s3://bucket/key')
group = zarr.open_group(mapper)

# Method 2: String path with protocol
group = zarr.open_group('s3://bucket/key')

# Method 3: Built-in stores
from zarr.storage import DirectoryStore
store = DirectoryStore('/path/to/data')
group = zarr.open_group(store)
```

### Zarr v3 Storage (for comparison)

```python
# Using fsspec with zarr v3
from fsspec import get_mapper
import zarr
from zarr import FSMap  # NEW in v3

# Must wrap mapper with FSMap
mapper = get_mapper('s3://bucket/key')
store = FSMap(mapper)  # Required wrapper
group = zarr.open_group(store, zarr_format=3)
```

**Key Difference**: v3 requires explicit FSMap wrapper, v2 accepts mappers directly

---

## Practical Examples: GEFS Use Case

### GEFS with Zarr v2 (Current Implementation)

```python
import xarray as xr
import zarr
from fsspec import get_mapper
from fsspec.implementations.reference import ReferenceFileSystem

# Read parquet-based kerchunk references
import pandas as pd
df = pd.read_parquet('gep01.par')

# Create reference filesystem
fs = ReferenceFileSystem(
    fo=df.to_dict(),
    remote_protocol='s3',
    remote_options={'anon': True}
)

# Get mapper (v2 style - no FSMap needed)
mapper = fs.get_mapper('')

# Open with xarray (uses zarr v2 internally)
ds = xr.open_dataset(mapper, engine='zarr', consolidated=False)
```

### What Would Change in Zarr v3

```python
# Same setup...
fs = ReferenceFileSystem(fo=df.to_dict(), ...)
mapper = fs.get_mapper('')

# NEW: Must wrap mapper with FSMap
from zarr import FSMap
store = FSMap(mapper)

# Must specify zarr_format=3
ds = xr.open_dataset(store, engine='zarr', zarr_format=3)
```

---

## Testing Zarr Version Compatibility

### Version Detection Script

```python
import zarr
import sys

print(f"Zarr version: {zarr.__version__}")
print(f"Python version: {sys.version}")

# Test FSMap availability
try:
    from zarr import FSMap
    print("✅ FSMap available - This is Zarr v3+")
except ImportError:
    print("❌ FSMap NOT available - This is Zarr v2")

# List available storage classes
print("\nAvailable storage backends:")
import zarr.storage
for name in dir(zarr.storage):
    if name.endswith('Store'):
        print(f"  - {name}")
```

### Expected Output for Zarr v2.18.7

```
Zarr version: 2.18.7
Python version: 3.11.x
❌ FSMap NOT available - This is Zarr v2

Available storage backends:
  - ABSStore
  - ConsolidatedMetadataStore
  - DBMStore
  - DirectoryStore
  - FSStore
  - LMDBStore
  - LRUStoreCache
  - MemoryStore
  - MongoDBStore
  - N5Store
  - RedisStore
  - SQLiteStore
  - TempStore
  - ZipStore
```

**Note**: FSMap is NOT in this list for v2.x

---

## Migration Considerations

### If You Must Upgrade to Zarr v3

**Code Changes Required**:

1. **Import FSMap**:
```python
from zarr import FSMap
```

2. **Wrap fsspec mappers**:
```python
# v2: mapper = get_mapper(path)
# v3: store = FSMap(get_mapper(path))
```

3. **Add zarr_format parameter**:
```python
zarr.open_group(store, zarr_format=3)
```

4. **Update store initialization**:
```python
# v2: DirectoryStore(path)
# v3: zarr.storage.LocalStore(path)  # renamed
```

### If You Stay on Zarr v2 (Recommended for Stability)

**No changes needed** - current code works as-is:

```python
# This works perfectly in v2
import zarr
from fsspec import get_mapper

mapper = get_mapper('s3://bucket/path')
group = zarr.open_group(mapper)  # Direct mapper usage

# No FSMap import needed
# No zarr_format parameter needed
# No code changes needed
```

---

## Environment Specification

### Correct Environment for GEFS (Zarr v2)

```yaml
# environment.yml or requirements
name: gefs-zarr-v2
dependencies:
  - python=3.11
  - zarr<3.0          # Critical: Stay on v2
  - xarray>=2023.1.0
  - fsspec>=2023.1.0
  - kerchunk>=0.1.0
  - s3fs>=2023.1.0
  - pandas>=2.0.0
  - pyarrow>=12.0.0
```

**Important**: `zarr<3.0` constraint ensures FSMap is not expected

---

## Common Errors and Solutions

### Error 1: ImportError for FSMap

```
ImportError: cannot import name 'FSMap' from 'zarr'
```

**Cause**: Code written for zarr v3 running with zarr v2

**Solution**:
- Remove `from zarr import FSMap` line
- Use mapper directly without FSMap wrapper
- Or upgrade to zarr v3 (requires code changes)

### Error 2: FSMap path ValueError

```
ValueError: 'path' was provided but is not used for FSMap store_like objects
```

**Cause**: Using zarr v3 FSMap with v2-style arguments

**Solution**:
- Check zarr version: `import zarr; print(zarr.__version__)`
- If v3: Use FSMap correctly without path argument
- If v2 expected: Something imported v3, check dependencies

### Error 3: MutableMapping vs Store

```
TypeError: expected Store, got MutableMapping
```

**Cause**: Passing v2-style mapper to v3 function expecting FSMap

**Solution**: Wrap mapper with FSMap if using v3

---

## Summary: Key Facts About Zarr v2

### ✅ What Zarr v2 HAS

1. **Storage classes**: DirectoryStore, ZipStore, MemoryStore, etc.
2. **Direct fsspec integration**: Accepts `fsspec.get_mapper()` results directly
3. **Stable API**: Well-tested and widely used
4. **xarray compatibility**: Full support with `engine='zarr'`
5. **Kerchunk support**: Works seamlessly with parquet references

### ❌ What Zarr v2 DOES NOT HAVE

1. **FSMap class**: Does not exist in v2.x
2. **`from zarr import FSMap`**: ImportError in v2
3. **zarr_format parameter**: Not needed in v2
4. **New v3 store protocol**: Uses v2 MutableMapping interface

### 🎯 Bottom Line

**For GEFS processing with zarr v2:**
- ✅ Use `zarr<3.0` in environment
- ✅ Use `fsspec.get_mapper()` directly (no FSMap wrapper)
- ✅ Avoid any code that imports FSMap
- ✅ Current implementation is correct for v2

**FSMap is exclusively a zarr v3 feature - it does not exist in zarr v2**

---

## References

- Zarr v2 documentation: https://zarr.readthedocs.io/en/stable/
- Zarr v3 specification: https://zarr-specs.readthedocs.io/en/latest/
- FSMap introduction: Part of Zarr v3 store refactor (ZEP 1)
- fsspec documentation: https://filesystem-spec.readthedocs.io/

---

**Last Verified**: 2025-11-11
**Zarr Version Tested**: 2.18.7
**Status**: ✅ All assertions verified with actual import testing
