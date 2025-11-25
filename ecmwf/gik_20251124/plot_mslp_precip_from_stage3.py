#!/usr/bin/env python3
"""
ECMWF MSLP + Precipitation Plot from Stage 3 Parquet Files

This script creates a plot similar to ecmwf-charts-mslp.py but loads data
from the Stage 3 parquet files using the grib-index-kerchunk method instead
of the ECMWF Open Data API.

Features:
- Loads data from S3 via parquet references (no direct API calls)
- Creates MSLP contours overlaid with precipitation shading
- Uses metview for professional meteorological visualization
- Regional subsetting support

Usage:
    python plot_mslp_precip_from_stage3.py --step 24 --region northern_africa
    python plot_mslp_precip_from_stage3.py --step 48 --region europe
"""

import numpy as np
import xarray as xr
import tempfile
import os
import argparse
from pathlib import Path

# Import the extraction functions from read_stage3_to_xarray
import sys
sys.path.insert(0, str(Path(__file__).parent))

from read_stage3_to_xarray import (
    read_parquet_to_refs,
    extract_single_timestep,
    get_model_run_time,
    PREDEFINED_REGIONS,
)

# Configuration
DEFAULT_INPUT_DIR = Path("/scratch/notebook/test_ecmwf_three_stage_prebuilt_output")


def extract_field_at_step(zstore, variable, step_hour, member='control'):
    """
    Extract a single 2D field for a given step.

    Args:
        zstore: Zarr store dictionary
        variable: Variable name (msl, tp, etc.)
        step_hour: Forecast step in hours
        member: Ensemble member (default: control)

    Returns:
        2D numpy array (lat, lon)
    """
    print(f"  Extracting {variable} at step {step_hour}h...")
    array_2d = extract_single_timestep(zstore, variable, step_hour, member)

    if array_2d is not None:
        print(f"    Shape: {array_2d.shape}, Range: [{array_2d.min():.2f}, {array_2d.max():.2f}]")
    else:
        print(f"    Failed to extract!")

    return array_2d


def array_to_netcdf_visualiser(data_2d, variable, step_hour, model_run_time=None):
    """
    Convert numpy array to metview NetCDF visualiser for plotting.

    Args:
        data_2d: 2D numpy array (721 x 1440 for 0.25 deg)
        variable: Variable name for metadata
        step_hour: Forecast step
        model_run_time: Model initialization time

    Returns:
        Tuple of (metview NetCDF object, metview visualiser Request, temp file path)
    """
    import metview as mv

    # Create xarray DataArray with proper coordinates
    lats = np.linspace(90, -90, data_2d.shape[0])
    lons = np.linspace(0, 359.75, data_2d.shape[1])

    da = xr.DataArray(
        data_2d,
        coords={'latitude': lats, 'longitude': lons},
        dims=['latitude', 'longitude'],
    )

    # Write to temporary NetCDF (metview can read this)
    with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as tmp:
        tmp_path = tmp.name

    ds = xr.Dataset({variable: da})
    ds.to_netcdf(tmp_path)

    # Read into metview
    nc_data = mv.read(tmp_path)

    # Create visualiser for plotting NetCDF as geo_matrix
    visualiser = mv.netcdf_visualiser(
        netcdf_data=nc_data,
        netcdf_plot_type='geo_matrix',
        netcdf_latitude_variable='latitude',
        netcdf_longitude_variable='longitude',
        netcdf_value_variable=variable
    )

    return nc_data, visualiser, tmp_path


