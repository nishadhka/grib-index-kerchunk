#!/usr/bin/env python3
"""
ECMWF Stage 3 Parquet Reader - Zarr V2 Method
Reads Stage 3 output from test_three_stage_ecmwf_prebuilt.py using xarray + zarr

This script demonstrates the traditional Zarr V2 approach:
- Uses zarr library (via fsspec reference filesystem)
- Uses xarray with zarr engine
- Simple high-level interface
- Good for prototyping and simple analysis

Usage:
    python zarrv2_read_ecmwf_stage3.py --member control
    python zarrv2_read_ecmwf_stage3.py --member ens_01 --output plots/
"""

import pandas as pd
import numpy as np
import xarray as xr
import json
import os
import argparse
from pathlib import Path
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
import cartopy.feature as cfeature
from datetime import datetime, timedelta
import fsspec
import warnings

warnings.filterwarnings('ignore')

# Set up anonymous S3 access
os.environ['AWS_NO_SIGN_REQUEST'] = 'YES'

# Configuration
DEFAULT_INPUT_DIR = Path("/scratch/notebook/test_ecmwf_three_stage_prebuilt_output")
DEFAULT_VARIABLE = '2t'  # 2-metre temperature

# ECMWF has 85 forecast hours
N_TIMESTEPS = 85

# Regional extraction (global or subset)
USE_REGIONAL_SUBSET = True
LAT_MIN, LAT_MAX = -12, 55  # Europe + Africa
LON_MIN, LON_MAX = -25, 65


def read_parquet_to_zarr_store(parquet_file):
    """
    Read Stage 3 parquet file and convert to zarr store.
    This is the same function used in test_three_stage_ecmwf_prebuilt.py
    """
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

    print(f"✅ Loaded {len(zstore)} zarr references from {parquet_file.name}")
    return zstore


def load_ecmwf_data_zarrv2(parquet_file, variable='2t'):
    """
    Load ECMWF data using Zarr V2 method (xarray + zarr library).

    This method:
    - Uses fsspec reference filesystem (requires zarr support)
    - Opens with xarray.open_datatree() using zarr engine
    - High-level, simple interface
    - May load full global arrays (memory intensive)

    Args:
        parquet_file: Path to Stage 3 output parquet file
        variable: Variable to extract (default: '2t')

    Returns:
        tuple: (numpy_array, coordinates_dict)
    """
    member = parquet_file.stem.replace('stage3_', '').replace('_final', '')
    print(f"\n📊 Loading {member} using Zarr V2 method...")

    try:
        # Read zarr store from parquet
        zstore = read_parquet_to_zarr_store(parquet_file)

        # ⭐ ZARR V2: Create reference filesystem (uses zarr library)
        print(f"   Creating fsspec reference filesystem (uses zarr)...")
        fs = fsspec.filesystem(
            "reference",
            fo={'refs': zstore, 'version': 1},
            remote_protocol='s3',
            remote_options={'anon': True}
        )
        mapper = fs.get_mapper("")

        # ⭐ ZARR V2: Open with xarray using zarr engine
        print(f"   Opening with xarray.open_datatree(engine='zarr')...")
        dt = xr.open_datatree(mapper, engine="zarr", consolidated=False)

        # Display structure
        print(f"   Datatree variables: {list(dt.keys())[:5]}...")

        # Navigate to variable data
        # ECMWF structure: /2t/instant/surface or /tp/accum/surface
        if variable == '2t':
            var_path = '/2t/instant/surface'
        elif variable == 'tp':
            var_path = '/tp/accum/surface'
        elif variable == '10u':
            var_path = '/10u/instant/surface'
        elif variable == '10v':
            var_path = '/10v/instant/surface'
        else:
            # Try to find variable
            var_paths = [k for k in dt.keys() if variable in k]
            if var_paths:
                var_path = var_paths[0]
            else:
                raise ValueError(f"Variable {variable} not found. Available: {list(dt.keys())[:10]}")

        print(f"   Extracting variable from {var_path}...")
        data_var = dt[var_path].ds[variable]

        print(f"   Variable shape: {data_var.shape}")
        print(f"   Dimensions: {data_var.dims}")

        # Extract coordinates
        if 'latitude' in data_var.coords:
            lats = data_var.latitude.values
            lons = data_var.longitude.values
        elif 'lat' in data_var.coords:
            lats = data_var.lat.values
            lons = data_var.lon.values
        else:
            print(f"   Warning: No latitude/longitude coordinates found")
            lats = np.arange(data_var.shape[1]) if len(data_var.shape) > 1 else None
            lons = np.arange(data_var.shape[2]) if len(data_var.shape) > 2 else None

        # Get time coordinate
        if 'valid_time' in data_var.coords:
            times = data_var.valid_time.values
        elif 'time' in data_var.coords:
            times = data_var.time.values
        else:
            times = np.arange(data_var.shape[0])

        # Regional subset (if enabled)
        if USE_REGIONAL_SUBSET and lats is not None and lons is not None:
            print(f"   Selecting regional subset: lat[{LAT_MIN}:{LAT_MAX}], lon[{LON_MIN}:{LON_MAX}]...")
            regional_data = data_var.sel(
                latitude=slice(LAT_MAX, LAT_MIN),
                longitude=slice(LON_MIN, LON_MAX)
            )
        else:
            print(f"   Loading full global array...")
            regional_data = data_var

        # ⭐ ZARR V2: Compute to numpy array
        # This triggers xarray to read all chunks via zarr
        print(f"   Computing data to numpy array (may take time for global data)...")
        numpy_data = regional_data.compute()

        # Get final coordinates
        final_lats = regional_data.latitude.values if 'latitude' in regional_data.coords else lats
        final_lons = regional_data.longitude.values if 'longitude' in regional_data.coords else lons

        coords = {
            'latitude': final_lats,
            'longitude': final_lons,
            'time': times,
            'n_timesteps': numpy_data.shape[0]
        }

        print(f"✅ {member} loaded successfully!")
        print(f"   Data shape: {numpy_data.shape}")
        print(f"   Timesteps: {coords['n_timesteps']}")
        print(f"   Memory: ~{numpy_data.nbytes / 1024 / 1024:.1f} MB")

        return numpy_data.values, coords

    except Exception as e:
        print(f"❌ Error loading {member}: {e}")
        import traceback
        traceback.print_exc()
        return None, None


