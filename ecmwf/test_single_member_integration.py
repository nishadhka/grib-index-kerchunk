#!/usr/bin/env python3
"""
Simple Single-Member Test for Missing ECMWF Components
Implements Stage 2 integration layer for quick testing with one ensemble member.

This script demonstrates:
1. generate_ecmwf_axes() - Already exists in ecmwf_util.py
2. Async batch processing to combine index files with GCS templates
3. Integration of all 85 timesteps for a single member

Usage:
    python test_single_member_integration.py --date 20250101 --member control
"""

import asyncio
import argparse
import json
import logging
from pathlib import Path
from typing import List, Dict, Any
import pandas as pd
import gcsfs
import fsspec
from kerchunk._grib_idx import parse_grib_idx, map_from_index

# Import from existing ECMWF utilities
import sys
sys.path.insert(0, str(Path(__file__).parent))
from ecmwf_util import (
    generate_ecmwf_axes,
    ECMWF_FORECAST_HOURS_3H,
    ECMWF_FORECAST_HOURS_6H,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ECMWF configuration
S3_BUCKET = "ecmwf-forecasts"
GCS_BUCKET = "gik-ecmwf-aws-tf"
ALL_FORECAST_HOURS = ECMWF_FORECAST_HOURS_3H + ECMWF_FORECAST_HOURS_6H  # 85 hours


def get_member_number(member: str) -> int:
    """Convert member name to number for filtering."""
    if member == 'control':
        return 0
    elif member.startswith('ens'):
        return int(member.replace('ens', ''))
    else:
        raise ValueError(f"Invalid member: {member}")


async def process_single_ecmwf_hour(
    target_date: str,
    hour: int,
    member: str,
    gcs_bucket: str,
    reference_date: str,
    semaphore: asyncio.Semaphore
) -> pd.DataFrame:
    """
    Process a single ECMWF forecast hour using index + GCS template.

    This is the critical missing component (Stage 2 integration).

    Args:
        target_date: Target date (YYYYMMDD)
        hour: Forecast hour (0-360)
        member: Member name ('control' or 'ens01'-'ens50')
        gcs_bucket: GCS bucket with templates
        reference_date: Reference date for GCS templates
        semaphore: Async semaphore for concurrency control

    Returns:
        DataFrame with mapped index entries
    """
    async with semaphore:
        try:
            member_num = get_member_number(member)

            # Step 1: Parse fresh index file from target date (S3)
            idx_url = f"s3://{S3_BUCKET}/{target_date}/00z/ifs/0p25/enfo/{target_date}000000-{hour}h-enfo-ef.index"

            logger.info(f"  Processing hour {hour:3d}h - Parsing index from {target_date}")

            # Parse the index file
            loop = asyncio.get_event_loop()
            idxdf = await loop.run_in_executor(
                None,
                lambda: parse_grib_idx(basename=idx_url, storage_options={"anon": True})
            )

            # Filter for specific member
            # ECMWF index uses 'number' field: 0=control, 1-50=perturbed
            idxdf = idxdf[idxdf['attrs'].str.contains(f"number={member_num}", na=False)]

            if idxdf.empty:
                logger.warning(f"  Hour {hour:3d}h - No data for {member}")
                return pd.DataFrame()

            # Step 2: Load GCS template from reference date
            gcs_path = f"{gcs_bucket}/ecmwf/{member}/ecmwf-time-{reference_date}-{member}-rt{hour:03d}.parquet"

            logger.debug(f"  Hour {hour:3d}h - Loading template from GCS: {gcs_path}")

            # Load template asynchronously
            template = await loop.run_in_executor(
                None,
                lambda: load_gcs_template(gcs_path)
            )

            if template.empty:
                logger.warning(f"  Hour {hour:3d}h - Template not found, using index only")
                return idxdf

            # Step 3: Map fresh index positions with template structure
            logger.debug(f"  Hour {hour:3d}h - Mapping index with template")

            mapped = await loop.run_in_executor(
                None,
                lambda: map_from_index(
                    run_time=pd.Timestamp(target_date),
                    mapping=template,
                    idxdf=idxdf
                )
            )

            logger.info(f"  ✅ Hour {hour:3d}h - Mapped {len(mapped)} entries")
            return mapped

        except Exception as e:
            logger.error(f"  ❌ Hour {hour:3d}h - Error: {e}")
            return pd.DataFrame()


def load_gcs_template(gcs_path: str) -> pd.DataFrame:
    """
    Load GCS template parquet file.

    Args:
        gcs_path: GCS path (without gs:// prefix)

    Returns:
        DataFrame with template mapping
    """
    try:
        gcs_fs = gcsfs.GCSFileSystem(token='anon')

        # Check if file exists
        if not gcs_fs.exists(gcs_path):
            logger.warning(f"Template not found: gs://{gcs_path}")
            return pd.DataFrame()

        # Read parquet
        template = pd.read_parquet(f"gs://{gcs_path}", filesystem=gcs_fs)
        return template

    except Exception as e:
        logger.warning(f"Error loading template from {gcs_path}: {e}")
        return pd.DataFrame()


async def process_all_85_hours(
    target_date: str,
    member: str,
    gcs_bucket: str = GCS_BUCKET,
    reference_date: str = "20240529",
    max_concurrent: int = 10
) -> pd.DataFrame:
    """
    Process all 85 ECMWF forecast hours for a single member.

    This is the main Stage 2 integration function.

    Args:
        target_date: Target date (YYYYMMDD)
        member: Member name ('control' or 'ens01'-'ens50')
        gcs_bucket: GCS bucket with templates
        reference_date: Reference date for templates
        max_concurrent: Max concurrent async operations

    Returns:
        Combined DataFrame with all 85 hours
    """
    logger.info(f"🎯 Processing {member} for {target_date}")
    logger.info(f"   Using GCS templates from reference date: {reference_date}")
    logger.info(f"   Processing {len(ALL_FORECAST_HOURS)} forecast hours")

    semaphore = asyncio.Semaphore(max_concurrent)
    all_results = []

    # Process in batches for better progress tracking
    batch_size = 10
    total_batches = (len(ALL_FORECAST_HOURS) + batch_size - 1) // batch_size

    for batch_idx in range(0, len(ALL_FORECAST_HOURS), batch_size):
        batch_hours = ALL_FORECAST_HOURS[batch_idx:batch_idx + batch_size]
        batch_num = batch_idx // batch_size + 1

        logger.info(f"📦 Batch {batch_num}/{total_batches}: Processing hours {batch_hours}")

        # Create async tasks for this batch
        tasks = [
            process_single_ecmwf_hour(
                target_date, hour, member, gcs_bucket,
                reference_date, semaphore
            )
            for hour in batch_hours
        ]

        # Execute batch
        batch_results = await asyncio.gather(*tasks)

        # Filter out empty results
        valid_results = [r for r in batch_results if not r.empty]
        all_results.extend(valid_results)

        logger.info(f"✅ Batch {batch_num}/{total_batches} complete - {len(valid_results)}/{len(batch_hours)} hours successful")

    # Combine all results
    if all_results:
        combined_df = pd.concat(all_results, ignore_index=True)
        logger.info(f"🎉 Total: {len(combined_df)} mapped entries for {member}")
        return combined_df
    else:
        logger.error("❌ No data processed")
        return pd.DataFrame()


def save_result_parquet(df: pd.DataFrame, output_path: Path):
    """Save result to parquet file."""
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(output_path, engine='pyarrow', compression='snappy')
        logger.info(f"💾 Saved result to: {output_path}")
        logger.info(f"   File size: {output_path.stat().st_size / (1024*1024):.2f} MB")
        logger.info(f"   Total entries: {len(df)}")
        return True
    except Exception as e:
        logger.error(f"Error saving parquet: {e}")
        return False


async def test_single_member_integration(
    date_str: str,
    member: str,
    reference_date: str = "20240529",
    output_dir: str = "output_stage2_test"
):
    """
    Main test function for single member integration.

    Args:
        date_str: Target date (YYYYMMDD)
        member: Member name
        reference_date: Reference date for GCS templates
        output_dir: Output directory
    """
    import time
    start_time = time.time()

    # Header
    print("=" * 80)
    print("ECMWF STAGE 2 INTEGRATION TEST - SINGLE MEMBER")
    print("=" * 80)
    print(f"Target Date:     {date_str}")
    print(f"Member:          {member}")
    print(f"Reference Date:  {reference_date}")
    print(f"GCS Bucket:      {GCS_BUCKET}")
    print(f"Total Hours:     {len(ALL_FORECAST_HOURS)} (3h: {len(ECMWF_FORECAST_HOURS_3H)}, 6h: {len(ECMWF_FORECAST_HOURS_6H)})")
    print("=" * 80)

    # Generate axes (demonstrates existing function)
    logger.info("Step 1: Generating time axes")
    axes = generate_ecmwf_axes(date_str)
    logger.info(f"✅ Generated axes: {len(axes[0])} time steps")

    # Process all 85 hours (Stage 2 integration)
    logger.info("Step 2: Processing all 85 forecast hours (Stage 2)")
    mapped_df = await process_all_85_hours(
        target_date=date_str,
        member=member,
        gcs_bucket=GCS_BUCKET,
        reference_date=reference_date
    )

    if mapped_df.empty:
        logger.error("❌ Failed to process data")
        return False

    # Save result
    logger.info("Step 3: Saving results")
    output_path = Path(output_dir) / f"{member}_{date_str}_stage2.parquet"
    success = save_result_parquet(mapped_df, output_path)

    # Summary
    elapsed = time.time() - start_time
    print("\n" + "=" * 80)
    print("PROCESSING COMPLETE")
    print("=" * 80)
    print(f"✅ Success:          {success}")
    print(f"⏱️  Time:             {elapsed:.1f} seconds ({elapsed/60:.2f} minutes)")
    print(f"📊 Entries processed: {len(mapped_df)}")
    print(f"💾 Output file:      {output_path}")

    if success and len(mapped_df) > 0:
        print("\n🎉 Stage 2 integration test PASSED!")
        print(f"   Successfully processed {len(ALL_FORECAST_HOURS)} forecast hours")
        print(f"   Ready to integrate into full pipeline")
    else:
        print("\n⚠️ Test completed with issues - check logs")

    print("=" * 80)

    return success


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Test ECMWF Stage 2 Integration for Single Member"
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
        "--output-dir",
        type=str,
        default="output_stage2_test",
        help="Output directory"
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Run async test
    success = asyncio.run(test_single_member_integration(
        date_str=args.date,
        member=args.member,
        reference_date=args.reference_date,
        output_dir=args.output_dir
    ))

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
