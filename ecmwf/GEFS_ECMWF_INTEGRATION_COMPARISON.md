# GEFS vs ECMWF: GCS Template + Index Integration Comparison

**Date:** 2025-11-10
**Status:** ✅ ECMWF Stage 2 Integration Complete

---

## Executive Summary

Both GEFS and ECMWF follow a **three-stage architecture** to achieve fast daily forecast processing:

- **Stage 0 (Preprocessing)**: Create GCS template files from reference date
- **Stage 1 (Structure)**: Build zarr structure using scan_grib or prebuilt templates
- **Stage 2 (Index+Template Merge)**: **CRITICAL STEP** - Merge fresh index with GCS templates
- **Stage 3 (Final Zarr)**: Write final zarr store

The **critical difference** was that ECMWF was missing the Stage 2 integration that GEFS had implemented. This has now been **completed** with `test_ecmwf_gcs_index_integration.py`.

---

## Architecture Comparison

### GEFS Three-Stage Pipeline

```
┌─────────────────────────────────────────────────────────────┐
│  STAGE 0: Preprocessing (One-time per reference date)      │
│  gefs_index_preprocessing_fixed.py                          │
├─────────────────────────────────────────────────────────────┤
│  Input:   GRIB files + .idx files (reference date)         │
│  Process: build_idx_grib_mapping() from kerchunk           │
│  Output:  GCS parquet templates                            │
│           gs://gik-fmrc/gefs/gep01/                        │
│           gefs-time-20241112-gep01-rt000.parquet           │
│           gefs-time-20241112-gep01-rt003.parquet           │
│           ... (one per forecast hour)                       │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  STAGE 1: Scan GRIB to Deflated Store                      │
│  test_three_stage_gefs_simple.py → test_stage1()           │
├─────────────────────────────────────────────────────────────┤
│  Input:   GRIB files (target date)                         │
│  Process: filter_build_grib_tree()                         │
│  Output:  Deflated zarr store (structure only)             │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  STAGE 2: IDX + GCS Templates → Mapped Index  ⭐CRITICAL⭐  │
│  test_three_stage_gefs_simple.py → test_stage2()           │
├─────────────────────────────────────────────────────────────┤
│  Input:   1. Fresh .idx files (target date)                │
│           2. GCS templates (reference date)                 │
│  Process: cs_create_mapped_index()                         │
│           ├─ parse_grib_idx() - read fresh idx             │
│           ├─ pd.read_parquet() - load GCS template         │
│           └─ map_from_index() - merge them                 │
│  Output:  Mapped index DataFrame                           │
│  Files:   gefs_util.py:736 (cs_create_mapped_index)       │
│           gefs_util.py:576 (process_single_gefs_file)      │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  STAGE 3: Create Final Zarr Store                          │
│  test_three_stage_gefs_simple.py → test_stage3()           │
├─────────────────────────────────────────────────────────────┤
│  Input:   Mapped index + deflated store                    │
│  Process: process_unique_groups()                          │
│  Output:  Final zarr store ready for xarray                │
└─────────────────────────────────────────────────────────────┘
```

### ECMWF Three-Stage Pipeline (NOW COMPLETE)

