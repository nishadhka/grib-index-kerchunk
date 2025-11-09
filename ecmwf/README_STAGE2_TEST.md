# ECMWF Stage 2 Integration Test - Single Member

## Overview

This test script implements the **missing Stage 2 components** identified in `20251107-missing-comps-ecmwf-gik.md`. It focuses on a **single ensemble member** for quick testing.

## What This Implements

### Critical Missing Components (from the analysis):

1. ✅ **`generate_ecmwf_axes()`** - Already exists in `ecmwf_util.py` (handles 85 timesteps)
2. ✅ **Async batch processing** - NEW in `test_single_member_integration.py`
3. ✅ **Index + GCS template integration** - NEW `process_single_ecmwf_hour()`
4. ✅ **Stage 2 integration layer** - NEW `process_all_85_hours()`

### The Integration Process

```
Stage 2 Integration (New Implementation):
┌─────────────────────────────────────────────────────────────┐
│  For each of 85 forecast hours:                             │
│                                                              │
│  1. Parse fresh index file from target date (S3)            │
│     → Binary byte positions for variables                   │
│                                                              │
│  2. Load GCS template from reference date                   │
│     → Variable structure and metadata                       │
│                                                              │
│  3. Map fresh positions with template structure             │
│     → Complete parquet mapping                              │
└─────────────────────────────────────────────────────────────┘
```

## Quick Start

### Prerequisites

1. **GCS Templates must exist** (from Stage 0):
   ```bash
   # Check if templates exist
   gsutil ls gs://gik-ecmwf-aws-tf/ecmwf/control/

   # Should show: ecmwf-time-20240529-control-rt*.parquet (85 files)
   ```

2. **Python dependencies**:
   ```bash
   pip install pandas gcsfs fsspec kerchunk
   ```

### Run Test for Single Member

```bash
# Test with control member
python test_single_member_integration.py \
    --date 20250101 \
    --member control \
    --reference-date 20240529

# Test with perturbed member
python test_single_member_integration.py \
    --date 20250101 \
    --member ens01 \
    --reference-date 20240529

# With debug logging
python test_single_member_integration.py \
    --date 20250101 \
    --member control \
    --debug
```

### Expected Output

```
================================================================================
ECMWF STAGE 2 INTEGRATION TEST - SINGLE MEMBER
================================================================================
Target Date:     20250101
Member:          control
Reference Date:  20240529
GCS Bucket:      gik-ecmwf-aws-tf
Total Hours:     85 (3h: 49, 6h: 36)
================================================================================

Step 1: Generating time axes
✅ Generated axes: 85 time steps

Step 2: Processing all 85 forecast hours (Stage 2)
📦 Batch 1/9: Processing hours [0, 3, 6, 9, 12, 15, 18, 21, 24, 27]
  Processing hour   0h - Parsing index from 20250101
  ✅ Hour   0h - Mapped 123 entries
  Processing hour   3h - Parsing index from 20250101
  ✅ Hour   3h - Mapped 123 entries
  ...
✅ Batch 1/9 complete - 10/10 hours successful
...
🎉 Total: 10455 mapped entries for control

Step 3: Saving results
💾 Saved result to: output_stage2_test/control_20250101_stage2.parquet
   File size: 2.45 MB
   Total entries: 10455

================================================================================
PROCESSING COMPLETE
================================================================================
✅ Success:          True
⏱️  Time:             125.3 seconds (2.09 minutes)
📊 Entries processed: 10455
💾 Output file:      output_stage2_test/control_20250101_stage2.parquet

🎉 Stage 2 integration test PASSED!
   Successfully processed 85 forecast hours
   Ready to integrate into full pipeline
================================================================================
```

## Performance Expectations

### Single Member Test:
- **Time**: 2-4 minutes
- **Memory**: < 1 GB
- **Network**: ~50-100 MB (downloading index files only)
- **Output**: ~2-3 MB parquet file

### Comparison:
| Method | Time | Network | Memory |
|--------|------|---------|--------|
| **Stage 2 (This)** | 2-4 min | 50 MB | < 1 GB |
| scan_grib (old) | 60+ min | 40+ GB | 16 GB |
| **Speedup** | **15-30x faster** | **800x less data** | **16x less memory** |

## What This Tests

### Functionality:
- ✅ Parsing ECMWF index files from S3
- ✅ Loading GCS templates with authentication
- ✅ Mapping index data to template structure
- ✅ Processing all 85 forecast hours (3h + 6h intervals)
- ✅ Async batch processing with concurrency control
- ✅ Member filtering (control vs ens01-ens50)

