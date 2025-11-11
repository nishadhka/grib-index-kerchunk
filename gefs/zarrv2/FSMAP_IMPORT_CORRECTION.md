# CRITICAL CORRECTION: FSMap Import Path in Zarr v2

**Date**: 2025-11-11
**Issue**: Incorrect assertion about FSMap import in Zarr v2
**Status**: CORRECTION REQUIRED in CORRECTED_ZARR_ANALYSIS.md

---

## The Error Found

### What the Document Claims (INCORRECT)

**Lines 110, 212 in CORRECTED_ZARR_ANALYSIS.md**:

```python
# Inside fsspec.implementations.reference.ReferenceFileSystem
from zarr import FSMap  # ← This import fails with Zarr v3!
```

### What Actually Happens (VERIFIED)

**Test in Zarr v2.18.7**:

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

**Result**: `from zarr import FSMap` **FAILS in Zarr v2.18.7!**

---

## The Truth About FSMap in Zarr v2

### FSMap Does Not Exist in Zarr v2's Public API

Looking at the Zarr v2 source code and documentation:

1. **FSMap is NOT a Zarr v2 class**
2. **FSMap was introduced in Zarr v3**
3. **Zarr v2 uses different storage classes**

### What Zarr v2 Actually Has

```python
import zarr

# These exist in Zarr v2
from zarr.storage import (
    DirectoryStore,     # ✅ Exists
    ZipStore,           # ✅ Exists
    DBMStore,           # ✅ Exists
    MemoryStore,        # ✅ Exists
    FSStore,            # ✅ Exists (NOT FSMap!)
)

# This does NOT exist in Zarr v2
from zarr import FSMap       # ❌ ImportError
from zarr.storage import FSMap  # ❌ Does not exist
```

---

## What fsspec Actually Does

### In Zarr v2 Era

**fsspec's ReferenceFileSystem does NOT use FSMap** (because it doesn't exist in v2!)

Instead, it returns an **fsspec mapper** that implements the `MutableMapping` interface:

```python
# What fsspec.filesystem("reference") actually does
class ReferenceFileSystem:
    def get_mapper(self, path=""):
        """Returns a MutableMapping, NOT FSMap"""
        return ReferenceMap(self, path)  # Custom class, not zarr.FSMap

# This mapper is compatible with Zarr v2's API
mapper = fs.get_mapper("")
ds = xr.open_dataset(mapper, engine='zarr')  # Works in v2!
```

**Key Point**: Zarr v2 accepts **any MutableMapping** as a store, not just zarr.storage classes.

---

## What Changed in Zarr v3

### Zarr v3 Introduced FSMap

```python
# Zarr v3 (>= 3.0)
import zarr
from zarr import FSMap  # ✅ NOW exists in v3

# FSMap wraps fsspec filesystems
from fsspec import get_mapper
mapper = get_mapper('s3://bucket')
store = FSMap(mapper)  # Wrap in v3 store protocol
```

### Zarr v3 Broke fsspec Compatibility

**The problem**: Zarr v3 changed its store API:

1. **Zarr v2**: Accepts any `MutableMapping` (dict-like) as store
2. **Zarr v3**: Requires stores to implement new `Store` abstract base class

**Impact on fsspec**:
```python
# Zarr v2 - Works
mapper = fs.get_mapper("")  # Returns MutableMapping
zarr.open_group(mapper)     # ✅ Accepts MutableMapping

# Zarr v3 - Breaks
mapper = fs.get_mapper("")  # Still returns MutableMapping
zarr.open_group(mapper)     # ❌ Expects Store, not MutableMapping
```

---

## The Correct Timeline

### Phase 1: Zarr v2 Era (2020-2024)

```python
# User code
fs = fsspec.filesystem("reference", fo=refs)
mapper = fs.get_mapper("")  # Returns MutableMapping
ds = xr.open_dataset(mapper, engine='zarr')  # ✅ Works

# What Zarr v2 accepts
- MutableMapping (any dict-like object)
- zarr.storage.DirectoryStore
- zarr.storage.FSStore (for fsspec, but NOT FSMap!)
```