```
┌─────────────────────────────────────────────────────────────┐
│  STAGE 0: Preprocessing (One-time per reference date)      │
│  ecmwf_par_to_ensemble_members.py                          │
├─────────────────────────────────────────────────────────────┤
│  Input:   .par files (reference date)                      │
│  Process: Split ensemble par into individual members       │
│  Output:  GCS parquet templates                            │
│           gs://gik-fmrc/v2ecmwf_fmrc/ens_control/          │
│           ecmwf-2024052900-control-rt000.par               │
│           gs://gik-fmrc/v2ecmwf_fmrc/ens_09/               │
│           ecmwf-2024052900-ens09-rt000.par                 │
│           ... (one per member, ~85 hours each)             │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  STAGE 1: Use Prebuilt Parquet or Scan GRIB                │
│  test_three_stage_ecmwf_prebuilt.py → test_stage1()        │
├─────────────────────────────────────────────────────────────┤
│  Input:   Prebuilt parquet OR GRIB files                   │
│  Process: Load prebuilt or scan_grib                       │
│  Output:  Deflated zarr store (structure only)             │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  STAGE 2: INDEX + GCS Templates → References  ⭐NEW!⭐      │
│  test_ecmwf_gcs_index_integration.py (COMPLETED)           │
├─────────────────────────────────────────────────────────────┤
│  Input:   1. Fresh .index files (target date, S3)          │
│           2. GCS templates (reference date, GCS)            │
│  Process: build_complete_parquet_from_indices()            │
│           ├─ parse_grib_index() - custom ECMWF JSON        │
│           ├─ create_references_from_index()                │
│           └─ merge_with_gcs_template() - merge them        │
│  Output:  Merged parquet references                        │
│  Files:   ecmwf_index_processor.py:168                     │
│           ecmwf_index_processor.py:252                     │
│  Test:    test_ecmwf_gcs_index_integration.py              │
│  Result:  ✅ 6430 refs (2774 template + 3657 index)        │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  STAGE 3: Create Final Zarr Store                          │
│  test_three_stage_ecmwf_prebuilt.py → test_stage3()        │
├─────────────────────────────────────────────────────────────┤
│  Input:   Index references + deflated store                │
│  Process: Combine and write zarr                           │
│  Output:  Final zarr store ready for xarray                │
│  Status:  ⚠️ NEEDS INTEGRATION with Stage 2 output         │
└─────────────────────────────────────────────────────────────┘
```

---

## Key Technical Differences

### Index File Formats

| Aspect | GEFS | ECMWF |
|--------|------|-------|
| **Index Format** | `.idx` - Standard GRIB index (text) | `.index` - Custom JSON format |
| **Parser** | `parse_grib_idx()` from kerchunk | `parse_grib_index()` - custom |
| **Index Content** | Space-delimited text | JSON objects (one per line) |
| **Kerchunk Support** | ✅ Native support | ❌ Requires custom parser |
| **Example Line** | `1:0:d=2020010100:TMP:2 m above ground` | `{"_offset": 0, "_length": 123, "param": "t"}` |

### Template File Structure

| Aspect | GEFS | ECMWF |
|--------|------|-------|
| **Template Type** | Parquet (mapping) | Parquet (kerchunk refs) |
| **Structure** | DataFrame with GRIB metadata | key-value pairs |
| **Columns** | ~20 columns (varname, step, etc.) | 2 columns: key, value |
| **Size** | Larger (metadata rich) | Smaller (kerchunk format) |
| **Path Pattern** | `gefs-time-{date}-{member}-rt{hour:03d}.parquet` | `ecmwf-{date}00-{member}-rt{hour:03d}.par` |

### GCS Path Patterns

**GEFS:**
```bash
gs://gik-fmrc/gefs/gep01/gefs-time-20241112-gep01-rt000.parquet
gs://gik-fmrc/gefs/gep01/gefs-time-20241112-gep01-rt003.parquet
# Pattern: {bucket}/gefs/{member}/gefs-time-{date}-{member}-rt{hour}.parquet
```

**ECMWF:**
```bash
gs://gik-fmrc/v2ecmwf_fmrc/ens_control/ecmwf-2024052900-control-rt000.par
gs://gik-fmrc/v2ecmwf_fmrc/ens_09/ecmwf-2024052900-ens09-rt000.par
# Pattern: {bucket}/v2ecmwf_fmrc/{member_dir}/ecmwf-{date}00-{member}-rt{hour}.par
# Note: member_dir uses zero-padding (ens_09), filename also (ens09)
```

### Merge Strategy

**GEFS Approach:**
```python
# gefs_util.py:612-635
# 1. Read fresh idx file (binary positions for target date)
idxdf = parse_grib_idx(basename=fname, storage_options=storage_options)

# 2. Read GCS template (metadata structure from reference date)
deduped_mapping = pd.read_parquet(gcs_mapping_path, filesystem=gcs_fs)

# 3. Merge using kerchunk's map_from_index
mapped_index = map_from_index(datestr, deduped_mapping, idxdf_filtered)

# Result: DataFrame with merged metadata + fresh binary positions
```

