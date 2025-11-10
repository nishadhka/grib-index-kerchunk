#!/usr/bin/env python3
"""
Simple Single-Member Test for ECMWF Stage 2 Integration

This script uses the EXISTING methods from ecmwf_index_processor.py and adds:
1. Async batch processing wrapper for efficiency
2. Integration with GCS templates (via enhanced merge_with_gcs_template)
3. Single-member quick testing

IMPORTANT: This does NOT duplicate scan_grib. It uses only index-based processing.

Usage:
    python test_single_member_integration.py --date 20250101 --member control
"""

import asyncio
import argparse
import logging
import time
from pathlib import Path
from typing import List, Dict, Any
import sys

# Import existing ECMWF index processor methods
sys.path.insert(0, str(Path(__file__).parent))
from ecmwf_index_processor import (
    build_complete_parquet_from_indices,
    save_parquet,
    ALL_FORECAST_HOURS,
    HOURS_3H,
    HOURS_6H,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def process_member_async(
    date_str: str,
    member: str,
    reference_date: str = "20240529",
    run: str = "00",
    use_gcs_template: bool = True
) -> Dict[str, Any]:
    """
    Async wrapper for processing a single member using existing index processor.

    This leverages the existing build_complete_parquet_from_indices() function
    from ecmwf_index_processor.py with GCS template integration.

    Args:
        date_str: Target date (YYYYMMDD)
        member: Member name ('control' or 'ens01'-'ens50')
        reference_date: Reference date for GCS templates
        run: Run hour (default '00')
        use_gcs_template: Whether to use GCS templates (Stage 2 integration)

    Returns:
        Dictionary with processing results
    """
    logger.info(f"🎯 Processing {member} for {date_str}")
    logger.info(f"   Using GCS templates: {use_gcs_template}")
    if use_gcs_template:
        logger.info(f"   Reference date: {reference_date}")
    logger.info(f"   Total forecast hours: {len(ALL_FORECAST_HOURS)}")

    # Use existing function from ecmwf_index_processor.py
    # This already handles:
    # - Parsing JSON index files (parse_grib_index)
    # - Creating references (create_references_from_index)
    # - Processing all 85 hours
    # - Merging with GCS template (if requested)
    loop = asyncio.get_event_loop()
    refs = await loop.run_in_executor(
        None,
        lambda: build_complete_parquet_from_indices(
            date_str=date_str,
            run=run,
            member_name=member,
            hours=ALL_FORECAST_HOURS,  # All 85 hours
            use_gcs_template=use_gcs_template,
            gcs_template_date=reference_date if use_gcs_template else None
        )
    )

    return refs


async def test_single_member(
    date_str: str,
    member: str,
    reference_date: str = "20240529",
    output_dir: str = "output_stage2_test",
    use_gcs_template: bool = True
):
    """
    Test Stage 2 integration for a single member.

    Args:
        date_str: Target date (YYYYMMDD)
        member: Member name
        reference_date: Reference date for GCS templates
        output_dir: Output directory
        use_gcs_template: Whether to use GCS templates
    """
    start_time = time.time()

    # Header
    print("=" * 80)
    print("ECMWF STAGE 2 INTEGRATION TEST - SINGLE MEMBER")
    print("Using existing ecmwf_index_processor.py methods")
    print("=" * 80)
    print(f"Target Date:     {date_str}")
    print(f"Member:          {member}")
    print(f"Reference Date:  {reference_date if use_gcs_template else 'N/A (index only)'}")
    print(f"GCS Integration: {'✅ Enabled' if use_gcs_template else '❌ Disabled'}")
    print(f"Total Hours:     {len(ALL_FORECAST_HOURS)} (3h: {len(HOURS_3H)}, 6h: {len(HOURS_6H)})")
    print("=" * 80)
    print()

    # Process member
    logger.info("Processing member using build_complete_parquet_from_indices()")
    refs = await process_member_async(
        date_str=date_str,
        member=member,
        reference_date=reference_date,
        use_gcs_template=use_gcs_template
    )

    if not refs:
        logger.error("❌ Failed to process data")
        return False

    # Save result
    logger.info("Saving results to parquet")
    output_path = Path(output_dir)
    output_file = output_path / f"{member}_{date_str}_stage2.parquet"

    success = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: save_parquet(refs, output_file)
    )

    # Summary
    elapsed = time.time() - start_time
    print("\n" + "=" * 80)
    print("PROCESSING COMPLETE")
    print("=" * 80)
    print(f"✅ Success:          {success}")
    print(f"⏱️  Time:             {elapsed:.1f} seconds ({elapsed/60:.2f} minutes)")
    print(f"📊 References:       {len(refs)}")
    print(f"💾 Output file:      {output_file}")

    if output_file.exists():
        file_size = output_file.stat().st_size / (1024 * 1024)
        print(f"💾 File size:        {file_size:.2f} MB")

    if success and len(refs) > 0:
        print("\n🎉 Stage 2 integration test PASSED!")
        print(f"   Successfully processed {len(ALL_FORECAST_HOURS)} forecast hours")
        print(f"   Method: Index-based processing {'+ GCS templates' if use_gcs_template else '(no templates)'}")
        print(f"   Ready to integrate into full pipeline")
    else:
        print("\n⚠️ Test completed with issues - check logs")

    print("=" * 80)

    return success


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Test ECMWF Stage 2 Integration using existing index processor methods"
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
        "--no-gcs-template",
        action="store_true",
        help="Disable GCS template integration (index-only mode)"
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
    success = asyncio.run(test_single_member(
        date_str=args.date,
        member=args.member,
        reference_date=args.reference_date,
        output_dir=args.output_dir,
        use_gcs_template=not args.no_gcs_template
    ))

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