def plot_timesteps_panel(data, coords, member, variable='2t', output_dir=None):
    """
    Create multi-panel plot showing multiple timesteps for single member.
    Plots all 85 timesteps in a grid layout.

    Args:
        data: numpy array (timesteps, lat, lon)
        coords: dictionary with coordinates
        member: member name
        variable: variable name
        output_dir: output directory for plot
    """
    print(f"\n🎨 Creating timestep panel plot for {member}...")

    n_timesteps = data.shape[0]
    print(f"   Total timesteps: {n_timesteps}")

    # Select subset of timesteps to plot (every 8th step = ~24h intervals for ECMWF)
    # ECMWF: 0-144h at 3h (49 steps), 150-360h at 6h (36 steps) = 85 total
    # Show every 8th step ≈ every 24h
    plot_steps = list(range(0, n_timesteps, 8))
    if plot_steps[-1] != n_timesteps - 1:
        plot_steps.append(n_timesteps - 1)

    n_plots = len(plot_steps)
    print(f"   Plotting {n_plots} timesteps: {plot_steps}")

    # Create grid layout (4 columns)
    n_cols = 4
    n_rows = (n_plots + n_cols - 1) // n_cols

    fig, axes = plt.subplots(n_rows, n_cols, figsize=(5*n_cols, 4*n_rows),
                            subplot_kw={'projection': ccrs.PlateCarree()})

    if n_rows == 1:
        axes = axes.reshape(1, -1)

    axes_flat = axes.flatten()

    # Get coordinates
    lats = coords['latitude']
    lons = coords['longitude']

    # Determine variable-specific settings
    if variable == '2t':
        var_label = '2m Temperature'
        var_units = 'K'
        cmap = 'RdYlBu_r'
    elif variable == 'tp':
        var_label = 'Total Precipitation'
        var_units = 'mm'
        cmap = 'Blues'
    elif variable in ['10u', '10v']:
        var_label = f'10m {variable.upper()} Wind'
        var_units = 'm/s'
        cmap = 'RdBu_r'
    else:
        var_label = variable
        var_units = 'units'
        cmap = 'viridis'

    # Plot each timestep
    for idx, step in enumerate(plot_steps):
        ax = axes_flat[idx]

        # Get data for timestep
        data_slice = data[step]

        # Calculate timestep hour (ECMWF specific)
        if step < 49:
            hour = step * 3
        else:
            hour = 144 + (step - 48) * 6

        # Create plot
        if variable == 'tp':
            # For precipitation, use levels starting from 0
            vmin, vmax = 0, np.nanpercentile(data_slice, 98)
            levels = np.linspace(vmin, vmax, 11)
        else:
            vmin, vmax = np.nanpercentile(data_slice, [2, 98])
            levels = np.linspace(vmin, vmax, 11)

        cf = ax.contourf(lons, lats, data_slice,
                        levels=levels, cmap=cmap,
                        transform=ccrs.PlateCarree(),
                        extend='both')

        # Add map features
        ax.add_feature(cfeature.COASTLINE, linewidth=0.5)
        ax.add_feature(cfeature.BORDERS, linewidth=0.3, alpha=0.5)
        ax.add_feature(cfeature.LAND, alpha=0.1)

        # Set extent
        if USE_REGIONAL_SUBSET:
            ax.set_extent([LON_MIN, LON_MAX, LAT_MIN, LAT_MAX])

        # Add gridlines
        if idx % n_cols == 0:  # First column only
            gl = ax.gridlines(draw_labels=True, linewidth=0.3, alpha=0.3)
            gl.top_labels = False
            gl.right_labels = False
            gl.xlabel_style = {'size': 8}
            gl.ylabel_style = {'size': 8}

        # Title
        ax.set_title(f'T+{hour:03d}h\nStep {step}/{n_timesteps-1}',
                    fontsize=10)

    # Hide unused subplots
    for idx in range(n_plots, len(axes_flat)):
        axes_flat[idx].set_visible(False)

    # Add colorbar
    cbar_ax = fig.add_axes([0.92, 0.15, 0.02, 0.7])
    cbar = plt.colorbar(cf, cax=cbar_ax)
    cbar.set_label(f'{var_label} ({var_units})', rotation=270, labelpad=20)

    # Overall title
    plt.suptitle(f'ECMWF {var_label} - {member.upper()}\n'
                f'All {n_timesteps} Forecast Hours (showing every ~24h)\n'
                f'Zarr V2 Method (xarray + zarr library)',
                fontsize=16, y=0.98)

    # Save figure
    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(exist_ok=True, parents=True)
        output_file = output_dir / f'zarrv2_{member}_{variable}_all_timesteps.png'
    else:
        output_file = f'zarrv2_ecmwf_{member}_{variable}_all_timesteps.png'

    plt.tight_layout(rect=[0, 0, 0.9, 0.96])
    plt.savefig(str(output_file), dpi=150, bbox_inches='tight')
    print(f"✅ Panel plot saved: {output_file}")
    plt.close()

    return str(output_file)