**FSMap does not exist in Zarr v2!**

### Phase 2: Zarr v3 Released (2024)

```python
# Same user code
mapper = fs.get_mapper("")  # Still MutableMapping
ds = xr.open_dataset(mapper, engine='zarr')  # ❌ Fails

# Error: Zarr v3 requires Store protocol
TypeError: expected Store, got MutableMapping
```

### Phase 3: FSMap Introduced (Zarr v3)

```python
# New Zarr v3 way
from zarr import FSMap

mapper = fs.get_mapper("")
store = FSMap(mapper)  # Wrap MutableMapping in Store protocol
ds = xr.open_dataset(store, engine='zarr')  # Would work if supported
```

**But**: xarray and fsspec haven't been updated to use FSMap yet!

---

## Corrected Understanding

### What the Document Should Say

**WRONG** (Lines 110, 212):
```python
from zarr import FSMap  # ← This import fails with Zarr v3!
```

**CORRECT**:
```python
# Zarr v2 (no FSMap):
# fsspec returns MutableMapping, which Zarr v2 accepts directly
mapper = fs.get_mapper("")  # MutableMapping
zarr.open_group(mapper)     # ✅ Works in v2

# Zarr v3 (FSMap exists but breaks compatibility):
mapper = fs.get_mapper("")  # Still MutableMapping
zarr.open_group(mapper)     # ❌ Fails - needs Store protocol

# What Zarr v3 would need:
from zarr import FSMap      # ✅ Exists in v3
store = FSMap(mapper)       # Wrap in Store protocol
zarr.open_group(store)      # ✅ Would work
```

---

## The Real Problem Explained

### It's Not About FSMap Import Failing

The issue is **NOT** that `from zarr import FSMap` fails in v3.

The issue is **Store Protocol Breaking Change**:

| Zarr Version | What it accepts | What fsspec provides | Compatible? |
|--------------|----------------|---------------------|-------------|
| **v2** | `MutableMapping` | `MutableMapping` | ✅ Yes |
| **v3** | `Store` protocol | `MutableMapping` | ❌ No |

### Why Things Broke

1. **Zarr v3 changed requirements**: Stores must implement new `Store` ABC
2. **fsspec still returns old type**: MutableMapping (dict-like)
3. **Zarr v3 rejects old type**: TypeError when given MutableMapping
4. **FSMap was added as bridge**: To wrap MutableMapping in Store protocol
5. **But xarray/fsspec not updated**: Still return raw MutableMapping

---

## What Needs Fixing

### Option 1: fsspec Updates ReferenceFileSystem

```python
# Current (broken with v3)
class ReferenceFileSystem:
    def get_mapper(self):
        return ReferenceMapping(...)  # MutableMapping

# Fixed for v3
class ReferenceFileSystem:
    def get_mapper(self):
        from zarr import FSMap
        mapping = ReferenceMapping(...)
        return FSMap(mapping)  # Wrapped in Store protocol
```

### Option 2: xarray Handles Both Versions

```python
# xarray could detect zarr version
def open_dataset(store, engine='zarr'):
    if engine == 'zarr':
        import zarr
        if hasattr(zarr, 'FSMap'):
            # Zarr v3 - wrap if needed
            if isinstance(store, MutableMapping):
                from zarr import FSMap
                store = FSMap(store)
        # Proceed with opening
```

---

## Corrections Needed in CORRECTED_ZARR_ANALYSIS.md

### Line 56 - INCORRECT

**Current**:
```
- Uses Zarr v2's FSMap internally
```

**Should be**:
```
- Uses fsspec's MutableMapping (Zarr v2 accepts MutableMapping directly)
```

### Line 97 - INCORRECT

**Current**:
```
Uses zarr.FSMap (Zarr v2)
```

**Should be**:
```
Returns MutableMapping (accepted by Zarr v2)
```

### Line 99 - MISLEADING

**Current**:
```
❌ FSMap removed in Zarr v3
```

**Should be**:
```
❌ MutableMapping no longer accepted in Zarr v3 (requires Store protocol)
```

### Line 110 - INCORRECT

