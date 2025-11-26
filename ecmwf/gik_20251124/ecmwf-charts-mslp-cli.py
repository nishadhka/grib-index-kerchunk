#!/usr/bin/env python3
"""
ECMWF Open Data - MSLP + Precipitation Chart Generator (CLI Version)

This is a command-line version of ecmwf-charts-mslp.py that allows flexible
control over step, region, and output naming.

Data Source: ECMWF Open Data API (ecmwf-opendata package)
             Downloads GRIB files directly from ECMWF servers

ECMWF Step to UTC Time Conversion:
==================================
For a 00Z model run:
    Step  0  -> 00:00 UTC (analysis)
    Step  6  -> 06:00 UTC (+6h forecast)
    Step 12  -> 12:00 UTC (+12h forecast)
    Step 24  -> 00:00 UTC next day (+24h forecast)

Output Filename Format:
=======================
    YYYYMMDD_HHZ_hrXXX_region_opendata.png

Usage:
    # Generate 6-hour forecast for Africa (matching official ECMWF chart)
    python ecmwf-charts-mslp-cli.py --step 6 --region africa

    # Generate 24-hour forecast for Europe
    python ecmwf-charts-mslp-cli.py --step 24 --region europe

    # Custom precipitation window
    python ecmwf-charts-mslp-cli.py --step 24 --precip-window 12 --region global
"""

import metview as mv
from ecmwf.opendata import Client
import numpy as np
import argparse
import datetime
import os
from pathlib import Path


def step_to_valid_time(model_run_time, step_hour):
    """Convert forecast step to valid time."""
    if model_run_time is None:
        return None
    return model_run_time + datetime.timedelta(hours=step_hour)


def generate_output_filename(model_run_time, step_hour, region):
    """Generate output filename in YYYYMMDD_HHZ_hrXXX_region format."""
    if model_run_time is None:
        return f"mslp_precip_step{step_hour:03d}_{region}_opendata"

    date_str = model_run_time.strftime('%Y%m%d')
    hour_str = model_run_time.strftime('%H')
    region_clean = region.lower().replace(' ', '_').replace('-', '_')

    return f"{date_str}_{hour_str}Z_hr{step_hour:03d}_{region_clean}_opendata"


