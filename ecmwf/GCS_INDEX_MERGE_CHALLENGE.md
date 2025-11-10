# ECMWF GCS Template + Index DataFrame Merge Challenge

## Critical Understanding

**ECMWF .index format is DIFFERENT from standard GRIB .idx format**

- ECMWF uses **JSON format** (.index files, one JSON object per line)
- Standard GRIB uses **text format** (.idx files, space/colon delimited)
- **Cannot use kerchunk's `parse_grib_idx`** - it's for standard format
- **Cannot use kerchunk's `map_from_index`** directly - merge is more complex

## Why Custom Parsers Exist

`ecmwf_index_processor.py` has custom methods for a reason:

1. **`parse_grib_index()`** - Parses ECMWF's JSON .index format
2. **`create_references_from_index()`** - Creates kerchunk refs from parsed data
3. **`merge_with_gcs_template()`** - The critical merge logic (INCOMPLETE)

## The DataFrame Merge Challenge

### What We Have:

**1. Index DataFrame** (from `parse_ecmwf_json_index()`):
```python
# Columns from ECMWF JSON .index:
- byte_offset: int
- byte_length: int
- variable: str (e.g., "2t", "tp", "10u")
- level: str (e.g., "sfc", "isobaricInhPa")
- step: str (forecast hour as string)
- member: str ("control", "ens01", etc.)
- date: str
- time: str
- levelist: str (pressure level if applicable)
- raw_data: dict (full JSON entry)
```

**2. GCS Template DataFrame** (from `load_gcs_template()`):
```python
# Structure from ecmwf_index_preprocessing.py output:
# Need to inspect actual template to know columns!
# Likely has:
- varname: str
- typeOfLevel: str
- level: int/str
- uri: str (reference to GRIB file)
- offset: int
- length: int
- inline_value: bytes (possibly)
- ... other kerchunk metadata
```

### The Merge Problem:

**Question 1**: What are the actual column names in GCS template?
- Need to inspect template parquet files from Stage 0
- Column names might differ from index DataFrame

**Question 2**: What's the join key?
- Variable name? (but naming might differ)
- Level? (but format might differ)
- Combination?

**Question 3**: What do we keep from each?
- Index: Fresh byte positions (offset, length) for target date
- Template: Variable structure, metadata, hierarchy
- How to combine?

**Question 4**: How to handle mismatches?
- Variables in index but not in template?
- Variables in template but not in index?
- Different pressure levels?

## Current Implementation Status

### In `ecmwf_index_processor.py` (lines 251-311):

```python
def merge_with_gcs_template(index_refs, template_date, member_name, gcs_bucket):
    # Load template DataFrame
    template_df = pd.read_parquet(f"gs://{gcs_path}", filesystem=gcs_fs)

    # Current merge (INCORRECT - just dict merge):
    merged_refs = index_refs.copy()
    for key, value in template_df.items():  # ❌ Iterating dict, not DataFrame
        if key not in merged_refs or key.startswith('.z'):
            merged_refs[key] = value

    return merged_refs
```

**Problems**:
1. ❌ `template_df.items()` iterates over columns, not rows
2. ❌ Not actually merging DataFrames
3. ❌ Not matching variables between index and template
4. ❌ Not updating byte positions

### What It Should Do:

```python
def merge_index_with_template(index_df, template_df, grib_url, target_date):
    """Properly merge index and template DataFrames."""

    # 1. Identify join keys (depends on template structure)
    # Options:
    #   - varname + level
    #   - varname + typeOfLevel + levelist
    #   - custom matching logic

    # 2. Merge DataFrames
    merged = pd.merge(
        template_df,
        index_df,
        left_on=['varname', 'level'],  # Adjust based on actual columns
        right_on=['variable', 'level'],
        how='inner'  # or 'left', 'outer'?
    )

    # 3. Update byte positions from fresh index
    merged['uri'] = grib_url  # Point to target date GRIB
    merged['offset'] = merged['byte_offset']  # From fresh index
    merged['length'] = merged['byte_length']  # From fresh index

    # 4. Convert to kerchunk references format
    references = {}
    for idx, row in merged.iterrows():
        key = build_zarr_key(row)  # Build proper zarr path
        references[key] = [row['uri'], row['offset'], row['length']]

    return references
```

## What Needs Investigation