**ECMWF Approach:**
```python
# ecmwf_index_processor.py:252-356
# 1. Parse fresh index files (custom JSON format)
idx_entries = parse_grib_index(idx_url, member_filter=member_name)

# 2. Create references from index
hour_refs = create_references_from_index(grib_url, idx_entries)

# 3. Load GCS template (kerchunk reference format)
template_df = pd.read_parquet(f"gs://{gcs_path}", filesystem=gcs_fs)
template_refs = {row['key']: row['value'] for _, row in template_df.iterrows()}

# 4. Merge strategy
merged_refs = template_refs.copy()  # Start with template structure
for key, value in index_refs.items():
    if not key.startswith('_'):  # Update with fresh positions
        merged_refs[key] = value

# Result: Dict with template structure + fresh byte positions
```

---

## Integration Test Results

### GEFS Test Results (Reference)
```bash
# From: test_three_stage_gefs_simple.py
Member: gep01
Date: 20250918
Reference: 20241112
Hours: 0-240 (3-hour intervals = 81 timesteps)

Stage 0: ✅ Templates exist in GCS
Stage 1: ✅ Deflated store created
Stage 2: ✅ Mapped index with cs_create_mapped_index()
         - Reads .idx files (target date)
         - Merges with GCS templates (reference date)
         - Uses kerchunk's map_from_index()
Stage 3: ✅ Final zarr store
```

### ECMWF Test Results (NEW!)
```bash
# From: test_ecmwf_gcs_index_integration.py
python test_ecmwf_gcs_index_integration.py \
    --date 20250101 \
    --reference-date 20240529 \
    --member 09

Member: ens09
Target Date: 20250101
Reference Date: 20240529
Hours: 0-360 (85 timesteps)

✅ Integration test PASSED

Results:
- Template entries: 2774 (from GCS reference)
- Index entries: 3657 (from fresh S3 index)
- Merged total: 6430 references
- Processing time: 30.3 seconds
- Output: output_gcs_integration_test_20250101/ens09.parquet
```

### Breakdown of Merge Results
```python
# Template (2774 entries from reference date 20240529):
- .zarray, .zattrs (zarr metadata)
- Coordinate variables
- Pre-built structure and mappings

# Index (3657 entries from target date 20250101):
- Fresh byte positions for all 85 forecast hours
- step_000/0/0/0 → [grib_url, offset, length]
- step_003/0/0/0 → [grib_url, offset, length]
- ... (all timesteps and variables)

# Merged (6430 entries):
- Template structure (metadata, coordinates)
- Fresh data references (updated byte positions)
- Ready for Stage 3 zarr creation
```

---

## Next Steps: Integrating with test_three_stage_ecmwf_prebuilt.py

### Current Status

| Stage | GEFS Status | ECMWF Status | Action Needed |
|-------|-------------|--------------|---------------|
| **Stage 0** | ✅ Complete | ✅ Complete | None - templates exist |
| **Stage 1** | ✅ Complete | ✅ Complete | None - works as-is |
| **Stage 2** | ✅ Complete | ✅ **JUST COMPLETED** | ⚠️ **Integration needed** |
| **Stage 3** | ✅ Complete | ⚠️ Partial | ⚠️ **Update required** |

### Integration Plan

#### Option 1: Add Stage 2 to test_three_stage_ecmwf_prebuilt.py (RECOMMENDED)

Replace the current Stage 2 in `test_three_stage_ecmwf_prebuilt.py` with the new integration:

**Current Code (Lines 507-644):**
```python
def test_stage2_index():
    """STAGE 2: Index-based processing for ALL 85 hours."""
    # Currently just builds from index without GCS templates
    for hour in ALL_FORECAST_HOURS:
        idx_url = f"s3://{S3_BUCKET}/{date_str}/{run}z/ifs/0p25/enfo/{date_str}000000-{hour}h-enfo-ef.index"
        idx_entries = parse_grib_index(idx_url, member_filter=member_name)
        # ... creates references but NO GCS template merge
```

