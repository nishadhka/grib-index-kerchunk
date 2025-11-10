#!/usr/bin/env python3
"""
ECMWF GCS Template + Index Integration Test

Tests the critical Stage 2 integration that merges:
1. GCS templates (pre-built structure from reference date)
2. Fresh index files (byte positions from target date)

This is NOT using standard kerchunk parsers because ECMWF uses
a custom JSON .index format that requires custom parsing.

Key challenge: Properly merging two DataFrames with different structures.

Usage:
    python test_ecmwf_gcs_index_integration.py --date 20250101 --member control
"""

import asyncio
import argparse
import logging
import time
import json
from pathlib import Path
from typing import Dict, Any, List
import sys

import pandas as pd
import gcsfs
import fsspec

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ECMWF configuration
HOURS_3H = list(range(0, 145, 3))
HOURS_6H = list(range(150, 361, 6))
ALL_FORECAST_HOURS = HOURS_3H + HOURS_6H  # 85 hours

S3_BUCKET = "ecmwf-forecasts"
GCS_BUCKET = "gik-ecmwf-aws-tf"


def parse_ecmwf_json_index(idx_url: str, member_filter: str = None) -> pd.DataFrame:
    """
    Parse ECMWF's custom JSON .index format.

    ECMWF uses .index files with JSON format (one JSON object per line),
    NOT the standard GRIB .idx format that kerchunk's parse_grib_idx expects.

    Args:
        idx_url: URL to ECMWF .index file (JSON format)
        member_filter: Member name to filter (e.g., 'control', 'ens01')

    Returns:
        DataFrame with parsed index entries
    """
    try:
        fs = fsspec.filesystem("s3", anon=True)

        entries = []
        with fs.open(idx_url, 'r') as f:
            for line_num, line in enumerate(f):
                if not line.strip():
                    continue

                # Parse JSON entry (ECMWF specific format)
                entry_data = json.loads(line.strip())

                # Extract member number
                member_num = int(entry_data.get('number', 0))
                if member_num == 0:
                    member = 'control'
                else:
                    member = f'ens{member_num:02d}'

                # Filter by member if specified
                if member_filter and member != member_filter:
                    continue

                # Build entry
                entry = {
                    'byte_offset': entry_data['_offset'],
                    'byte_length': entry_data['_length'],
                    'variable': entry_data.get('param', ''),
                    'level': entry_data.get('levtype', ''),
                    'step': entry_data.get('step', '0'),
                    'member': member,
                    'date': entry_data.get('date', ''),
                    'time': entry_data.get('time', ''),
                    'levelist': entry_data.get('levelist', ''),
                    'raw_data': entry_data  # Keep raw for debugging
                }

                entries.append(entry)

        df = pd.DataFrame(entries)
        logger.info(f"Parsed {len(df)} entries from ECMWF JSON index")
        return df

    except Exception as e:
        logger.error(f"Error parsing ECMWF index {idx_url}: {e}")
        return pd.DataFrame()


def load_gcs_template(
    reference_date: str,
    member: str,
    hour: int,
    gcs_bucket: str = GCS_BUCKET
) -> pd.DataFrame:
    """
    Load GCS template DataFrame for a specific hour.

    Args:
        reference_date: Reference date (YYYYMMDD)
        member: Member name
        hour: Forecast hour
        gcs_bucket: GCS bucket name

    Returns:
        Template DataFrame
    """
    try:
        gcs_path = f"{gcs_bucket}/ecmwf/{member}/ecmwf-time-{reference_date}-{member}-rt{hour:03d}.parquet"

        gcs_fs = gcsfs.GCSFileSystem(token='anon')

        if not gcs_fs.exists(gcs_path):
            logger.warning(f"Template not found: gs://{gcs_path}")
            return pd.DataFrame()

        # Read parquet
        template_df = pd.read_parquet(f"gs://{gcs_path}", filesystem=gcs_fs)

        logger.info(f"Loaded GCS template: {len(template_df)} entries")
        return template_df

    except Exception as e:
        logger.error(f"Error loading GCS template: {e}")
        return pd.DataFrame()


def merge_index_with_template(
    index_df: pd.DataFrame,
    template_df: pd.DataFrame,
    grib_url: str
) -> Dict[str, Any]:
    """
    Merge fresh index DataFrame with GCS template DataFrame.

    This is the CRITICAL integration step. The challenge is that:
    - Index has: byte positions, variable names, levels
    - Template has: complete structure, metadata, variable mappings

    Need to properly merge these to create complete references.

    Args:
        index_df: Fresh index from target date
        template_df: Pre-built template from GCS
        grib_url: URL to GRIB file (for references)

    Returns:
        Dictionary of merged references
    """
    logger.info("Starting DataFrame merge...")
    logger.info(f"  Index columns: {list(index_df.columns)}")
    logger.info(f"  Template columns: {list(template_df.columns)}")

    # TODO: This is where the complex merge logic goes
    # The merge strategy depends on:
    # 1. What columns are in the GCS template?
    # 2. What's the join key? (variable + level?)
    # 3. How to handle missing entries?

    # For now, create basic references from index
    references = {}

    for idx, row in index_df.iterrows():
        # Build reference key
        var_name = row['variable'].lower().replace(' ', '_')
        level_name = row['level'].replace(' ', '_')

        # Zarr-style key
        key = f"{var_name}/{level_name}/0.0.0"

        # Reference: [url, offset, length]
        references[key] = [
            grib_url,
            int(row['byte_offset']),
            int(row['byte_length'])
        ]

    # TODO: Merge with template structure
    # This is where template_df should be used to enhance the structure

    logger.info(f"Created {len(references)} merged references")
    return references