### 1. Inspect GCS Template Structure

```bash
# Load a template and inspect
python -c "
import pandas as pd
import gcsfs

gcs_fs = gcsfs.GCSFileSystem(token='anon')
df = pd.read_parquet('gs://gik-ecmwf-aws-tf/ecmwf/control/ecmwf-time-20240529-control-rt000.parquet', filesystem=gcs_fs)

print('Columns:', df.columns.tolist())
print('Shape:', df.shape)
print('Sample:')
print(df.head())
"
```

### 2. Understand Variable Name Mapping

- ECMWF index uses short names: "2t", "tp", "10u"
- Template might use full names: "2 metre temperature"
- Need mapping between them

### 3. Determine Level Matching

- Index has: level="sfc", levelist=""
- Template has: typeOfLevel="surface", level=0
- How to match?

### 4. Handle Multi-Level Variables

- Temperature at multiple pressure levels
- Need to match: varname + level combination

## Testing Strategy

### Phase 1: Inspect Templates (IMMEDIATE)

Run on your side:
```bash
python -c "
import pandas as pd
import gcsfs

gcs_fs = gcsfs.GCSFileSystem(token='anon')
template = pd.read_parquet(
    'gs://gik-ecmwf-aws-tf/ecmwf/control/ecmwf-time-20240529-control-rt000.parquet',
    filesystem=gcs_fs
)

print('=== GCS Template Structure ===')
print('Columns:', list(template.columns))
print('Shape:', template.shape)
print('\\nFirst 5 rows:')
print(template.head())
print('\\nColumn types:')
print(template.dtypes)
print('\\nSample values for key columns:')
for col in ['varname', 'level', 'typeOfLevel', 'uri', 'offset', 'length']:
    if col in template.columns:
        print(f'{col}: {template[col].iloc[0] if len(template) > 0 else None}')
"
```

### Phase 2: Test Merge Logic

Using `test_ecmwf_gcs_index_integration.py`:
```bash
# Test single hour first
python test_ecmwf_gcs_index_integration.py \
    --date 20250101 \
    --member control \
    --max-hours 1 \
    --debug
```

This will show:
- Index DataFrame columns
- Template DataFrame columns
- What needs to be matched

### Phase 3: Implement Proper Merge

Based on inspection results, implement in `merge_index_with_template()`:
```python
def merge_index_with_template(index_df, template_df, grib_url):
    # Based on actual column names from inspection
    # Implement proper merge logic
    # Update byte positions
    # Create references
    pass
```

### Phase 4: Validate Output

- Check merged references have correct structure
- Verify byte positions updated to target date
- Test with xarray to ensure it works

## Key Questions to Answer

1. **What columns are in GCS template?**
   - Run inspection script above
   - Document exact column names and types

2. **How to match variables?**
   - Direct name match?
   - Need mapping table?
   - Case sensitive?

3. **How to match levels?**
   - Surface vs "sfc"?
   - Pressure levels - direct match?

4. **What's the reference format in template?**
   - Single reference per row?
   - Multiple references?
   - What does uri column contain?

5. **Are there inline values?**
   - Small variables stored inline?
   - Need special handling?

## Success Criteria

The merge is successful when:
- [ ] All variables from index matched to template
- [ ] Byte positions updated to target date GRIB file
- [ ] References point to correct S3 URIs
- [ ] Output can be converted to zarr store
- [ ] Can open with xarray
- [ ] All 85 timesteps present
- [ ] Variables have correct dimensions

## Next Steps

1. **You inspect GCS templates** - run inspection script, share column structure
2. **You test current merge** - run test_ecmwf_gcs_index_integration.py
3. **You identify issues** - what doesn't match? What's missing?
4. **We implement proper merge** - based on your findings
5. **You validate** - test with real data, verify with xarray

## Files Involved

- `test_ecmwf_gcs_index_integration.py` - NEW test file (clearly named)
- `ecmwf_index_processor.py` - Has merge function to fix (lines 251-311)
- GCS templates at: `gs://gik-ecmwf-aws-tf/ecmwf/{member}/ecmwf-time-{date}-{member}-rt{hour:03d}.parquet`
- ECMWF index files at: `s3://ecmwf-forecasts/{date}/00z/ifs/0p25/enfo/{date}000000-{hour}h-enfo-ef.index`