**New Code (Using Integration):**
```python
def test_stage2_with_gcs_templates():
    """STAGE 2: INDEX + GCS Templates → Merged References (GEFS Pattern)."""
    log_stage(2, "INDEX + GCS TEMPLATES → MERGED REFERENCES")

    from ecmwf_index_processor import build_complete_parquet_from_indices

    log_checkpoint("Using GEFS-style integration with GCS templates")
    log_checkpoint(f"Target date: {TEST_DATE} (fresh index from S3)")
    log_checkpoint(f"Reference date: {REFERENCE_DATE} (templates from GCS)")

    # Build with GCS template merge enabled
    refs = build_complete_parquet_from_indices(
        date_str=TEST_DATE,
        run=TEST_RUN,
        member_name=TEST_MEMBER,
        hours=ALL_FORECAST_HOURS,
        use_gcs_template=True,  # ⭐ ENABLE GCS TEMPLATE MERGE
        gcs_template_date=REFERENCE_DATE
    )

    # Save merged references
    stage2_output = OUTPUT_DIR / f"stage2_{TEST_MEMBER}_merged_refs.parquet"
    df = pd.DataFrame([
        {'key': k, 'value': v if isinstance(v, (str, bytes)) else json.dumps(v)}
        for k, v in refs.items()
    ])
    df.to_parquet(stage2_output)

    log_checkpoint(f"✅ Stage 2 Complete!")
    log_checkpoint(f"   Template entries: {refs.get('_template_entries', 'N/A')}")
    log_checkpoint(f"   Index entries: {refs.get('_index_entries', 'N/A')}")
    log_checkpoint(f"   Merged total: {len(refs)}")
    log_checkpoint(f"   Saved to: {stage2_output}")

    return refs
```

#### Option 2: Create New Unified Test Script

Create `test_three_stage_ecmwf_unified.py` that:
1. Combines best of both worlds
2. Uses prebuilt for Stage 1 (fast)
3. Uses GCS+Index integration for Stage 2 (complete)
4. Updates Stage 3 to use merged references

#### Option 3: Run Tests Separately Then Combine

1. **Stage 1+2:** Run `test_ecmwf_gcs_index_integration.py` → Get merged parquet
2. **Stage 3:** Run `test_three_stage_ecmwf_prebuilt.py` → Load merged parquet → Create zarr

---

## Implementation Checklist

### ✅ Completed (Stage 2 Integration)
- [x] Create `test_ecmwf_gcs_index_integration.py`
- [x] Implement `merge_with_gcs_template()` in `ecmwf_index_processor.py`
- [x] Handle member input normalization (both "09" and "ens09")
- [x] Fix GCS path pattern (ens_09 directory + ens09 filename)
- [x] Test successful merge (6430 refs = 2774 template + 3657 index)
- [x] Validate with real data (date=20250101, reference=20240529, member=09)

### 🚧 Next Steps (Stage 3 Integration)
- [ ] Update `test_three_stage_ecmwf_prebuilt.py` Stage 2 section
- [ ] Modify Stage 3 to accept merged references from Stage 2
- [ ] Test full three-stage pipeline end-to-end
- [ ] Validate final zarr can be opened with xarray
- [ ] Run for multiple members (control, ens01-ens50)
- [ ] Performance benchmarking vs old approach

### 📝 Documentation
- [ ] Update `test_three_stage_ecmwf_prebuilt.py` comments
- [ ] Add inline documentation explaining GEFS pattern
- [ ] Create performance comparison report
- [ ] Document recommended workflow for daily operations

---

## Performance Considerations

### GEFS Performance (Reference)
```
Stage 0 (one-time): ~5 minutes per member (preprocessing)
Stage 1: ~2-3 minutes (scan_grib for structure)
Stage 2: ~30 seconds (idx + GCS templates)
Stage 3: ~1 minute (final zarr)
Total: ~4-5 minutes per day per member
```

