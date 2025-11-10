#!/usr/bin/env python3
"""
ECMWF Three-Stage Processing with GCS Template Integration (GEFS Pattern)
Tests the complete three-stage pipeline with GCS template + index merge.

Key Features:
- Stage 0: Pre-built GCS templates (one-time preprocessing)
- Stage 1: Uses prebuilt parquet OR scan_grib for structure
- Stage 2: ⭐ GCS template + fresh index merge (GEFS pattern) ⭐
- Stage 3: Final zarr store creation

Stage 2 Integration:
- Reads fresh .index files from S3 (target date)
- Merges with GCS template parquet (reference date)
- Follows the same pattern as GEFS for fast daily processing
- Uses merge_with_gcs_template() from ecmwf_index_processor.py

Configuration:
- Reference Date: 20240529 (for GCS templates)
- GCS Bucket: gs://gik-fmrc/v2ecmwf_fmrc
- Service Account: coiled-data-e4drr_202505.json

Usage:
    python test_three_stage_ecmwf_prebuilt.py
    # Or specify a zip file:
    python test_three_stage_ecmwf_prebuilt.py --zip ecmwf_20251006_00_efficient.zip
"""

import pandas as pd
import numpy as np
import xarray as xr
import json
import os
import copy
import time
import zipfile
import shutil
import argparse
import re
from pathlib import Path
from datetime import datetime
from glob import glob

# Import ECMWF utilities
from ecmwf_util import (
    generate_ecmwf_axes,
    ECMWF_FORECAST_DICT,
    ECMWF_FORECAST_HOURS
)

# Set up anonymous S3 access
os.environ['AWS_NO_SIGN_REQUEST'] = 'YES'

# ==============================================================================
# CONFIGURATION
# ==============================================================================

# Reference date with pre-built GCS mappings (if Stage 0 was done)
REFERENCE_DATE = '20240529'

# GCS configuration (needed for Stage 2)
GCS_BUCKET = 'gik-fmrc'
GCS_BASE_PATH = 'v2ecmwf_fmrc'  # Base path within the bucket
GCP_SERVICE_ACCOUNT = 'coiled-data-e4drr_202505.json'  # Just filename, not full path

# Variables to extract (subset of ECMWF variables)
FORECAST_DICT = {
    "2 metre temperature": "2t:sfc",
    "Total precipitation": "tp:sfc",
    "10 metre U wind": "10u:sfc",
    "10 metre V wind": "10v:sfc"
}

# Output directory
OUTPUT_DIR = Path("test_ecmwf_three_stage_prebuilt_output")

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

def log_stage(stage_num, stage_name):
    """Print stage header."""
    print(f"\n{'='*80}")
    print(f"STAGE {stage_num}: {stage_name}")
    print(f"{'='*80}")


