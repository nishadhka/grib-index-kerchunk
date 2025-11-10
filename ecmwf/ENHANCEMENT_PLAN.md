# Enhancement Plan: Proper Stage 2 Integration

## Current Situation

**Correction**: The user identified that `test_three_stage_ecmwf_prebuilt.py` Stage 2 is MISSING the critical `map_from_index` + GCS templates integration (GEFS pattern).

My `test_single_member_integration.py` has the right concept, just needs proper implementation.

## What's Missing in test_three_stage_ecmwf_prebuilt.py

### Current Stage 2 (Lines 510-584):
```python
# Simple approach - NOT using map_from_index
idx_entries = parse_grib_index(idx_url, member_filter)
hour_refs = create_references_from_index(grib_url, idx_entries)
```

**Issues**:
- ❌ Uses custom index parser, not kerchunk's `parse_grib_idx`
- ❌ Does NOT use `map_from_index`
- ❌ Does NOT use GCS templates
- ❌ Missing the GEFS pattern integration

### What it SHOULD do (GEFS Pattern):
```python
# 1. Parse fresh index with kerchunk
from kerchunk._grib_idx import parse_grib_idx, map_from_index

idxdf = parse_grib_idx(basename=idx_url, suffix="index", storage_options={"anon": True})

# 2. Load GCS template
template = load_gcs_template(reference_date, member, hour)

# 3. Map them together
mapped = map_from_index(
    run_time=pd.Timestamp(target_date),
    mapping=template,
    idxdf=idxdf
)
```

## Enhancement Strategy

### Phase 1: User Testing (Current)

User will enhance `test_single_member_integration.py` to:
1. Use kerchunk's `parse_grib_idx` instead of custom parser
2. Implement proper `map_from_index` integration
3. Test with GCS templates from Stage 0
4. Verify all 85 timesteps work
5. Validate output with xarray

### Phase 2: Feedback Integration

User will provide:
1. Working code for `map_from_index` + GCS templates
2. Performance metrics
3. Issues encountered
4. Output validation results

### Phase 3: Update prebuilt.py

Based on user feedback, update `test_three_stage_ecmwf_prebuilt.py`:
1. Replace simple Stage 2 with GEFS-pattern Stage 2
2. Integrate `map_from_index` + GCS templates
3. Keep prebuilt zip strategy for Stage 1
4. Maintain Stage 3 merge logic
5. Add proper validation

## Key Components to Implement

### 1. Kerchunk's parse_grib_idx

```python
from kerchunk._grib_idx import parse_grib_idx

# ECMWF uses .index suffix (JSON format)
idxdf = parse_grib_idx(
    basename="s3://ecmwf-forecasts/20250101/00z/ifs/0p25/enfo/2025010100000-0h-enfo-ef",
    suffix="index",  # Not .idx, but .index
    storage_options={"anon": True}
)

# Filter for member
member_num = 0  # control
idxdf_filtered = idxdf[idxdf['number'] == member_num]
```

### 2. GCS Template Loading

```python
import gcsfs
import pandas as pd

def load_gcs_template(reference_date, member, hour, gcs_bucket="gik-ecmwf-aws-tf"):
    """Load GCS template for map_from_index."""
    gcs_path = f"{gcs_bucket}/ecmwf/{member}/ecmwf-time-{reference_date}-{member}-rt{hour:03d}.parquet"

    gcs_fs = gcsfs.GCSFileSystem(token='anon')  # or use service account
    template = pd.read_parquet(f"gs://{gcs_path}", filesystem=gcs_fs)

    return template
```

### 3. map_from_index Integration

```python
from kerchunk._grib_idx import map_from_index

def process_hour_with_gcs_template(target_date, reference_date, member, hour):
    """Process single hour using GEFS pattern."""

    # 1. Parse fresh index
    idx_url = f"s3://ecmwf-forecasts/{target_date}/00z/ifs/0p25/enfo/{target_date}000000-{hour}h-enfo-ef"
    idxdf = parse_grib_idx(basename=idx_url, suffix="index", storage_options={"anon": True})

    # 2. Filter for member
    member_num = get_member_number(member)
    idxdf_filtered = idxdf[idxdf['number'] == member_num]

    # 3. Load GCS template
    template = load_gcs_template(reference_date, member, hour)

    # 4. Map together
    mapped = map_from_index(
        run_time=pd.Timestamp(target_date),
        mapping=template,
        idxdf=idxdf_filtered
    )

    return mapped
```

### 4. Process All 85 Hours

```python
async def process_all_hours_gefs_pattern(target_date, reference_date, member):
    """Process all 85 hours using GEFS pattern."""

    all_hours = ECMWF_FORECAST_HOURS  # 85 hours
    results = []

    for hour in all_hours:
        mapped = await asyncio.get_event_loop().run_in_executor(
            None,
            process_hour_with_gcs_template,
            target_date, reference_date, member, hour
        )
        results.append(mapped)

    # Combine all hours
    combined = pd.concat(results, ignore_index=True)
    return combined
```

## Testing Checklist

User should verify:
- [ ] `parse_grib_idx` works with ECMWF .index files
- [ ] Member filtering works correctly (number=0 for control, 1-50 for perturbed)
- [ ] GCS templates load successfully
- [ ] `map_from_index` produces correct output
- [ ] All 85 hours process successfully
- [ ] Output has correct structure
- [ ] Can create zarr store from output
- [ ] Can open with xarray
- [ ] All variables present
- [ ] Time dimensions correct (85 steps)

## Expected Outcomes

### Performance:
- **With map_from_index + GCS templates**: ~2-4 minutes per member
- **Network**: < 100 MB (just index files)
- **Memory**: < 2 GB

### Output:
- Complete DataFrame with all 85 timesteps
- Proper byte-range references
- Correct variable structure from template
- Fresh byte positions from index

## Integration Back to prebuilt.py

Once user confirms it works:

1. **Replace Stage 2 function** (lines 510-584):
   - Remove `build_complete_parquet_from_indices`
   - Add `build_complete_parquet_with_gcs_templates`
   - Use `map_from_index` + GCS templates

2. **Update Stage 3**:
   - May need adjustments based on new Stage 2 output format
   - Ensure merge logic works with mapped DataFrames

3. **Add validation**:
   - Verify GCS templates exist before Stage 2
   - Better error handling
   - Progress reporting

## Next Steps

1. **User tests and enhances** `test_single_member_integration.py`
2. **User provides feedback** on what works
3. **I integrate** working approach into `test_three_stage_ecmwf_prebuilt.py`
4. **Full pipeline test** with all 51 members

## Notes

- The prebuilt zip strategy (Stage 1) is still valuable - keep it
- Stage 2 needs the GEFS pattern (map_from_index + GCS)
- Stage 3 merge logic should still work
- This is the critical missing piece for production ECMWF processing
