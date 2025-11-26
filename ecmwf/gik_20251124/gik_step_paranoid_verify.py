#!/usr/bin/env python3
"""
GIK Stage 3 Paranoid Verification Script

This script validates that the Stage 3 `tp` field is indeed the same cumulative
precipitation field as ECMWF Open Data, with identical semantics.

Approach:
=========
For each step (default: 6, 12, 24h):

1. Call fetch_gik_stage3 with precip_window = step + 9999
   -> Forces precip_start = max(0, step - (step + 9999)) = 0
   -> Returns cumulative precipitation from 0 → step (no differencing)

2. Call fetch_ecmwf_opendata with the same precip_window
   -> Also forces precip_start = 0
   -> Returns cumulative precipitation from 0 → step

3. Compare the two cumulative fields directly:
   - Shape, min, max, mean
   - Difference stats (mean, std, RMSE, max |diff|)
   - Pearson correlation coefficient

Why This Matters:
=================
The critical assumption in the GIK method is:

    "Stage 3 tp must be the same cumulative variable as in the original ECMWF GRIB"

If Stage 3 had pre-processed tp into 6-hour accumulations, doing another difference
would be wrong. This script PROVES that Stage 3 tp is the original cumulative field.

Expected Results:
=================
If everything is correct:
- Differences should be essentially zero (< 1e-6 mm)
- Correlation should be exactly 1.0 (or 0.9999999...)
- Model run times should match

Usage:
    # Default: verify steps 6, 12, 24h
    python gik_step_paranoid_verify.py --date 20251125

    # Verify specific steps
    python gik_step_paranoid_verify.py --steps 3 6 9 12 24 48 --date 20251125

    # Also verify MSL (not just TP)
    python gik_step_paranoid_verify.py --date 20251125 --verify-msl
"""

import numpy as np
import argparse
from pathlib import Path
import sys

# Reuse functions from the comparison script
sys.path.insert(0, str(Path(__file__).parent))

from compare_ecmwf_opendata_vs_gik import (
    fetch_gik_stage3,
    fetch_ecmwf_opendata,
    DEFAULT_GIK_INPUT_DIR,
)


def compute_stats(arr1, arr2, name):
    """
    Compute comprehensive comparison statistics between two arrays.

    Returns dict with all stats for programmatic use.
    """
    stats = {}

    # Basic shape check
    stats['shape_match'] = arr1.shape == arr2.shape
    stats['shape1'] = arr1.shape
    stats['shape2'] = arr2.shape

    if not stats['shape_match']:
        print(f"  ERROR: Shape mismatch! {arr1.shape} vs {arr2.shape}")
        return stats

    # Value ranges
    stats['min1'] = float(np.min(arr1))
    stats['max1'] = float(np.max(arr1))
    stats['mean1'] = float(np.mean(arr1))
    stats['min2'] = float(np.min(arr2))
    stats['max2'] = float(np.max(arr2))
    stats['mean2'] = float(np.mean(arr2))

    # Differences
    diff = arr1 - arr2
    abs_diff = np.abs(diff)

    stats['mean_diff'] = float(np.mean(diff))
    stats['std_diff'] = float(np.std(diff))
    stats['max_abs_diff'] = float(np.max(abs_diff))
    stats['rmse'] = float(np.sqrt(np.mean(diff**2)))

    # Correlation
    # Flatten and compute Pearson correlation
    flat1 = arr1.flatten()
    flat2 = arr2.flatten()

    # Handle case where one array is constant (would cause division by zero)
    if np.std(flat1) > 0 and np.std(flat2) > 0:
        correlation = np.corrcoef(flat1, flat2)[0, 1]
        stats['correlation'] = float(correlation)
    else:
        stats['correlation'] = 1.0 if np.allclose(flat1, flat2) else 0.0

    # Determine if match
    # Using very tight tolerance for "paranoid" verification
    stats['is_match'] = stats['max_abs_diff'] < 1e-3  # 0.001 mm tolerance
    stats['is_exact'] = stats['max_abs_diff'] < 1e-6  # essentially zero

    return stats