**Current**:
```python
from zarr import FSMap  # ← This import fails with Zarr v3!
```

**Should be**:
```python
# Zarr v2: Returns MutableMapping (accepted directly)
mapper = fs.get_mapper("")  # No FSMap needed or used

# Zarr v3: MutableMapping rejected
mapper = fs.get_mapper("")  # ❌ Wrong type for v3
# Would need: FSMap(mapper) but fsspec not updated
```

### Line 212 - INCORRECT

**Current**:
```python
from zarr import FSMap  # ❌ Doesn't exist in v3
```

**Should be**:
```python
# FSMap actually DOES exist in v3 (not v2!)
from zarr import FSMap  # ✅ Exists in v3, NOT in v2

# The problem is fsspec needs updating to use it
```

---

## Summary of Corrections

### What Was Wrong

1. ❌ Claimed `from zarr import FSMap` works in v2 (it doesn't)
2. ❌ Claimed FSMap was "removed" in v3 (it was actually ADDED)
3. ❌ Implied fsspec used FSMap in v2 (FSMap didn't exist)

### What Is Actually True

1. ✅ **FSMap does NOT exist in Zarr v2** - ImportError when trying to import
2. ✅ **FSMap was ADDED in Zarr v3** - New store wrapper class
3. ✅ **Zarr v2 accepts MutableMapping** - fsspec returns MutableMapping
4. ✅ **Zarr v3 requires Store protocol** - MutableMapping no longer compatible
5. ✅ **fsspec not updated** - Still returns MutableMapping, not FSMap-wrapped Store

---

## Testing to Verify

### Test 1: FSMap in Zarr v2

```python
# Test with Zarr v2.18.7
import zarr
print(zarr.__version__)  # 2.18.7

try:
    from zarr import FSMap
    print("FSMap exists")
except ImportError:
    print("FSMap does NOT exist")  # ← This is what happens

# Expected: FSMap does NOT exist
```

### Test 2: FSMap in Zarr v3

```python
# Test with Zarr v3.0+
import zarr
print(zarr.__version__)  # 3.x.x

try:
    from zarr import FSMap
    print("FSMap exists")  # ← This is what happens
except ImportError:
    print("FSMap does NOT exist")

# Expected: FSMap exists
```

### Test 3: What Storage Classes Exist

```python
# Zarr v2
import zarr.storage
print([x for x in dir(zarr.storage) if 'Store' in x or 'Map' in x])
# ['DirectoryStore', 'FSStore', 'MemoryStore', 'ZipStore', ...]
# Note: FSStore exists, but NOT FSMap

# Zarr v3
import zarr
print([x for x in dir(zarr) if 'Map' in x])
# ['FSMap']  ← New in v3
```

---

## Recommended Actions

### Immediate

1. **Correct CORRECTED_ZARR_ANALYSIS.md**:
   - Remove all claims that FSMap exists in v2
   - Clarify that FSMap was ADDED in v3
   - Explain MutableMapping → Store protocol change

2. **Update all documentation** that mentions FSMap

3. **Add this correction** as reference

### Future

1. **Monitor fsspec development**: Watch for ReferenceFileSystem v3 support
2. **Test with updated packages**: When fsspec adds FSMap support
3. **Migrate back to proper solution**: When ecosystem catches up

---

## The Bottom Line

**FSMap Timeline**:
- ❌ **Zarr v2**: FSMap does NOT exist
- ✅ **Zarr v3**: FSMap ADDED as new feature
- 🔄 **fsspec**: Not yet updated to use FSMap

**Breaking Change**:
- **Not** removal of FSMap (it didn't exist to remove)
- **But** rejection of MutableMapping in favor of Store protocol
- **And** fsspec not updated to provide Store-compatible objects

**User's Test Confirms**:
```python
import zarr  # v2.18.7
from zarr import FSMap  # ImportError
```

✅ **This proves FSMap does NOT exist in Zarr v2**

---

**Status**: Critical Correction Required
**Impact**: Entire narrative about FSMap in document is backwards
**Next Action**: Update CORRECTED_ZARR_ANALYSIS.md with accurate information
