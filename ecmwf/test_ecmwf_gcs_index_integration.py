#!/usr/bin/env python3
"""
ECMWF GCS Template + Index Integration Test

Tests the critical Stage 2 integration using existing methods from
ecmwf_index_processor.py:
- build_complete_parquet_from_indices()
- process_single_member()
- merge_with_gcs_template()

Uses correct GCS paths where par files are located:
gs://gik-fmrc/v2ecmwf_fmrc/ens_control/ecmwf-{date}00-control-rt{hour:03d}.par
gs://gik-fmrc/v2ecmwf_fmrc/ens_01/ecmwf-{date}00-ens01-rt{hour:03d}.par

These par files are created from ecmwf_par_to_ensemble_members.py,
where all ensemble members from single par (run_ecmwf_preprocessing.py)
are split into individual member par files.

Usage:
    python test_ecmwf_gcs_index_integration.py --date 20250101 --member control
"""

import argparse
import logging
import sys
import time
from pathlib import Path

# Import existing methods from ecmwf_index_processor
from ecmwf_index_processor import (
    process_single_member,
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

# Configuration
GCS_BUCKET = "gik-fmrc"
GCS_BASE_PATH = "v2ecmwf_fmrc"
SERVICE_ACCOUNT_JSON = "/home/roller/Documents/08-2023/impact_weather_icpac/lab/icpac_gcp/e4drr/gcp-coiled-sa-20250310/coiled-data-e4drr_202505.json"


def verify_gcs_template_exists(reference_date: str, member: str) -> bool:
    """
    Verify that GCS template exists for the reference date and member.

    Args:
        reference_date: Reference date (YYYYMMDD)
        member: Member name (control, ens01, etc.)

    Returns:
        True if template exists
    """
    try:
        import gcsfs
        import json

        # Load service account
        with open(SERVICE_ACCOUNT_JSON, 'r') as f:
            service_account_info = json.load(f)

        # Create GCS filesystem
        gcs_fs = gcsfs.GCSFileSystem(
            token=service_account_info,
            project=service_account_info.get('project_id')
        )

        # Build GCS path
        # Pattern: gs://gik-fmrc/v2ecmwf_fmrc/ens_control/ecmwf-2024052900-control-rt000.par
        if member == 'control':
            member_dir = 'ens_control'
            member_name = 'control'
        else:
            # ens01 -> ens_01
            member_num = member.replace('ens', '')
            member_dir = f'ens_{member_num}'
            member_name = member  # Keep as ens01

        # Check first hour template (rt000)
        gcs_path = f"{GCS_BUCKET}/{GCS_BASE_PATH}/{member_dir}/ecmwf-{reference_date}00-{member_name}-rt000.par"

        if gcs_fs.exists(gcs_path):
            logger.info(f"✅ GCS template exists: gs://{gcs_path}")
            return True
        else:
            logger.warning(f"❌ GCS template NOT found: gs://{gcs_path}")
            return False

    except Exception as e:
        logger.error(f"Error checking GCS template: {e}")
        return False


def inspect_gcs_template(reference_date: str, member: str, hour: int = 0):
    """
    Inspect GCS template structure to understand what we're merging with.

    Args:
        reference_date: Reference date (YYYYMMDD)
        member: Member name
        hour: Forecast hour (default 0)
    """
    try:
        import gcsfs
        import json
        import pandas as pd

        logger.info(f"\n{'='*80}")
        logger.info(f"INSPECTING GCS TEMPLATE STRUCTURE")
        logger.info(f"{'='*80}")

        # Load service account
        with open(SERVICE_ACCOUNT_JSON, 'r') as f:
            service_account_info = json.load(f)

        # Create GCS filesystem
        gcs_fs = gcsfs.GCSFileSystem(
            token=service_account_info,
            project=service_account_info.get('project_id')
        )

        # Build GCS path
        if member == 'control':
            member_dir = 'ens_control'
            member_name = 'control'
        else:
            member_num = member.replace('ens', '')
            member_dir = f'ens_{member_num}'
            member_name = member

        gcs_path = f"{GCS_BUCKET}/{GCS_BASE_PATH}/{member_dir}/ecmwf-{reference_date}00-{member_name}-rt{hour:03d}.par"

        logger.info(f"Loading: gs://{gcs_path}")

        # Read parquet
        template = pd.read_parquet(f"gs://{gcs_path}", filesystem=gcs_fs)

        logger.info(f"\nTemplate Structure:")
        logger.info(f"  Columns: {list(template.columns)}")
        logger.info(f"  Shape: {template.shape}")
        logger.info(f"  Size: {len(template)} rows")

        logger.info(f"\nColumn Types:")
        for col, dtype in template.dtypes.items():
            logger.info(f"  {col}: {dtype}")

        logger.info(f"\nFirst 3 rows:")
        print(template.head(3))

        logger.info(f"\nSample values for key columns:")
        key_cols = ['key', 'value', 'varname', 'level', 'typeOfLevel', 'uri', 'offset', 'length']
        for col in key_cols:
            if col in template.columns:
                sample = template[col].iloc[0] if len(template) > 0 else None
                logger.info(f"  {col}: {sample}")

        logger.info(f"{'='*80}\n")

        return template

    except Exception as e:
        logger.error(f"Error inspecting template: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_integration(
    target_date: str,
    reference_date: str,
    member: str,
    run: str = "00",
    inspect_template: bool = False,
    test_hours: int = None
):
    """
    Test GCS template + index integration using existing methods.

    Args:
        target_date: Target date for fresh index (YYYYMMDD)
        reference_date: Reference date for GCS template (YYYYMMDD)
        member: Member name (control, ens01, etc.)
        run: Run hour (default "00")
        inspect_template: Whether to inspect template structure first
        test_hours: Number of hours to test (None = all 85)
    """
    logger.info(f"\n{'='*80}")
    logger.info(f"ECMWF GCS TEMPLATE + INDEX INTEGRATION TEST")
    logger.info(f"{'='*80}")
    logger.info(f"Target Date:      {target_date}")
    logger.info(f"Reference Date:   {reference_date}")
    logger.info(f"Member:           {member}")
    logger.info(f"Run:              {run}Z")
    logger.info(f"Service Account:  {SERVICE_ACCOUNT_JSON}")
    logger.info(f"GCS Bucket:       gs://{GCS_BUCKET}/{GCS_BASE_PATH}")
    logger.info(f"{'='*80}\n")

    # Step 1: Verify GCS template exists
    logger.info("Step 1: Verifying GCS template exists...")
    if not verify_gcs_template_exists(reference_date, member):
        logger.error("GCS template not found. Cannot proceed.")
        logger.error("Please run ecmwf_par_to_ensemble_members.py first to create templates.")
        return None

    # Step 2: Inspect template (optional)
    if inspect_template:
        logger.info("\nStep 2: Inspecting GCS template structure...")
        template = inspect_gcs_template(reference_date, member, hour=0)
        if template is None:
            logger.error("Failed to inspect template")
            return None

        logger.info("\nPress Enter to continue with integration test...")
        input()

    # Step 3: Run integration using existing process_single_member
    logger.info("\nStep 3: Running integration test...")
    logger.info("Using process_single_member() from ecmwf_index_processor.py")

    output_dir = Path(f"output_gcs_integration_test_{target_date}")

    # Determine hours to test
    hours = None
    if test_hours:
        hours = ALL_FORECAST_HOURS[:test_hours]
        logger.info(f"Testing first {test_hours} hours: {hours}")
    else:
        hours = ALL_FORECAST_HOURS
        logger.info(f"Testing all {len(ALL_FORECAST_HOURS)} hours")

    start_time = time.time()

    # Call build_complete_parquet_from_indices directly with GCS template
    from ecmwf_index_processor import build_complete_parquet_from_indices, save_parquet

    refs = build_complete_parquet_from_indices(
        date_str=target_date,
        run=run,
        member_name=member,
        hours=hours,
        use_gcs_template=True,  # ENABLE GCS template merge
        gcs_template_date=reference_date
    )

    # Save parquet
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{member}.parquet"
    success = save_parquet(refs, output_file)

    # Create result dict
    result = {
        'member': member,
        'success': success,
        'refs_count': len(refs),
        'output_file': str(output_file) if success else None,
        'error': None if success else 'Failed to save parquet'
    }

    elapsed = time.time() - start_time

    # Step 4: Report results
    logger.info(f"\n{'='*80}")
    logger.info(f"INTEGRATION TEST RESULTS")
    logger.info(f"{'='*80}")
    logger.info(f"Member:           {result['member']}")
    logger.info(f"Success:          {result['success']}")
    logger.info(f"References:       {result['refs_count']}")
    logger.info(f"Output File:      {result['output_file']}")
    logger.info(f"Time:             {elapsed:.1f} seconds ({elapsed/60:.2f} minutes)")

    if result['error']:
        logger.error(f"Error:            {result['error']}")

    logger.info(f"{'='*80}\n")

    return result


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Test ECMWF GCS Template + Index Integration"
    )

    parser.add_argument(
        "--date",
        type=str,
        required=True,
        help="Target date (YYYYMMDD) for fresh index"
    )

    parser.add_argument(
        "--reference-date",
        type=str,
        default="20240529",
        help="Reference date (YYYYMMDD) for GCS templates (default: 20240529)"
    )

    parser.add_argument(
        "--member",
        type=str,
        default="control",
        help="Member name (control, ens01-ens50)"
    )

    parser.add_argument(
        "--run",
        type=str,
        default="00",
        choices=["00", "12"],
        help="Run hour (default: 00)"
    )

    parser.add_argument(
        "--inspect-template",
        action="store_true",
        help="Inspect GCS template structure before running test"
    )

    parser.add_argument(
        "--test-hours",
        type=int,
        default=None,
        help="Number of hours to test (default: all 85)"
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
    result = test_integration(
        target_date=args.date,
        reference_date=args.reference_date,
        member=args.member,
        run=args.run,
        inspect_template=args.inspect_template,
        test_hours=args.test_hours
    )

    # Exit code
    if result and result['success']:
        print("\n✅ Integration test PASSED")
        sys.exit(0)
    else:
        print("\n❌ Integration test FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