def main():
    parser = argparse.ArgumentParser(
        description='Generate MSLP + Precipitation plots from ECMWF Open Data',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
ECMWF Step Reference (for 00Z run):
    Step  6  -> Valid: 06:00 UTC  (matches ECMWF chart "+6h")
    Step 12  -> Valid: 12:00 UTC  (matches ECMWF chart "+12h")
    Step 24  -> Valid: 00:00 UTC next day

Examples:
    # Match ECMWF official chart showing +6h forecast
    python ecmwf-charts-mslp-cli.py --step 6 --region africa

    # 24-hour forecast for Europe with 12h precip accumulation
    python ecmwf-charts-mslp-cli.py --step 24 --precip-window 12 --region europe
        '''
    )
    parser.add_argument('--step', type=int, default=6,
                        help='Forecast step in hours (default: 6)')
    parser.add_argument('--region', type=str, default='africa',
                        help='Region name for metview (default: africa)')
    parser.add_argument('--precip-window', type=int, default=6,
                        help='Precipitation accumulation window in hours (default: 6)')
    parser.add_argument('--output', type=str, default=None,
                        help='Output filename (auto-generated if not specified)')
    parser.add_argument('--cache-grib', type=str, default=None,
                        help='Path to cache downloaded GRIB file')

    args = parser.parse_args()

    print("="*80)
    print("ECMWF Open Data - MSLP + Precipitation Chart")
    print("="*80)
    print(f"Step: {args.step}h")
    print(f"Region: {args.region}")
    print(f"Precip window: {args.precip_window}h")
    print("="*80)

    # Initialize client
    client = Client("ecmwf", beta=False)

    # Determine steps needed
    precip_start = max(0, args.step - args.precip_window)
    steps_needed = sorted(list(set([precip_start, args.step])))

    print(f"\nDownloading GRIB data...")
    print(f"  Steps: {steps_needed}")
    print(f"  Variables: msl, tp")

    # Download file
    if args.cache_grib:
        filename = args.cache_grib
    else:
        filename = f'ecmwf_opendata_step{args.step}.grib'

    try:
        client.retrieve(
            step=steps_needed,
            stream="oper",
            type="fc",
            levtype="sfc",
            param=['msl', 'tp'],
            target=filename
        )
        print(f"  Downloaded: {filename}")
    except Exception as e:
        print(f"  Download failed: {e}")
        return False

    # Read with metview
    print("\nProcessing data...")
    data = mv.read(filename)

    # Extract MSL
    msl = data.select(shortName='msl', step=args.step)

    # Compute precipitation
    if precip_start == 0:
        tp = data.select(shortName='tp', step=args.step)
    else:
        tp_start = data.select(shortName='tp', step=precip_start)
        tp_end = data.select(shortName='tp', step=args.step)
        tp = tp_end - tp_start

    # Convert units
    msl = msl / 100  # Pa to hPa
    tp = tp * 1000   # m to mm

    # Get model run time from GRIB
    base_date = mv.grib_get(msl, ['dataDate', 'dataTime'])
    date_str = str(int(base_date[0]))
    time_str = str(int(base_date[1])).zfill(4)

    year = int(date_str[:4])
    month = int(date_str[4:6])
    day = int(date_str[6:8])
    hour = int(time_str[:2])

    model_run_time = datetime.datetime(year, month, day, hour)
    valid_time = step_to_valid_time(model_run_time, args.step)

    print(f"  Model run: {model_run_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Valid time: {valid_time.strftime('%Y-%m-%d %H:%M:%S UTC')} (+{args.step}h)")

    # Generate output filename
    if args.output:
        output_name = args.output
    else:
        output_name = generate_output_filename(model_run_time, args.step, args.region)

    print(f"  Output: {output_name}.png")

    # Define map settings
    coast = mv.mcoast(
        map_coastline_colour="charcoal",
        map_coastline_resolution="medium",
        map_coastline_land_shade="on",
        map_coastline_land_shade_colour="cream",
        map_coastline_sea_shade="off",
        map_boundaries="on",
        map_boundaries_colour="charcoal",
        map_boundaries_thickness=1,
        map_disputed_boundaries="off",
        map_grid_colour="tan",
        map_label_height=0.35,
    )

    view = mv.geoview(
        area_mode="name",
        area_name=args.region,
        coastlines=coast
    )

    # Define styles
    tp_shade = mv.mcont(
        legend="on",
        contour_automatics_settings="style_name",
        contour_style_name="sh_blured_f05t300lst"
    )

    msl_shade = mv.mcont(
        legend="off",
        contour_automatics_settings="style_name",
        contour_style_name="ct_blk_i5_t2"
    )

    # Title with GRIB metadata
    title = mv.mtext(
        text_lines=[
            "Rain and mean sea level pressure (ECMWF Open Data)",
            f"START TIME: <grib_info key='base-date' format='%a %d %B %Y %H' where='shortName=msl' /> UTC",
            f"VALID TIME: <grib_info key='valid-date' format='%a %d %B %Y %H' where='shortName=msl' /> UTC, STEP: {args.step}h"
        ],
        text_font_size=0.4,
        text_colour='charcoal'
    )

    ecmwf_text = mv.mtext(
        text_lines=[
            "Data source: ECMWF Open Data API (ecmwf-opendata)",
            "© European Centre for Medium-Range Weather Forecasts (ECMWF)",
            "Source: www.ecmwf.int Licence: CC-BY-4.0 and ECMWF Terms of Use"
        ],
        text_justification='center',
        text_font_size=0.3,
        text_mode="positional",
        text_box_x_position=6.,
        text_box_y_position=-0.2,
        text_box_x_length=8,
        text_box_y_length=2,
        text_colour='charcoal'
    )

    # Generate plot
    print("\nGenerating plot...")

    png = mv.png_output(
        output_name=output_name,
        output_title=f"MSLP + Precip Step {args.step}h",
        output_width=1000,
    )
    mv.setoutput(png)
    mv.plot(view, tp, tp_shade, msl, msl_shade, title, ecmwf_text)

    # Rename output file if needed
    if os.path.exists(f"{output_name}.1.png"):
        os.rename(f"{output_name}.1.png", f"{output_name}.png")

    print(f"\nSaved: {output_name}.png")

    print("\n" + "="*80)
    print("Done!")
    print("="*80)

    return True


if __name__ == "__main__":
    success = main()
    if not success:
        exit(1)