def log_checkpoint(message):
    """Print checkpoint with timestamp."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}")


def extract_date_run_from_zip(zip_filename):
    """
    Extract date and run from zip filename.
    Format: ecmwf_YYYYMMDD_HH_efficient.zip
    Returns: (date_str, run_str)
    """
    pattern = r'ecmwf_(\d{8})_(\d{2})_efficient\.zip'
    match = re.match(pattern, Path(zip_filename).name)

    if match:
        date_str = match.group(1)
        run_str = match.group(2)
        return date_str, run_str
    else:
        raise ValueError(f"Unable to extract date and run from filename: {zip_filename}")


def unzip_and_prepare(zip_file, extract_dir):
    """
    Unzip the prebuilt zip file and return paths to parquet files.
    """
    log_checkpoint(f"Extracting {zip_file}...")

    # Clear extraction directory if exists
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)

    # Extract zip file
    with zipfile.ZipFile(zip_file, 'r') as zf:
        zf.extractall(extract_dir)

    # Find extracted directory (should be one directory inside extract_dir)
    extracted_dirs = list(extract_dir.glob("ecmwf_*_efficient"))
    if not extracted_dirs:
        raise ValueError(f"No extracted directory found in {extract_dir}")

    extracted_dir = extracted_dirs[0]

    # Find members directory
    members_dir = extracted_dir / "members"
    if not members_dir.exists():
        raise ValueError(f"Members directory not found: {members_dir}")

    # Find all member parquet files
    member_parquets = {}
    for member_dir in sorted(members_dir.glob("*")):
        if member_dir.is_dir():
            member_name = member_dir.name
            parquet_files = list(member_dir.glob("*.parquet"))
            if parquet_files:
                member_parquets[member_name] = parquet_files[0]

    # Also check for comprehensive parquet
    comprehensive_parquet = None
    comprehensive_dir = extracted_dir / "comprehensive"
    if comprehensive_dir.exists():
        comprehensive_files = list(comprehensive_dir.glob("*.parquet"))
        if comprehensive_files:
            comprehensive_parquet = comprehensive_files[0]

    log_checkpoint(f"Found {len(member_parquets)} member parquet files")
    if comprehensive_parquet:
        log_checkpoint(f"Found comprehensive parquet: {comprehensive_parquet.name}")

    return {
        'members': member_parquets,
        'comprehensive': comprehensive_parquet,
        'extracted_dir': extracted_dir
    }


def read_parquet_to_zarr_store(parquet_file):
    """Read parquet and convert to zarr store."""
    df = pd.read_parquet(parquet_file)
    zstore = {}

    for _, row in df.iterrows():
        key = row['key']
        value = row['value']

        if isinstance(value, bytes):
            value = value.decode('utf-8')

        if isinstance(value, str):
            if value.startswith('[') or value.startswith('{'):
                try:
                    value = json.loads(value)
                except:
                    pass

        zstore[key] = value

    # Remove version key if exists
    if 'version' in zstore:
        del zstore['version']

    log_checkpoint(f"Loaded parquet: {parquet_file.name} ({len(zstore)} entries)")
    return zstore


def create_parquet_simple(zstore, output_file):
    """Save zarr store as parquet (simple version)."""
    data = []
    for key, value in zstore.items():
        if isinstance(value, str):
            encoded_value = value.encode('utf-8')
        elif isinstance(value, (list, dict)):
            encoded_value = json.dumps(value).encode('utf-8')
        else:
            encoded_value = str(value).encode('utf-8')
        data.append((key, encoded_value))

    df = pd.DataFrame(data, columns=['key', 'value'])
    df.to_parquet(output_file)
    log_checkpoint(f"✅ Saved parquet: {output_file} ({len(df)} rows)")
    return df


# ==============================================================================
# STAGE 0: CHECK GCS TEMPLATES (OPTIONAL)
# ==============================================================================

def check_stage0_templates(test_members):
    """Check if Stage 0 GCS templates exist for all test members."""
    log_stage(0, "CHECK GCS TEMPLATES (Optional)")

    try:
        import gcsfs

        log_checkpoint("Checking for pre-built GCS templates...")
        gcs_fs = gcsfs.GCSFileSystem(token=GCP_SERVICE_ACCOUNT)

        templates_found = {}

        for member in test_members:
            # Map member name to GCS path format
            # Directory: ens_01, control -> ens_control
            # Filename: ens01, control -> control (no change for control, remove underscore for others)
            if member == 'control':
                gcs_member = 'ens_control'
                gcsmember = 'control'  # Filename stays as 'control'
            else:
                # Keep the underscore format for directory (ens_01 stays ens_01)
                gcs_member = member
                # Remove underscore for filename (ens_01 -> ens01)
                gcsmember = member.replace("_", "")

            # Check for reference date templates
            # Path structure: gs://gik-fmrc/v2ecmwf_fmrc/ens_01/ecmwf-{REFERENCE_DATE}00-ens01-rt000.par
            sample_path = f"gs://{GCS_BUCKET}/{GCS_BASE_PATH}/{gcs_member}/ecmwf-{REFERENCE_DATE}00-{gcsmember}-rt000.par"

            if gcs_fs.exists(sample_path):
                templates_found[member] = True
                log_checkpoint(f"✅ Templates found for {member} at {gcs_member}")
            else:
                templates_found[member] = False
                log_checkpoint(f"⚠️ Templates NOT found for {member} at {sample_path}")

        all_found = all(templates_found.values())

        if all_found:
            log_checkpoint(f"✅ All GCS templates FOUND for reference date {REFERENCE_DATE}")
            return True
        else:
            missing = [m for m, found in templates_found.items() if not found]
            log_checkpoint(f"⚠️ Missing templates for: {', '.join(missing)}")
            log_checkpoint(f"   Stage 2 will skip missing members!")
            return False

    except Exception as e:
        log_checkpoint(f"⚠️ Cannot check GCS: {e}")
        log_checkpoint(f"   Stage 2 will be skipped in this test")
        return False


# ==============================================================================
# STAGE 1: USE PREBUILT PARQUET FILES (or scan GRIB for hours 0,3 only)
# ==============================================================================

def test_stage1_prebuilt(member_parquets, test_date, test_run):
    """Test Stage 1: Use prebuilt parquet files instead of scanning GRIB."""
    log_stage(1, "USE PREBUILT PARQUET FILES")

    start_time = time.time()

    try:
        # We'll use the prebuilt parquet files directly
        log_checkpoint(f"Using prebuilt parquet files for {len(member_parquets)} members")

        # For demonstration, let's load one member to show the structure
        first_member = list(member_parquets.keys())[0]
        first_parquet = member_parquets[first_member]

        log_checkpoint(f"Loading sample member: {first_member}")
        sample_store = read_parquet_to_zarr_store(first_parquet)

        # These prebuilt parquets are already deflated stores from scan_grib processing
        # They represent the output of Stage 1 (hours 0 and 3 only)

        elapsed = time.time() - start_time

        log_checkpoint(f"✅ Stage 1 Complete (using prebuilt)!")
        log_checkpoint(f"   Time: {elapsed:.1f} seconds")
        log_checkpoint(f"   Members available: {len(member_parquets)}")
        log_checkpoint(f"   Sample store refs: {len(sample_store)}")
        log_checkpoint(f"   Note: These are from scan_grib of hours 0 and 3 only")

        # Return dictionary of deflated stores
        deflated_stores = {}
        for member, parquet_path in member_parquets.items():
            deflated_stores[member] = read_parquet_to_zarr_store(parquet_path)

        return deflated_stores

    except Exception as e:
        log_checkpoint(f"❌ Stage 1 Failed: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_stage1_scan_grib_fallback(test_date, test_run, test_member='ens_01'):
    """Fallback: Test Stage 1 using scan_grib for hours 0 and 3 only."""
    log_stage("1-FALLBACK", "SCAN GRIB (Hours 0 and 3 only)")

    start_time = time.time()

    try:
        from kerchunk.grib2 import scan_grib, grib_tree
        from kerchunk._grib_idx import strip_datavar_chunks

        # Only process hours 0 and 3 (as per the efficient creator output)
        test_hours = [0, 3]

        # Build ECMWF URLs for test hours
        ecmwf_files = []
        for hour in test_hours:
            # ECMWF URL format
            url = (f"s3://ecmwf-forecasts/{test_date}/{test_run}z/ifs/0p25/enfo/"
                   f"{test_date}{test_run}0000-{hour}h-enfo-ef.grib2")
            ecmwf_files.append(url)
            log_checkpoint(f"Will process: {url}")

        # Extract member number from member name (e.g., ens_01 -> 1)
        if test_member == 'control':
            member_num = 0
        else:
            member_num = int(test_member.replace('ens_', ''))

        # Run Stage 1: scan_grib and build tree
        log_checkpoint(f"Running scan_grib for member {test_member} (number={member_num})...")

        all_groups = []
        for url in ecmwf_files:
            log_checkpoint(f"  Scanning {url}...")
            groups = scan_grib(
                url,
                storage_options={"anon": True},
                filter=FORECAST_DICT  # Filter variables
            )

            # Filter for specific member
            member_groups = []
            for g in groups:
                if 'attrs' in g and f'number={member_num}' in str(g['attrs']):
                    member_groups.append(g)

            all_groups.extend(member_groups)

        log_checkpoint(f"Scanned {len(all_groups)} groups for member {test_member}")

        # Build hierarchical GRIB tree
        log_checkpoint("Building GRIB tree...")
        grib_tree_store = grib_tree(all_groups)

        # Create deflated copy
        log_checkpoint("Creating deflated store...")
        deflated_store = copy.deepcopy(grib_tree_store)
        strip_datavar_chunks(deflated_store)

        # Save deflated store
        stage1_output = OUTPUT_DIR / f"stage1_{test_member}_deflated_store.json"
        with open(stage1_output, 'w') as f:
            json.dump(deflated_store, f, indent=2)

        elapsed = time.time() - start_time

        log_checkpoint(f"✅ Stage 1 Fallback Complete!")
        log_checkpoint(f"   Time: {elapsed:.1f} seconds")
        log_checkpoint(f"   Original refs: {len(grib_tree_store['refs'])}")
        log_checkpoint(f"   Deflated refs: {len(deflated_store['refs'])}")
        log_checkpoint(f"   Saved to: {stage1_output}")

        return {test_member: deflated_store}

    except Exception as e:
        log_checkpoint(f"❌ Stage 1 Fallback Failed: {e}")
        import traceback
        traceback.print_exc()
        return None


# ==============================================================================
# HELPER FUNCTIONS FROM ecmwf_index_processor.py
# ==============================================================================

def parse_grib_index(idx_url, member_filter=None):
    """
    Parse ECMWF GRIB index file (JSON format) to extract byte ranges and metadata.
    Imported from ecmwf_index_processor.py

    Args:
        idx_url: URL to the .index file
        member_filter: Filter for specific member name (e.g., 'control', 'ens01')

    Returns:
        List of index entries with byte ranges and metadata
    """
    import fsspec
    import json

    try:
        fs = fsspec.filesystem("s3", anon=True)

        entries = []
        with fs.open(idx_url, 'r') as f:
            for line_num, line in enumerate(f):
                if not line.strip():
                    continue

                # Parse JSON entry
                entry_data = json.loads(line.strip())

                # Parse ensemble member number
                member_num = int(entry_data.get('number', 0))
                if member_num == 0:
                    member = 'control'
                else:
                    member = f'ens{member_num:02d}'

                # Filter by member if specified
                if member_filter and member != member_filter:
                    continue

                # Extract metadata
                entry = {
                    'byte_offset': entry_data['_offset'],
                    'byte_length': entry_data['_length'],
                    'variable': entry_data.get('param', ''),
                    'level': entry_data.get('levtype', ''),
                    'step': entry_data.get('step', '0'),
                    'member': member,
                    'date': entry_data.get('date', ''),
                    'time': entry_data.get('time', ''),
                    'line_num': line_num
                }

                entries.append(entry)

        return entries

    except Exception as e:
        log_checkpoint(f"Error parsing index {idx_url}: {e}")
        return []


def create_references_from_index(grib_url, idx_entries, file_size=None):
    """
    Create kerchunk references using index byte ranges.
    Imported from ecmwf_index_processor.py

    Args:
        grib_url: URL to the GRIB file
        idx_entries: Parsed index entries (list of dicts)
        file_size: Total file size (for last entry)

    Returns:
        Dictionary of kerchunk references
    """
    import fsspec

    references = {}

    # Get file size if needed and not provided
    if file_size is None and any(e['byte_length'] == -1 for e in idx_entries):
        try:
            fs = fsspec.filesystem("s3", anon=True)
            info = fs.info(grib_url)
            file_size = info['size']
        except:
            log_checkpoint(f"Could not get file size for {grib_url}")

    # Create references for each entry
    for entry in idx_entries:
        # Determine byte range
        start = entry['byte_offset']
        length = entry['byte_length']

        if length == -1:
            if file_size:
                length = file_size - start
            else:
                continue  # Skip if we can't determine length

        # Create reference key
        var_name = entry['variable'].lower().replace(' ', '_')
        level_name = entry['level'].replace(' ', '_')
        member_name = entry['member']

        # Build zarr-style key
        key = f"{var_name}/{level_name}/{member_name}/0.0.0"

        # Store reference [url, offset, length]
        references[key] = [grib_url, start, length]

    # Add zarr metadata
    references['.zgroup'] = json.dumps({"zarr_format": 2})

    return references


# ==============================================================================
# STAGE 2: INDEX + GCS TEMPLATES → MERGED REFERENCES (GEFS Pattern)
# ==============================================================================

def test_stage2_with_gcs_templates(test_date, test_run, test_members):
    """
    Test Stage 2: INDEX + GCS Templates → Merged References (GEFS Pattern).

    This is the critical Stage 2 integration that was missing from ECMWF.
    Now follows the same pattern as GEFS:

    1. Read fresh .index files from S3 (target date) - byte positions
    2. Read GCS template parquet files (reference date) - zarr structure
    3. Merge them using merge_with_gcs_template()

    This enables:
    - Fast daily processing (only parse small index files)
    - Reusable templates (one-time preprocessing)
    - Consistent zarr structure
    """
    log_stage(2, "INDEX + GCS TEMPLATES → MERGED REFERENCES (All 85 hours)")

    start_time = time.time()

    try:
        # Import from ecmwf_index_processor.py
        from ecmwf_index_processor import build_complete_parquet_from_indices, save_parquet

        log_checkpoint(f"Using GEFS-style integration with GCS templates")
        log_checkpoint(f"   Target date: {test_date} (fresh index from S3)")
        log_checkpoint(f"   Reference date: {REFERENCE_DATE} (templates from GCS)")
        log_checkpoint(f"   GCS Bucket: gs://{GCS_BUCKET}/{GCS_BASE_PATH}")
        log_checkpoint(f"   Service Account: {GCP_SERVICE_ACCOUNT}")
        log_checkpoint(f"   Processing {len(test_members)} members")
        log_checkpoint(f"   Forecast hours: {len(ECMWF_FORECAST_HOURS)} total (0-360h)")

        member_results = {}

        for member in test_members:
            log_checkpoint(f"\n{'='*60}")
            log_checkpoint(f"Processing member: {member}")
            log_checkpoint(f"{'='*60}")

            # Normalize member format: ens_01 -> ens01, control -> control
            if member == 'control':
                member_normalized = 'control'
            else:
                # Handle both ens_01 and ens01 formats
                member_normalized = member.replace('_', '')  # ens_01 -> ens01
                # Ensure it's in ensNN format
                if member_normalized.startswith('ens'):
                    member_num_str = member_normalized.replace('ens', '')
                    member_normalized = f'ens{int(member_num_str):02d}'

            log_checkpoint(f"   Member format: {member} → {member_normalized}")

            try:
                # Build with GCS template merge enabled
                # This calls the real function from ecmwf_index_processor.py
                # Note: gcs_bucket, gcs_base_path, service_account_json are hard-coded
                # inside ecmwf_index_processor.py (not parameters)
                refs = build_complete_parquet_from_indices(
                    date_str=test_date,
                    run=test_run,
                    member_name=member_normalized,
                    hours=ECMWF_FORECAST_HOURS,
                    use_gcs_template=True,  # ⭐ ENABLE GCS TEMPLATE MERGE ⭐
                    gcs_template_date=REFERENCE_DATE
                )

                if refs:
                    # Save merged references
                    stage2_output = OUTPUT_DIR / f"stage2_{member}_merged.parquet"

                    # Convert to DataFrame format for parquet
                    df_data = []
                    for key, value in refs.items():
                        if isinstance(value, str):
                            encoded_value = value.encode('utf-8')
                        elif isinstance(value, (list, dict)):
                            encoded_value = json.dumps(value).encode('utf-8')
                        else:
                            encoded_value = str(value).encode('utf-8')
                        df_data.append((key, encoded_value))

                    df = pd.DataFrame(df_data, columns=['key', 'value'])
                    df.to_parquet(stage2_output)

                    log_checkpoint(f"   Saved: {stage2_output}")

                    member_results[member] = refs

                    # Log merge statistics
                    log_checkpoint(f"✅ {member}: {len(refs)} total references")
                    if '_template_entries' in refs:
                        log_checkpoint(f"   Template entries: {refs['_template_entries']}")
                    if '_index_entries' in refs:
                        log_checkpoint(f"   Index entries: {refs['_index_entries']}")
                else:
                    log_checkpoint(f"⚠️ {member}: No references created")

            except Exception as e:
                log_checkpoint(f"❌ Error processing {member}: {e}")
                import traceback
                traceback.print_exc()
                continue

        elapsed = time.time() - start_time

        log_checkpoint(f"\n{'='*60}")
        log_checkpoint(f"✅ Stage 2 Complete!")
        log_checkpoint(f"{'='*60}")
        log_checkpoint(f"   Time: {elapsed:.1f} seconds ({elapsed/60:.1f} minutes)")
        log_checkpoint(f"   Members processed: {len(member_results)}/{len(test_members)}")
        if len(member_results) > 0:
            log_checkpoint(f"   Average time per member: {elapsed/len(member_results):.1f}s")
        log_checkpoint(f"   Method: GCS template + fresh index merge (GEFS pattern)")

        return member_results

    except Exception as e:
        log_checkpoint(f"❌ Stage 2 Failed: {e}")
        import traceback
        traceback.print_exc()
        return None


# ==============================================================================
# STAGE 3: CREATE FINAL ZARR STORE (All 85 timesteps)
# ==============================================================================

def test_stage3(deflated_stores, stage2_refs, test_date):
    """Test Stage 3: Merge Stage 1 (structure) + Stage 2 (all data) to create final zarr."""
    log_stage(3, "CREATE FINAL ZARR STORE (All 85 timesteps)")

    if not deflated_stores or not stage2_refs:
        log_checkpoint("⚠️ Skipping Stage 3: Missing required inputs from previous stages")
        return None

    start_time = time.time()

    try:
        # Generate time dimensions for ALL 85 hours
        log_checkpoint("Calculating time dimensions for all 85 forecast hours...")
        axes = generate_ecmwf_axes(test_date)

        # Get all forecast hours
        all_hours = ECMWF_FORECAST_HOURS
        times = axes[1][:len(all_hours)]
        valid_times = axes[0][:len(all_hours)]
        steps = pd.TimedeltaIndex([pd.Timedelta(hours=h) for h in all_hours])

        log_checkpoint(f"   Time dimensions: {len(all_hours)} timesteps")

        results = {}

        # Process each member
        for member in deflated_stores.keys():
            if member not in stage2_refs:
                log_checkpoint(f"⚠️ Skipping {member}: No Stage 2 references available")
                continue

            log_checkpoint(f"\n{'='*60}")
            log_checkpoint(f"Processing {member}...")
            log_checkpoint(f"{'='*60}")

            deflated_store = deflated_stores[member]
            complete_refs = stage2_refs[member]

            # Merge deflated store (structure from hours 0,3) with complete refs (all 85 hours)
            log_checkpoint(f"   Merging Stage 1 structure with Stage 2 complete references...")

            # Start with deflated store structure
            if isinstance(deflated_store, dict) and 'refs' in deflated_store:
                final_store = deflated_store.get('refs', {}).copy()
            else:
                final_store = deflated_store.copy()

            # Update with all timestep references from Stage 2
            for key, ref in complete_refs.items():
                if not key.startswith('_'):  # Skip metadata
                    final_store[key] = ref

            log_checkpoint(f"   Stage 1 entries: {len(deflated_store)}")
            log_checkpoint(f"   Stage 2 entries: {len(complete_refs)}")
            log_checkpoint(f"   Final store entries: {len(final_store)}")

            # Save final zarr store as parquet
            stage3_output = OUTPUT_DIR / f"stage3_{member}_final.parquet"
            create_parquet_simple(final_store, stage3_output)

            results[member] = (final_store, stage3_output)

            log_checkpoint(f"   ✅ {member} complete: {len(final_store)} entries")

        elapsed = time.time() - start_time

        log_checkpoint(f"\n{'='*60}")
        log_checkpoint(f"✅ Stage 3 Complete!")
        log_checkpoint(f"{'='*60}")
        log_checkpoint(f"   Time: {elapsed:.1f} seconds ({elapsed/60:.1f} minutes)")
        log_checkpoint(f"   Members processed: {len(results)}")

        return results

    except Exception as e:
        log_checkpoint(f"❌ Stage 3 Failed: {e}")
        import traceback
        traceback.print_exc()
        return None


# ==============================================================================
# VALIDATION: TEST OPENING WITH XARRAY
# ==============================================================================

def validate_final_outputs(stage3_results):
    """Validate that the final parquets can be opened with xarray."""
    log_stage("VALIDATE", "TEST XARRAY OPENING")

    if not stage3_results:
        log_checkpoint("⚠️ Skipping validation: No results to test")
        return

    import fsspec

    validation_results = {}

    for member, (zstore, parquet_file) in stage3_results.items():
        log_checkpoint(f"\nValidating {member}...")

        try:
            log_checkpoint(f"   Creating fsspec reference filesystem...")
            fs = fsspec.filesystem(
                "reference",
                fo={'refs': zstore, 'version': 1},
                remote_protocol='s3',
                remote_options={'anon': True}
            )
            mapper = fs.get_mapper("")

            log_checkpoint(f"   Opening with xarray.open_datatree()...")
            dt = xr.open_datatree(mapper, engine="zarr", consolidated=False)

            # Check structure
            variables = list(dt.keys())

            validation_results[member] = {
                'success': True,
                'variables': len(variables),
                'parquet_file': parquet_file
            }

            log_checkpoint(f"   ✅ {member}: Successfully opened! {len(variables)} variables")

        except Exception as e:
            validation_results[member] = {
                'success': False,
                'error': str(e)
            }
            log_checkpoint(f"   ❌ {member}: Validation failed: {e}")

    # Summary
    successful = sum(1 for r in validation_results.values() if r['success'])

    log_checkpoint(f"\n✅ VALIDATION COMPLETE!")
    log_checkpoint(f"   Successful: {successful}/{len(validation_results)}")

    return validation_results


# ==============================================================================
# MAIN TEST ROUTINE
# ==============================================================================

def main():
    """Run all three stages using prebuilt zip files."""
    parser = argparse.ArgumentParser(description='Test ECMWF three-stage processing with prebuilt files')
    parser.add_argument('--zip', type=str, help='Specific zip file to use')
    parser.add_argument('--skip-gcs-check', action='store_true', help='Skip GCS template checking (deprecated, no longer used)')
    parser.add_argument('--max-members', type=int, default=None, help='Maximum number of members to process (default: all)')
    args = parser.parse_args()

    print("\n🚀 Starting ECMWF Three-Stage Test with Prebuilt Files\n")

    overall_start = time.time()

    # Find zip file to use
    if args.zip:
        zip_file = Path(args.zip)
        if not zip_file.exists():
            print(f"❌ Specified zip file not found: {zip_file}")
            return
    else:
        # Find any available zip file
        zip_files = sorted(glob("ecmwf_*_efficient.zip"))
        if not zip_files:
            print("❌ No ecmwf_*_efficient.zip files found in current directory")
            print("   Please run ecmwf_ensemble_par_creator_efficient.py first to create them")
            return
        zip_file = Path(zip_files[0])
        print(f"Using zip file: {zip_file}")

    # Extract date and run from filename
    try:
        test_date, test_run = extract_date_run_from_zip(zip_file.name)
        print(f"Extracted TEST_DATE: {test_date}")
        print(f"Extracted TEST_RUN: {test_run}")
    except ValueError as e:
        print(f"❌ {e}")
        return

    # Create output directory
    OUTPUT_DIR.mkdir(exist_ok=True)

    print("="*80)
    print("ECMWF THREE-STAGE PROCESSING TEST (PREBUILT)")
    print("="*80)
    print(f"Test Date: {test_date}")
    print(f"Test Run: {test_run}")
    print(f"Zip File: {zip_file}")
    print(f"Output Dir: {OUTPUT_DIR}")
    print("="*80)

    # Unzip and prepare
    extract_dir = OUTPUT_DIR / "extracted"
    extraction_info = unzip_and_prepare(zip_file, extract_dir)

    member_parquets = extraction_info['members']
    test_members = sorted(member_parquets.keys())

    print(f"\nFound {len(test_members)} members in zip file:")
    print(f"Members: {', '.join(test_members[:5])}" +
          (f"... and {len(test_members)-5} more" if len(test_members) > 5 else ""))

    # Apply max-members limit if specified
    if args.max_members and args.max_members < len(test_members):
        test_members = test_members[:args.max_members]
        # Also limit member_parquets
        member_parquets = {k: v for k, v in member_parquets.items() if k in test_members}
        print(f"\n⚠️ Limiting to first {args.max_members} members for testing")
        print(f"Members to process: {', '.join(test_members)}")

    # Stage 1: Use prebuilt parquet files
    deflated_stores = test_stage1_prebuilt(member_parquets, test_date, test_run)

    if not deflated_stores:
        log_checkpoint("\n⚠️ Falling back to scan_grib for Stage 1...")
        # Use first member as example
        deflated_stores = test_stage1_scan_grib_fallback(test_date, test_run, test_members[0])

    # Stage 2: Index-based processing for ALL 85 hours (NO map_from_index or GCS templates)
    stage2_refs = None
    if deflated_stores:
        # Process all ensemble members
        log_checkpoint(f"\n📊 Processing all {len(test_members)} ensemble members")
        log_checkpoint(f"   Members: {', '.join(test_members[:5])}" +
                      (f"... and {len(test_members)-5} more" if len(test_members) > 5 else ""))
        log_checkpoint(f"   This will process 85 forecast hours per member")
        log_checkpoint(f"   Estimated time: ~{len(test_members) * 2:.0f} minutes (assuming ~2 min per member)")

        stage2_refs = test_stage2_with_gcs_templates(test_date, test_run, test_members)

    # Stage 3: Create final zarr store with all 85 timesteps
    stage3_results = None
    if deflated_stores and stage2_refs:
        stage3_results = test_stage3(deflated_stores, stage2_refs, test_date)

        # Validation: Test with xarray
        if stage3_results:
            validate_final_outputs(stage3_results)

    # Summary
    total_time = time.time() - overall_start

    print("\n" + "="*80)
    print("TEST COMPLETE!")
    print("="*80)
    print(f"Total Time: {total_time:.1f} seconds ({total_time/60:.1f} minutes)")
    print(f"Output Directory: {OUTPUT_DIR}")

    print("\n📁 Generated Files:")
    for f in sorted(OUTPUT_DIR.glob("*.parquet")):
        size_kb = f.stat().st_size / 1024
        print(f"   - {f.name} ({size_kb:.1f} KB)")

    print("\n📝 Summary:")
    print(f"   Zip file used: {zip_file.name}")
    print(f"   Members tested: {len(test_members)}")
    print(f"   Stage 1: ✅ Used prebuilt parquet files (hours 0, 3)")

    if stage2_refs:
        print(f"   Stage 2: ✅ Index-based processing (all 85 forecast hours)")
        print(f"            Method: Direct index parsing (NO map_from_index)")
        print(f"            Members: {len(stage2_refs)}")
    else:
        print(f"   Stage 2: ⚠️ Skipped or failed")

    if stage3_results:
        print(f"   Stage 3: ✅ Created final zarr stores")
        print(f"            Members: {len(stage3_results)}")
    else:
        print(f"   Stage 3: ⚠️ Skipped (requires Stage 2)")

    print("\n📝 Next Steps:")
    if stage3_results:
        print("   1. ✅ All three stages working successfully!")
        print("   2. All 51 ensemble members processed")
        print("   3. Process different dates by providing different zip files")
        print("   4. Use ecmwf_index_processor.py for production processing")
    else:
        print("   1. Check error messages above to diagnose issues")
        print("   2. Verify S3 access to ecmwf-forecasts bucket")
        print("   3. Ensure index files exist for the test date")

    print("="*80)


if __name__ == "__main__":
    main()