async def test_single_hour_integration(
    target_date: str,
    reference_date: str,
    member: str,
    hour: int
) -> Dict[str, Any]:
    """
    Test GCS template + index integration for a single hour.

    Args:
        target_date: Target date for fresh index
        reference_date: Reference date for GCS template
        member: Member name
        hour: Forecast hour

    Returns:
        Merged references
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"Testing hour {hour:3d}h")
    logger.info(f"{'='*60}")

    # 1. Parse fresh ECMWF JSON index
    idx_url = f"s3://{S3_BUCKET}/{target_date}/00z/ifs/0p25/enfo/{target_date}000000-{hour}h-enfo-ef.index"
    grib_url = f"s3://{S3_BUCKET}/{target_date}/00z/ifs/0p25/enfo/{target_date}000000-{hour}h-enfo-ef.grib2"

    logger.info(f"1. Parsing ECMWF JSON index: {idx_url}")
    index_df = await asyncio.get_event_loop().run_in_executor(
        None,
        parse_ecmwf_json_index,
        idx_url,
        member
    )

    if index_df.empty:
        logger.warning(f"No index data for {member} at {hour}h")
        return {}

    logger.info(f"   ✅ Index: {len(index_df)} entries")

    # 2. Load GCS template
    logger.info(f"2. Loading GCS template for reference date {reference_date}")
    template_df = await asyncio.get_event_loop().run_in_executor(
        None,
        load_gcs_template,
        reference_date,
        member,
        hour
    )

    if template_df.empty:
        logger.warning(f"No template found, using index only")
    else:
        logger.info(f"   ✅ Template: {len(template_df)} entries")

    # 3. Merge DataFrames
    logger.info(f"3. Merging index + template DataFrames")
    merged_refs = await asyncio.get_event_loop().run_in_executor(
        None,
        merge_index_with_template,
        index_df,
        template_df,
        grib_url
    )

    logger.info(f"   ✅ Merged: {len(merged_refs)} references")

    return {
        'hour': hour,
        'index_entries': len(index_df),
        'template_entries': len(template_df),
        'merged_refs': len(merged_refs),
        'references': merged_refs,
        'index_df': index_df,
        'template_df': template_df
    }


async def test_all_hours_integration(
    target_date: str,
    reference_date: str,
    member: str,
    max_hours: int = None
) -> Dict[str, Any]:
    """
    Test GCS template + index integration for all (or subset) of hours.

    Args:
        target_date: Target date
        reference_date: Reference date
        member: Member name
        max_hours: Limit number of hours to test

    Returns:
        Results for all hours
    """
    hours_to_test = ALL_FORECAST_HOURS
    if max_hours:
        hours_to_test = hours_to_test[:max_hours]

    logger.info(f"\n{'='*80}")
    logger.info(f"TESTING GCS TEMPLATE + INDEX INTEGRATION")
    logger.info(f"{'='*80}")
    logger.info(f"Target Date:     {target_date}")
    logger.info(f"Reference Date:  {reference_date}")
    logger.info(f"Member:          {member}")
    logger.info(f"Hours to test:   {len(hours_to_test)} / {len(ALL_FORECAST_HOURS)}")
    logger.info(f"{'='*80}\n")

    results = []

    for hour in hours_to_test:
        try:
            result = await test_single_hour_integration(
                target_date, reference_date, member, hour
            )
            results.append(result)
        except Exception as e:
            logger.error(f"Error processing hour {hour}: {e}")
            import traceback
            traceback.print_exc()

    # Summary
    logger.info(f"\n{'='*80}")
    logger.info(f"INTEGRATION TEST SUMMARY")
    logger.info(f"{'='*80}")
    logger.info(f"Hours tested: {len(results)} / {len(hours_to_test)}")

    successful = [r for r in results if r.get('merged_refs', 0) > 0]
    logger.info(f"Successful:   {len(successful)}")

    total_refs = sum(r.get('merged_refs', 0) for r in results)
    logger.info(f"Total refs:   {total_refs}")

    return {
        'results': results,
        'summary': {
            'hours_tested': len(results),
            'successful': len(successful),
            'total_refs': total_refs
        }
    }


def main():
    parser = argparse.ArgumentParser(
        description="Test ECMWF GCS Template + Index Integration"
    )

    parser.add_argument(
        "--date",
        type=str,
        required=True,
        help="Target date (YYYYMMDD)"
    )

    parser.add_argument(
        "--member",
        type=str,
        default="control",
        help="Member name (control, ens01-ens50)"
    )

    parser.add_argument(
        "--reference-date",
        type=str,
        default="20240529",
        help="Reference date for GCS templates (default: 20240529)"
    )

    parser.add_argument(
        "--max-hours",
        type=int,
        default=None,
        help="Limit number of hours to test (default: all 85)"
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Run test
    start_time = time.time()

    results = asyncio.run(test_all_hours_integration(
        target_date=args.date,
        reference_date=args.reference_date,
        member=args.member,
        max_hours=args.max_hours
    ))

    elapsed = time.time() - start_time

    # Final summary
    print(f"\n{'='*80}")
    print(f"TEST COMPLETE")
    print(f"{'='*80}")
    print(f"Time:          {elapsed:.1f} seconds ({elapsed/60:.2f} minutes)")
    print(f"Hours tested:  {results['summary']['hours_tested']}")
    print(f"Successful:    {results['summary']['successful']}")
    print(f"Total refs:    {results['summary']['total_refs']}")
    print(f"{'='*80}")

    # Exit code
    if results['summary']['successful'] > 0:
        print("\n✅ Integration test PASSED")
        sys.exit(0)
    else:
        print("\n❌ Integration test FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