def plot_timeseries_summary(data, coords, member, variable='2t', output_dir=None):
    """
    Create timeseries plot showing min/mean/max over time.

    Args:
        data: numpy array (timesteps, lat, lon)
        coords: dictionary with coordinates
        member: member name
        variable: variable name
        output_dir: output directory for plot
    """
    print(f"\n📈 Creating timeseries summary for {member}...")

    n_timesteps = data.shape[0]

    # Calculate statistics over space
    mean_vals = np.nanmean(data, axis=(1, 2))
    min_vals = np.nanmin(data, axis=(1, 2))
    max_vals = np.nanmax(data, axis=(1, 2))

    # Calculate forecast hours
    hours = []
    for step in range(n_timesteps):
        if step < 49:
            hour = step * 3
        else:
            hour = 144 + (step - 48) * 6
        hours.append(hour)

    # Create plot
    fig, ax = plt.subplots(figsize=(12, 6))

    # Plot mean with shaded min/max
    ax.plot(hours, mean_vals, 'b-', linewidth=2, label='Mean')
    ax.fill_between(hours, min_vals, max_vals, alpha=0.3, color='blue', label='Min-Max Range')

    # Variable-specific labels
    if variable == '2t':
        var_label = '2m Temperature'
        var_units = 'K'
    elif variable == 'tp':
        var_label = 'Total Precipitation'
        var_units = 'mm'
    elif variable in ['10u', '10v']:
        var_label = f'10m {variable.upper()} Wind'
        var_units = 'm/s'
    else:
        var_label = variable
        var_units = 'units'

    ax.set_xlabel('Forecast Hour', fontsize=12)
    ax.set_ylabel(f'{var_label} ({var_units})', fontsize=12)
    ax.set_title(f'ECMWF {var_label} - {member.upper()}\n'
                f'Spatial Statistics over All {n_timesteps} Forecast Hours\n'
                f'Zarr V2 Method (xarray + zarr library)',
                fontsize=14)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)

    # Save figure
    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(exist_ok=True, parents=True)
        output_file = output_dir / f'zarrv2_{member}_{variable}_timeseries.png'
    else:
        output_file = f'zarrv2_ecmwf_{member}_{variable}_timeseries.png'

    plt.tight_layout()
    plt.savefig(str(output_file), dpi=150, bbox_inches='tight')
    print(f"✅ Timeseries plot saved: {output_file}")
    plt.close()

    return str(output_file)