def print_verification_result(step, var_name, stats):
    """Print formatted verification results for one step/variable."""

    print(f"\n  {var_name} at step {step}h:")
    print(f"    GIK:      min={stats['min1']:.4f}, max={stats['max1']:.4f}, mean={stats['mean1']:.4f}")
    print(f"    OpenData: min={stats['min2']:.4f}, max={stats['max2']:.4f}, mean={stats['mean2']:.4f}")
    print(f"    ---")
    print(f"    Mean diff:    {stats['mean_diff']:.10f}")
    print(f"    Std diff:     {stats['std_diff']:.10f}")
    print(f"    Max |diff|:   {stats['max_abs_diff']:.10f}")
    print(f"    RMSE:         {stats['rmse']:.10f}")
    print(f"    Correlation:  {stats['correlation']:.10f}")
    print(f"    ---")

    if stats['is_exact']:
        print(f"    Result: EXACT MATCH (diff < 1e-6)")
    elif stats['is_match']:
        print(f"    Result: MATCH (diff < 0.001)")
    else:
        print(f"    Result: DIFFERS (max diff = {stats['max_abs_diff']:.6f})")


def verify_step(step, forecast_date, forecast_time, gik_input_dir, member, verify_msl=False):
    """
    Verify cumulative TP (and optionally MSL) at a single step.

    Uses precip_window = step + 9999 to force both methods to return
    cumulative precipitation (0 -> step).
    """
    print(f"\n{'='*60}")
    print(f"VERIFYING STEP {step}h (CUMULATIVE)")
    print(f"{'='*60}")

    # Force cumulative mode by using huge precip_window
    # This ensures precip_start = max(0, step - huge) = 0
    huge_window = step + 9999

    results = {'step': step, 'tp': None, 'msl': None}

    # Fetch from GIK Stage 3
    print(f"\n  Fetching GIK Stage 3 (cumulative mode, precip_window={huge_window})...")
    try:
        msl_gik, tp_gik, model_time_gik = fetch_gik_stage3(
            step_hour=step,
            precip_window=huge_window,
            input_dir=gik_input_dir,
            member=member
        )
    except Exception as e:
        print(f"  ERROR: GIK fetch failed: {e}")
        return results

    # Fetch from ECMWF Open Data
    print(f"\n  Fetching ECMWF Open Data (cumulative mode, precip_window={huge_window})...")
    try:
        msl_od, tp_od, model_time_od = fetch_ecmwf_opendata(
            step_hour=step,
            precip_window=huge_window,
            forecast_date=forecast_date,
            forecast_time=forecast_time
        )
    except Exception as e:
        print(f"  ERROR: ECMWF Open Data fetch failed: {e}")
        return results

    # Check model run times
    print(f"\n  Model Run Times:")
    print(f"    GIK:      {model_time_gik.strftime('%Y-%m-%d %H:%M UTC') if model_time_gik else 'Unknown'}")
    print(f"    OpenData: {model_time_od.strftime('%Y-%m-%d %H:%M UTC') if model_time_od else 'Unknown'}")

    if model_time_gik and model_time_od:
        if model_time_gik == model_time_od:
            print(f"    Model run times MATCH")
        else:
            print(f"    WARNING: Model run times DIFFER!")
            print(f"        Comparison may not be valid. Use --date to specify matching date.")

    # Compare TP (cumulative precipitation in mm)
    print(f"\n  CUMULATIVE PRECIPITATION COMPARISON:")
    tp_stats = compute_stats(tp_gik, tp_od, 'TP')
    print_verification_result(step, 'TP (mm)', tp_stats)
    results['tp'] = tp_stats

    # Optionally compare MSL
    if verify_msl:
        print(f"\n  MSL COMPARISON:")
        msl_stats = compute_stats(msl_gik, msl_od, 'MSL')
        print_verification_result(step, 'MSL (hPa)', msl_stats)
        results['msl'] = msl_stats

    return results