### Output Validation:
- ✅ Total entries should be ~10,000-12,000 for a single member
- ✅ All 85 forecast hours processed
- ✅ Parquet file created successfully

## Next Steps

### 1. Verify Test Success
```bash
# Check output file
ls -lh output_stage2_test/

# Inspect parquet
python -c "import pandas as pd; df = pd.read_parquet('output_stage2_test/control_20250101_stage2.parquet'); print(df.info())"
```

### 2. Test Multiple Members
```bash
# Test a few members
for member in control ens01 ens02; do
    python test_single_member_integration.py --date 20250101 --member $member
done
```

### 3. Integration into Full Pipeline

Once this test passes, the components can be integrated into:
- `run_day_ecmwf_ensemble_full.py` (to be created)
- Full 51-member parallel processing
- Production pipeline

## Troubleshooting

### Issue: "Template not found"
**Cause**: GCS templates don't exist yet
**Solution**: Run Stage 0 first (from `ecmwf_index_preprocessing.py`)
```bash
python ecmwf_index_preprocessing.py --date 20240529 --member control
```

### Issue: "No data for member"
**Cause**: Index file doesn't contain requested member
**Solution**: Check if member exists in the ECMWF data for that date

### Issue: "Permission denied" on GCS
**Cause**: GCS authentication issue
**Solution**:
```bash
# Use service account
export GOOGLE_APPLICATION_CREDENTIALS="path/to/coiled-data-e4drr_202505.json"

# Or modify script to use service account explicitly
```

### Issue: Slow processing
**Cause**: Network or too many concurrent operations
**Solution**: Adjust `max_concurrent` parameter (default: 10)

## File Structure

```
ecmwf/
├── test_single_member_integration.py  # This test script
├── ecmwf_util.py                      # Utility functions (has generate_ecmwf_axes)
├── ecmwf_index_processor.py           # Basic index processor
├── 20251107-missing-comps-ecmwf-gik.md # Analysis document
└── README_STAGE2_TEST.md              # This file
```

## Success Criteria

The test is successful if:
1. ✅ All 85 forecast hours processed
2. ✅ Total entries ~10,000-12,000 for one member
3. ✅ Processing time < 5 minutes
4. ✅ Parquet file created successfully
5. ✅ No critical errors in logs

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                     ECMWF Stage 2 Test                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Input: Target Date + Member Name                              │
│     ↓                                                           │
│  Step 1: generate_ecmwf_axes(date)                             │
│     → Creates 85 timestep axes                                 │
│     ↓                                                           │
│  Step 2: process_all_85_hours()                                │
│     │                                                           │
│     ├─→ Batch 1 (hours 0-27)                                   │
│     │   ├─→ process_single_ecmwf_hour(0h)                      │
│     │   │   ├─→ Parse index from S3                            │
│     │   │   ├─→ Load template from GCS                         │
│     │   │   └─→ Map with map_from_index()                      │
│     │   ├─→ process_single_ecmwf_hour(3h)                      │
│     │   └─→ ... (parallel async)                               │
│     │                                                           │
│     ├─→ Batch 2-8 (hours 30-330)                               │
│     └─→ Batch 9 (hours 336-360)                                │
│     ↓                                                           │
│  Step 3: Combine all results → Save parquet                    │
│     ↓                                                           │
│  Output: Parquet with all 85 timesteps                         │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Performance Tuning

### Adjust concurrency:
```python
# In the script, modify max_concurrent
mapped_df = await process_all_85_hours(
    target_date=date_str,
    member=member,
    max_concurrent=20  # Increase for faster processing (default: 10)
)
```

### Adjust batch size:
```python
# In process_all_85_hours(), modify batch_size
batch_size = 15  # Increase for fewer batch iterations (default: 10)
```

## Integration Checklist

Before integrating into production:
- [ ] Test with control member
- [ ] Test with at least 2 perturbed members (ens01, ens02)
- [ ] Verify output parquet can be read by xarray
- [ ] Check memory usage stays < 2 GB
- [ ] Verify all 85 hours processed successfully
- [ ] Test with different target dates
- [ ] Document any GCS authentication requirements

## Contact & Support

For issues or questions:
- Check logs in the output
- Review `20251107-missing-comps-ecmwf-gik.md` for architecture details
- Verify GCS templates exist from Stage 0