def main():
    """Main function."""
    parser = argparse.ArgumentParser(description='Read ECMWF Stage 3 output using Zarr V2 method')
    parser.add_argument('--member', type=str, default='control',
                       help='Ensemble member to process (default: control)')
    parser.add_argument('--variable', type=str, default='2t',
                       help='Variable to extract (default: 2t)')
    parser.add_argument('--input-dir', type=Path, default=DEFAULT_INPUT_DIR,
                       help='Input directory with Stage 3 parquet files')
    parser.add_argument('--output', type=Path, default=None,
                       help='Output directory for plots')
    args = parser.parse_args()

    print("="*80)
    print("ECMWF Stage 3 Parquet Reader - Zarr V2 Method")
    print("="*80)
    print(f"Member: {args.member}")
    print(f"Variable: {args.variable}")
    print(f"Input: {args.input_dir}")
    print(f"Method: Zarr V2 (xarray + zarr library)")
    print("="*80)

    # Find parquet file
    parquet_file = args.input_dir / f"stage3_{args.member}_final.parquet"

    if not parquet_file.exists():
        print(f"❌ Error: Parquet file not found: {parquet_file}")
        print(f"\nAvailable files in {args.input_dir}:")
        for f in sorted(args.input_dir.glob("stage3_*_final.parquet")):
            print(f"   - {f.name}")
        return False

    # Load data using Zarr V2 method
    data, coords = load_ecmwf_data_zarrv2(parquet_file, variable=args.variable)

    if data is None:
        print("❌ Failed to load data")
        return False

    # Verify we have all 85 timesteps
    n_timesteps = data.shape[0]
    if n_timesteps != N_TIMESTEPS:
        print(f"⚠️ Warning: Expected {N_TIMESTEPS} timesteps, got {n_timesteps}")
    else:
        print(f"\n✅ Verified: All {N_TIMESTEPS} timesteps present")

    # Create plots
    print(f"\n{'='*80}")
    print("Creating Plots")
    print(f"{'='*80}")

    # Panel plot (all timesteps)
    panel_plot = plot_timesteps_panel(data, coords, args.member,
                                     variable=args.variable,
                                     output_dir=args.output)

    # Timeseries summary
    timeseries_plot = plot_timeseries_summary(data, coords, args.member,
                                             variable=args.variable,
                                             output_dir=args.output)

    print(f"\n{'='*80}")
    print("✅ Processing Complete!")
    print(f"{'='*80}")
    print(f"Member: {args.member}")
    print(f"Variable: {args.variable}")
    print(f"Timesteps: {n_timesteps}")
    print(f"Data shape: {data.shape}")
    print(f"Plots created:")
    print(f"   - {panel_plot}")
    print(f"   - {timeseries_plot}")
    print(f"{'='*80}")

    return True


if __name__ == "__main__":
    success = main()
    if not success:
        exit(1)