def main():
    parser = argparse.ArgumentParser(
        description='Paranoid verification of GIK Stage 3 cumulative TP semantics',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
This script proves that Stage 3 tp is the original cumulative precipitation field
by comparing raw cumulative values at multiple forecast steps.

Examples:
    # Verify steps 6, 12, 24h for 2025-11-25 00Z run
    python gik_step_paranoid_verify.py --date 20251125

    # Verify more steps
    python gik_step_paranoid_verify.py --steps 3 6 9 12 24 48 72 --date 20251125

    # Also verify MSL
    python gik_step_paranoid_verify.py --date 20251125 --verify-msl
        '''
    )
    parser.add_argument('--steps', type=int, nargs='+', default=[6, 12, 24],
                        help='Forecast steps to verify (default: 6 12 24)')
    parser.add_argument('--date', type=str, required=True,
                        help='Forecast date in YYYYMMDD format (required to match GIK parquet)')
    parser.add_argument('--time', type=int, default=0, choices=[0, 12],
                        help='Forecast time (0 or 12, default: 0)')
    parser.add_argument('--gik-input-dir', type=Path, default=DEFAULT_GIK_INPUT_DIR,
                        help='Input directory for GIK parquet files')
    parser.add_argument('--member', type=str, default='control',
                        help='Ensemble member for GIK (default: control)')
    parser.add_argument('--verify-msl', action='store_true',
                        help='Also verify MSL (not just TP)')

    args = parser.parse_args()

    print("="*80)
    print("GIK STAGE 3 PARANOID VERIFICATION")
    print("="*80)
    print(f"Steps to verify: {args.steps}")
    print(f"Forecast date:   {args.date}")
    print(f"Forecast time:   {args.time:02d}Z")
    print(f"GIK input:       {args.gik_input_dir}")
    print(f"Member:          {args.member}")
    print(f"Verify MSL:      {args.verify_msl}")
    print("="*80)
    print("\nThis script verifies that Stage 3 tp is the SAME cumulative field")
    print("as ECMWF Open Data by forcing both methods into cumulative mode")
    print("(precip_window > step, so precip_start = 0).")
    print("="*80)

    all_results = []

    for step in args.steps:
        result = verify_step(
            step=step,
            forecast_date=args.date,
            forecast_time=args.time,
            gik_input_dir=args.gik_input_dir,
            member=args.member,
            verify_msl=args.verify_msl
        )
        all_results.append(result)

    # Final summary
    print("\n" + "="*80)
    print("VERIFICATION SUMMARY")
    print("="*80)

    print("\n  Step  |  TP Max |Diff|  |  TP Correlation  |  TP Result")
    print("  ------|-----------------|------------------|------------")

    all_passed = True
    for r in all_results:
        step = r['step']
        tp = r.get('tp')

        if tp is None:
            print(f"  {step:4d}h |      ERROR       |       ERROR      |  FAILED")
            all_passed = False
        else:
            max_diff = tp['max_abs_diff']
            corr = tp['correlation']
            result = "PASS" if tp['is_match'] else "FAIL"

            if not tp['is_match']:
                all_passed = False

            print(f"  {step:4d}h |  {max_diff:13.10f}  |  {corr:14.10f}  |  {result}")

    if args.verify_msl:
        print("\n  Step  | MSL Max |Diff|  | MSL Correlation  | MSL Result")
        print("  ------|-----------------|------------------|------------")

        for r in all_results:
            step = r['step']
            msl = r.get('msl')

            if msl is None:
                print(f"  {step:4d}h |      ERROR       |       ERROR      |  FAILED")
            else:
                max_diff = msl['max_abs_diff']
                corr = msl['correlation']
                result = "PASS" if msl['is_match'] else "FAIL"
                print(f"  {step:4d}h |  {max_diff:13.10f}  |  {corr:14.10f}  |  {result}")

    print("\n" + "="*80)

    if all_passed:
        print("FINAL RESULT: ALL STEPS PASSED")
        print("\nConclusion:")
        print("  - Stage 3 tp IS the original cumulative precipitation field")
        print("  - The GIK method is semantically equivalent to ECMWF Open Data")
        print("  - The --precip-window logic in compare_ecmwf_opendata_vs_gik.py is sound")
    else:
        print("FINAL RESULT: SOME STEPS FAILED")
        print("\nPossible issues:")
        print("  - Model run dates may not match (use --date to specify)")
        print("  - Stage 3 parquet may have different data than ECMWF Open Data")
        print("  - Check the detailed output above for specific failures")

    print("="*80)

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