### ECMWF Performance (Projected)
```
Stage 0 (one-time): Already done (templates exist in GCS)
Stage 1: ~1-2 minutes (prebuilt parquet) OR ~10 minutes (scan_grib)
Stage 2: ~30 seconds (85 hours, measured)
Stage 3: ~1-2 minutes (estimated)
Total: ~3-5 minutes per day per member

For 51 members (control + ens01-ens50):
Sequential: ~4 hours
Parallel (10 workers): ~25 minutes
```

### Bottleneck Analysis

**GEFS Bottleneck:** Stage 1 (scan_grib)
**Solution:** Pre-built templates in Stage 0

**ECMWF Bottleneck:** Stage 2 was missing
**Solution:** ✅ Now implemented with GCS template merge

**Next Bottleneck:** Stage 3 zarr writing
**Solution:** Parallel processing with Dask/Coiled

---

## Code Location Reference

### GEFS Files
```
gefs/test_three_stage_gefs_simple.py         # Three-stage test
gefs/gefs_util.py:736                         # cs_create_mapped_index()
gefs/gefs_util.py:576                         # process_single_gefs_file()
gefs/dev-test/gefs_index_preprocessing_fixed.py  # Stage 0 preprocessing
```

### ECMWF Files
```
ecmwf/test_ecmwf_gcs_index_integration.py          # ⭐ NEW Stage 2 test
ecmwf/ecmwf_index_processor.py:56                  # parse_grib_index()
ecmwf/ecmwf_index_processor.py:112                 # create_references_from_index()
ecmwf/ecmwf_index_processor.py:168                 # build_complete_parquet_from_indices()
ecmwf/ecmwf_index_processor.py:252                 # merge_with_gcs_template() ⭐
ecmwf/test_three_stage_ecmwf_prebuilt.py          # Needs Stage 2 update
ecmwf/ecmwf_par_to_ensemble_members.py            # Stage 0 preprocessing
```

---

## Example Usage

### Running ECMWF Stage 2 Integration Test

```bash
cd /home/user/grib-index-kerchunk/ecmwf

# Test with ensemble member 09
python test_ecmwf_gcs_index_integration.py \
    --date 20250101 \
    --reference-date 20240529 \
    --member 09

# Test with control member
python test_ecmwf_gcs_index_integration.py \
    --date 20250101 \
    --reference-date 20240529 \
    --member control

# Inspect template first
python test_ecmwf_gcs_index_integration.py \
    --date 20250101 \
    --reference-date 20240529 \
    --member 09 \
    --inspect-template

# Test with fewer hours (quick test)
python test_ecmwf_gcs_index_integration.py \
    --date 20250101 \
    --reference-date 20240529 \
    --member 09 \
    --test-hours 5
```

### Running GEFS Stage 2 (Reference)

```bash
cd /home/user/grib-index-kerchunk/gefs

python test_three_stage_gefs_simple.py
# Automatically runs all three stages with gep01 member
```

---

## Conclusion

✅ **ECMWF now has the same Stage 2 capability as GEFS!**

The key achievement is implementing the **GCS template + fresh index merge** pattern that GEFS uses. This enables:

1. **Fast daily processing** - Only parse small index files, not full GRIB
2. **Reusable templates** - One-time preprocessing, reuse forever
3. **Consistent structure** - Templates ensure zarr structure consistency
4. **Scalable** - Process 51 members in parallel efficiently

**Next milestone:** Integrate Stage 2 into `test_three_stage_ecmwf_prebuilt.py` and validate full three-stage pipeline.

---

**Questions or Issues?**
See related documents:
- `ecmwf/20251107-missing-comps-ecmwf-gik.md` - Original problem analysis
- `ecmwf/GCS_INDEX_MERGE_CHALLENGE.md` - Technical challenges
- `ecmwf/CRITICAL_EVALUATION.md` - Comparison analysis