def create_mslp_precip_plot(msl_data, tp_data, model_run_time, step_hour,
                            region='northern_africa', output_name='mslp_precip',
                            jupyter_output=False):
    """
    Create MSLP + precipitation plot using metview.

    Args:
        msl_data: 2D numpy array of MSL pressure in Pa
        tp_data: 2D numpy array of total precipitation in m
        model_run_time: Model initialization datetime
        step_hour: Forecast step in hours
        region: Region name for view
        output_name: Output filename prefix
        jupyter_output: If True, also render to Jupyter (only works in notebooks)
    """
    import metview as mv
    import datetime

    print(f"\nCreating plot for {region}...")

    # Convert units:
    # - MSL: Pa to hPa (divide by 100)
    # - TP: m to mm (multiply by 1000)
    msl_hpa = msl_data / 100.0
    tp_mm = tp_data * 1000.0

    print(f"  MSL range: {msl_hpa.min():.1f} - {msl_hpa.max():.1f} hPa")
    print(f"  TP range: {tp_mm.min():.2f} - {tp_mm.max():.2f} mm")

    # Create metview visualisers from NetCDF
    print("  Converting to metview visualisers...")
    msl_nc, msl_vis, msl_tmp = array_to_netcdf_visualiser(msl_hpa, 'msl', step_hour, model_run_time)
    tp_nc, tp_vis, tp_tmp = array_to_netcdf_visualiser(tp_mm, 'tp', step_hour, model_run_time)

    # Define coastlines
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

    # Define view
    view = mv.geoview(
        area_mode="name",
        area_name=region,
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

    # Title
    if model_run_time:
        base_date = model_run_time.strftime('%a %d %B %Y %H')
        valid_time = model_run_time + datetime.timedelta(hours=step_hour)
        valid_date = valid_time.strftime('%a %d %B %Y %H')
    else:
        base_date = "Unknown"
        valid_date = "Unknown"

    title = mv.mtext(
        text_lines=[
            "Rain and mean sea level pressure (from Stage 3 Parquet)",
            f"START TIME: {base_date}",
            f"VALID TIME: {valid_date}, STEP: {step_hour}h"
        ],
        text_font_size=0.4,
        text_colour='charcoal'
    )

    ecmwf_text = mv.mtext(
        text_lines=[
            "Data source: ECMWF via grib-index-kerchunk Stage 3 Parquet",
            "Original: European Centre for Medium-Range Weather Forecasts (ECMWF)",
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
    print("  Generating plot...")

    # Try jupyter output if requested
    if jupyter_output:
        try:
            mv.setoutput('jupyter', plot_widget=False)
            mv.plot(view, tp_vis, tp_shade, msl_vis, msl_shade, title, ecmwf_text)
        except Exception as e:
            print(f"  Note: Jupyter output not available ({e})")

    # Save as PNG - use absolute path for output
    output_dir = os.getcwd()
    output_path = os.path.join(output_dir, output_name)

    png = mv.png_output(
        output_name=output_path,
        output_title=f"MSLP + Precip Step {step_hour}h",
        output_width=1000,
    )
    mv.setoutput(png)
    mv.plot(view, tp_vis, tp_shade, msl_vis, msl_shade, title, ecmwf_text)

    # Clean up temporary files
    try:
        os.unlink(msl_tmp)
        os.unlink(tp_tmp)
    except:
        pass

    # Check for output file (metview may add .1.png suffix)
    output_file = f"{output_path}.1.png"
    if os.path.exists(output_file):
        # Rename to expected name
        final_output = f"{output_path}.png"
        os.rename(output_file, final_output)
        print(f"  Saved: {final_output}")
    else:
        print(f"  Saved: {output_path}.png (or similar)")

    return True


def compute_12h_precip(zstore, step_start, step_end, member='control'):
    """
    Compute 12-hour accumulated precipitation (difference between steps).

    TP is cumulative from model start, so 12h precip = tp(step_end) - tp(step_start)

    Args:
        zstore: Zarr store dictionary
        step_start: Start step (e.g., 12)
        step_end: End step (e.g., 24)
        member: Ensemble member

    Returns:
        2D numpy array of 12h precipitation
    """
    print(f"\n  Computing {step_end-step_start}h precipitation ({step_start}h to {step_end}h)...")

    tp_start = extract_field_at_step(zstore, 'tp', step_start, member)
    tp_end = extract_field_at_step(zstore, 'tp', step_end, member)

    if tp_start is None or tp_end is None:
        raise ValueError(f"Failed to extract tp for steps {step_start} and {step_end}")

    tp_12h = tp_end - tp_start

    # Handle any negative values (shouldn't happen but be safe)
    tp_12h = np.maximum(tp_12h, 0)

    print(f"    12h precip range: {tp_12h.min():.6f} - {tp_12h.max():.6f} m")

    return tp_12h


def main():
    """Main function."""
    parser = argparse.ArgumentParser(
        description='Create MSLP + Precipitation plot from Stage 3 Parquet files',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
    # Plot for step 24 over Northern Africa
    python plot_mslp_precip_from_stage3.py --step 24 --region northern_africa

    # Plot for step 48 over Europe
    python plot_mslp_precip_from_stage3.py --step 48 --region europe

    # Custom output name
    python plot_mslp_precip_from_stage3.py --step 24 --output my_forecast
        '''
    )
    parser.add_argument('--step', type=int, default=24,
                        help='Forecast step in hours (default: 24)')
    parser.add_argument('--member', type=str, default='control',
                        help='Ensemble member (default: control)')
    parser.add_argument('--input-dir', type=Path, default=DEFAULT_INPUT_DIR,
                        help='Input directory with parquet files')
    parser.add_argument('--region', type=str, default='northern_africa',
                        help='Region name for metview (default: northern_africa)')
    parser.add_argument('--output', type=str, default='mslp_precip_stage3',
                        help='Output filename prefix (default: mslp_precip_stage3)')
    parser.add_argument('--precip-window', type=int, default=12,
                        help='Precipitation accumulation window in hours (default: 12)')

    args = parser.parse_args()

    print("="*80)
    print("MSLP + Precipitation Plot from Stage 3 Parquet")
    print("="*80)
    print(f"Member: {args.member}")
    print(f"Step: {args.step}h")
    print(f"Precip window: {args.precip_window}h")
    print(f"Region: {args.region}")
    print(f"Output: {args.output}.png")
    print("="*80)

    # Find parquet file
    parquet_file = args.input_dir / f"stage3_{args.member}_final.parquet"

    if not parquet_file.exists():
        print(f"Error: Parquet file not found: {parquet_file}")
        return False

    # Load parquet references
    print(f"\nLoading parquet: {parquet_file.name}")
    zstore = read_parquet_to_refs(parquet_file)

    # Get model run time
    model_run_time = get_model_run_time(zstore, 'tp')
    if model_run_time:
        print(f"Model run: {model_run_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")

    # Extract MSL at the target step
    msl_data = extract_field_at_step(zstore, 'msl', args.step, args.member)
    if msl_data is None:
        print("Error: Failed to extract MSL data")
        return False

    # Compute precipitation for the window ending at target step
    precip_start = max(0, args.step - args.precip_window)

    if precip_start == 0:
        # Just use cumulative precip
        tp_data = extract_field_at_step(zstore, 'tp', args.step, args.member)
    else:
        # Compute difference
        tp_data = compute_12h_precip(zstore, precip_start, args.step, args.member)

    if tp_data is None:
        print("Error: Failed to extract precipitation data")
        return False

    # Create the plot
    output_name = f"{args.output}_step{args.step:03d}"
    create_mslp_precip_plot(
        msl_data, tp_data,
        model_run_time, args.step,
        region=args.region,
        output_name=output_name
    )

    print("\n" + "="*80)
    print("Done!")
    print("="*80)

    return True


if __name__ == "__main__":
    success = main()
    if not success:
        exit(1)
