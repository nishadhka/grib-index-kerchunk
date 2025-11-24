#!/usr/bin/env python3
"""
ECMWF Stage 3 Parquet Reader - Fixed Version

This version properly handles the Stage 3 parquet structure by:
1. Reading from individual step_XXX arrays instead of aggregated arrays
2. Aggregating timesteps on-the-fly to get all 85 forecast hours
3. Using direct numpy extraction (no xarray dependencies)

Based on aifs-etl.py extraction method but adapted for Stage 3 structure.

Usage:
    python zarrv2_read_ecmwf_stage3_fixed.py --member control
    python zarrv2_read_ecmwf_stage3_fixed.py --member ens_01 --variable 2t
"""

import pandas as pd
import numpy as np
import json
import os
import argparse
from pathlib import Path
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
import cartopy.feature as cfeature
import warnings
import base64
import re

warnings.filterwarnings('ignore')

# Set up anonymous S3 access
os.environ['AWS_NO_SIGN_REQUEST'] = 'YES'

# Configuration
DEFAULT_INPUT_DIR = Path("/scratch/notebook/test_ecmwf_three_stage_prebuilt_output")
DEFAULT_VARIABLE = '2t'

# ECMWF forecast hours
FORECAST_HOURS = list(range(0, 145, 3)) + list(range(150, 361, 6))  # 85 steps total
N_TIMESTEPS = len(FORECAST_HOURS)

# Regional extraction
USE_REGIONAL_SUBSET = True
LAT_MIN, LAT_MAX = -12, 55
LON_MIN, LON_MAX = -25, 65


def read_parquet_to_zarr_store(parquet_file):
    """Read Stage 3 parquet file and convert to zarr store."""
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

    if 'version' in zstore:
        del zstore['version']

    print(f"✅ Loaded {len(zstore)} zarr references from {parquet_file.name}")
    return zstore


def decode_chunk_reference(chunk_ref):
    """Decode a chunk reference. Returns (type, data)."""
    if isinstance(chunk_ref, str):
        if chunk_ref.startswith('base64:'):
            base64_str = chunk_ref[7:]
            try:
                decoded = base64.b64decode(base64_str)
                return 'base64', decoded
            except:
                return 'unknown', chunk_ref
        else:
            return 'unknown', chunk_ref
    elif isinstance(chunk_ref, list):
        if len(chunk_ref) >= 3:
            url = chunk_ref[0]
            offset = chunk_ref[1]
            length = chunk_ref[2]
            if isinstance(url, str) and ('s3://' in url or 's3.amazonaws.com' in url):
                return 's3', (url, offset, length)
    return 'unknown', chunk_ref


def decode_grib2_data(data):
    """Decode GRIB2 data to numpy array."""
    if data[:4] != b'GRIB':
        return None

    try:
        import xarray as xr
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(delete=False, suffix='.grib2') as tmp:
            tmp.write(data)
            tmp_path = tmp.name

        ds = xr.open_dataset(tmp_path, engine='cfgrib')
        var_names = list(ds.data_vars)
        if var_names:
            var_data = ds[var_names[0]].values
            ds.close()
            os.unlink(tmp_path)
            return var_data
        ds.close()
        os.unlink(tmp_path)
    except:
        pass

    return None


def fetch_s3_chunk(url, offset, length):
    """Fetch S3 chunk using fsspec."""
    try:
        import fsspec

        if url.startswith('s3://'):
            s3_path = url[5:]
        else:
            s3_path = url

        fs = fsspec.filesystem('s3', anon=True)

        with fs.open(s3_path, 'rb') as f:
            f.seek(offset)
            data = f.read(length)

        return data
    except Exception as e:
        print(f"    ⚠️ Error fetching S3 chunk: {e}")
        return None


def extract_single_timestep(zstore, variable, step_hour, member='control'):
    """
    Extract data for a single timestep from step_XXX arrays.

    Args:
        zstore: Zarr store dictionary
        variable: Variable name (e.g., '2t', 'tp')
        step_hour: Forecast hour (0, 3, 6, ..., 360)
        member: Ensemble member name

    Returns:
        numpy array (2D) or None
    """
    # Find the step_XXX key for this forecast hour
    step_key = f"step_{step_hour:03d}"

    # Variable name mapping (ECMWF GRIB naming)
    # 2t appears as '2t' in step arrays
    # tp appears as 'tp' in step arrays

    # Search for the chunk key
    # Format: step_XXX/<var>/sfc/<member>/0.0.0
    chunk_pattern = f"{step_key}/{variable}/sfc/{member}/0.0.0"

    if chunk_pattern not in zstore:
        # Try alternative patterns
        alt_patterns = [
            f"{step_key}/{variable}/sf/{member}/0.0.0",
            f"{step_key}/{variable}/surface/{member}/0.0.0",
        ]
        for pattern in alt_patterns:
            if pattern in zstore:
                chunk_pattern = pattern
                break
        else:
            return None

    chunk_ref = zstore[chunk_pattern]
    ref_type, ref_data = decode_chunk_reference(chunk_ref)

    if ref_type == 'base64':
        # Base64 encoded data
        data = ref_data

        # Try decompression
        try:
            import numcodecs
            # Need to get compressor info - check metadata
            # For now, assume no compression on base64
            array = np.frombuffer(data, dtype='<f8')
            # ECMWF global: 721 x 1440
            if array.size == 721 * 1440:
                array = array.reshape((721, 1440))
                return array
        except:
            pass

    elif ref_type == 's3':
        # S3 reference - fetch and decode
        url, offset, length = ref_data

        print(f"    Fetching step {step_hour:03d} from S3...")
        chunk_data = fetch_s3_chunk(url, offset, length)

        if chunk_data is None:
            return None

        # Check if GRIB2
        if chunk_data[:4] == b'GRIB':
            array = decode_grib2_data(chunk_data)
            return array
        else:
            # Try direct numpy decode
            try:
                array = np.frombuffer(chunk_data, dtype='<f8')
                if array.size == 721 * 1440:
                    array = array.reshape((721, 1440))
                    return array
            except:
                pass

    return None


def load_ecmwf_data_step_by_step(parquet_file, variable='2t'):
    """
    Load ECMWF data by aggregating individual step_XXX arrays.

    This is the correct approach for Stage 3 parquet files that store
    individual timesteps separately.

    Args:
        parquet_file: Path to Stage 3 output parquet file
        variable: Variable to extract (default: '2t')

    Returns:
        tuple: (numpy_array, coordinates_dict)
    """
    member = parquet_file.stem.replace('stage3_', '').replace('_final', '')
    print(f"\n📊 Loading {member} using step-by-step aggregation...")
    print(f"   Variable: {variable}")
    print(f"   Expected timesteps: {N_TIMESTEPS}")

    try:
        # Read zarr store from parquet
        zstore = read_parquet_to_zarr_store(parquet_file)

        # Find available steps in the zstore
        available_steps = set()
        step_pattern = re.compile(r'step_(\d+)/')
        for key in zstore.keys():
            match = step_pattern.match(key)
            if match:
                step_hour = int(match.group(1))
                available_steps.add(step_hour)

        available_steps = sorted(available_steps)
        print(f"   Found {len(available_steps)} timesteps in parquet")
        print(f"   Range: {min(available_steps)} to {max(available_steps)} hours")

        # Initialize array storage
        timestep_arrays = []

        # Extract each timestep
        print(f"\n   Extracting timesteps:")
        for i, step_hour in enumerate(available_steps):
            if i % 10 == 0:
                print(f"      Processing step {i+1}/{len(available_steps)} (hour {step_hour})...")

            array_2d = extract_single_timestep(zstore, variable, step_hour, member)

            if array_2d is not None:
                timestep_arrays.append(array_2d)
            else:
                print(f"      ⚠️ Failed to extract step {step_hour}")

        if not timestep_arrays:
            print("   ❌ No timesteps extracted")
            return None, None

        # Stack into 3D array (timesteps, lat, lon)
        data_3d = np.stack(timestep_arrays, axis=0)

        print(f"\n   ✅ Successfully extracted {len(timestep_arrays)} timesteps")
        print(f"   Data shape: {data_3d.shape}")

        # Create coordinates
        # ECMWF global grid: 90N to -90S, 0E to 360E
        # Shape: (721, 1440)
        lats = np.linspace(90, -90, 721)
        lons = np.linspace(0, 360, 1440)

        # Apply regional subset if enabled
        if USE_REGIONAL_SUBSET:
            print(f"\n   Applying regional subset: lat[{LAT_MIN}:{LAT_MAX}], lon[{LON_MIN}:{LON_MAX}]")

            # Convert longitude range to 0-360 format
            lon_min_360 = LON_MIN % 360
            lon_max_360 = LON_MAX % 360

            # Find indices
            lat_idx = np.where((lats >= LAT_MIN) & (lats <= LAT_MAX))[0]
            lon_idx = np.where((lons >= lon_min_360) & (lons <= lon_max_360))[0]

            if len(lat_idx) == 0 or len(lon_idx) == 0:
                print("   ⚠️ Regional subset resulted in empty array, using full grid")
            else:
                # Subset data
                data_3d = data_3d[:, lat_idx[0]:lat_idx[-1]+1, lon_idx[0]:lon_idx[-1]+1]
                lats = lats[lat_idx[0]:lat_idx[-1]+1]
                lons = lons[lon_idx[0]:lon_idx[-1]+1]

                # Convert lons back to -180 to 180 for plotting
                lons = np.where(lons > 180, lons - 360, lons)

                print(f"   Subset shape: {data_3d.shape}")

        coords = {
            'latitude': lats,
            'longitude': lons,
            'forecast_hours': available_steps,
            'n_timesteps': len(timestep_arrays)
        }

        print(f"\n✅ {member} loaded successfully!")
        print(f"   Final shape: {data_3d.shape}")
        print(f"   Timesteps: {coords['n_timesteps']}")
        print(f"   Memory: ~{data_3d.nbytes / 1024 / 1024:.1f} MB")

        return data_3d, coords

    except Exception as e:
        print(f"❌ Error loading {member}: {e}")
        import traceback
        traceback.print_exc()
        return None, None


def plot_timesteps_panel(data, coords, member, variable='2t', output_dir=None):
    """Create multi-panel plot showing multiple timesteps."""
    print(f"\n🎨 Creating timestep panel plot for {member}...")

    n_timesteps = data.shape[0]
    print(f"   Total timesteps: {n_timesteps}")

    # Select subset to plot
    plot_steps = list(range(0, n_timesteps, max(1, n_timesteps // 12)))
    if plot_steps[-1] != n_timesteps - 1:
        plot_steps.append(n_timesteps - 1)

    n_plots = len(plot_steps)
    print(f"   Plotting {n_plots} timesteps")

    # Create grid
    n_cols = 4
    n_rows = (n_plots + n_cols - 1) // n_cols

    fig, axes = plt.subplots(n_rows, n_cols, figsize=(5*n_cols, 4*n_rows),
                            subplot_kw={'projection': ccrs.PlateCarree()})

    if n_rows == 1:
        axes = axes.reshape(1, -1)

    axes_flat = axes.flatten()

    lats = coords['latitude']
    lons = coords['longitude']
    forecast_hours = coords['forecast_hours']

    # Variable settings
    if variable in ['2t', 't2m']:
        var_label = '2m Temperature'
        var_units = 'K'
        cmap = 'RdYlBu_r'
    elif variable == 'tp':
        var_label = 'Total Precipitation'
        var_units = 'mm'
        cmap = 'Blues'
    else:
        var_label = variable
        var_units = 'units'
        cmap = 'viridis'

    # Plot each timestep
    for idx, step in enumerate(plot_steps):
        ax = axes_flat[idx]

        data_slice = data[step]
        hour = forecast_hours[step] if step < len(forecast_hours) else step

        if variable == 'tp':
            vmin, vmax = 0, np.nanpercentile(data_slice, 98)
            levels = np.linspace(vmin, vmax, 11)
        else:
            vmin, vmax = np.nanpercentile(data_slice, [2, 98])
            levels = np.linspace(vmin, vmax, 11)

        cf = ax.contourf(lons, lats, data_slice,
                        levels=levels, cmap=cmap,
                        transform=ccrs.PlateCarree(),
                        extend='both')

        ax.add_feature(cfeature.COASTLINE, linewidth=0.5)
        ax.add_feature(cfeature.BORDERS, linewidth=0.3, alpha=0.5)
        ax.add_feature(cfeature.LAND, alpha=0.1)

        if USE_REGIONAL_SUBSET:
            ax.set_extent([LON_MIN, LON_MAX, LAT_MIN, LAT_MAX])

        ax.set_title(f'T+{hour:03d}h (step {step})', fontsize=10)

    # Hide unused subplots
    for idx in range(n_plots, len(axes_flat)):
        axes_flat[idx].set_visible(False)

    # Colorbar
    cbar_ax = fig.add_axes([0.92, 0.15, 0.02, 0.7])
    cbar = plt.colorbar(cf, cax=cbar_ax)
    cbar.set_label(f'{var_label} ({var_units})', rotation=270, labelpad=20)

    # Title
    plt.suptitle(f'ECMWF {var_label} - {member.upper()}\n'
                f'All {n_timesteps} Forecast Hours\n'
                f'Direct Step-by-Step Extraction',
                fontsize=16, y=0.98)

    # Save
    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(exist_ok=True, parents=True)
        output_file = output_dir / f'ecmwf_{member}_{variable}_timesteps.png'
    else:
        output_file = f'ecmwf_{member}_{variable}_timesteps.png'

    plt.tight_layout(rect=[0, 0, 0.9, 0.96])
    plt.savefig(str(output_file), dpi=150, bbox_inches='tight')
    print(f"✅ Panel plot saved: {output_file}")
    plt.close()

    return str(output_file)


def plot_timeseries_summary(data, coords, member, variable='2t', output_dir=None):
    """Create timeseries plot showing min/mean/max over time."""
    print(f"\n📈 Creating timeseries summary for {member}...")

    n_timesteps = data.shape[0]

    # Calculate statistics
    mean_vals = np.nanmean(data, axis=(1, 2))
    min_vals = np.nanmin(data, axis=(1, 2))
    max_vals = np.nanmax(data, axis=(1, 2))

    forecast_hours = coords['forecast_hours']

    # Create plot
    fig, ax = plt.subplots(figsize=(12, 6))

    ax.plot(forecast_hours, mean_vals, 'b-', linewidth=2, label='Mean')
    ax.fill_between(forecast_hours, min_vals, max_vals, alpha=0.3, color='blue', label='Min-Max Range')

    # Variable labels
    if variable in ['2t', 't2m']:
        var_label = '2m Temperature'
        var_units = 'K'
    elif variable == 'tp':
        var_label = 'Total Precipitation'
        var_units = 'mm'
    else:
        var_label = variable
        var_units = 'units'

    ax.set_xlabel('Forecast Hour', fontsize=12)
    ax.set_ylabel(f'{var_label} ({var_units})', fontsize=12)
    ax.set_title(f'ECMWF {var_label} - {member.upper()}\n'
                f'Spatial Statistics over All {n_timesteps} Forecast Hours',
                fontsize=14)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)

    # Save
    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(exist_ok=True, parents=True)
        output_file = output_dir / f'ecmwf_{member}_{variable}_timeseries.png'
    else:
        output_file = f'ecmwf_{member}_{variable}_timeseries.png'

    plt.tight_layout()
    plt.savefig(str(output_file), dpi=150, bbox_inches='tight')
    print(f"✅ Timeseries plot saved: {output_file}")
    plt.close()

    return str(output_file)


def main():
    """Main function."""
    parser = argparse.ArgumentParser(description='Read ECMWF Stage 3 output (Fixed Version)')
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
    print("ECMWF Stage 3 Parquet Reader - FIXED VERSION")
    print("="*80)
    print(f"Member: {args.member}")
    print(f"Variable: {args.variable}")
    print(f"Input: {args.input_dir}")
    print(f"Method: Direct step-by-step extraction")
    print("="*80)

    # Find parquet file
    parquet_file = args.input_dir / f"stage3_{args.member}_final.parquet"

    if not parquet_file.exists():
        print(f"❌ Error: Parquet file not found: {parquet_file}")
        print(f"\nAvailable files in {args.input_dir}:")
        for f in sorted(args.input_dir.glob("stage3_*_final.parquet")):
            print(f"   - {f.name}")
        return False

    # Load data
    data, coords = load_ecmwf_data_step_by_step(parquet_file, variable=args.variable)

    if data is None:
        print("❌ Failed to load data")
        return False

    # Create plots
    print(f"\n{'='*80}")
    print("Creating Plots")
    print(f"{'='*80}")

    panel_plot = plot_timesteps_panel(data, coords, args.member,
                                     variable=args.variable,
                                     output_dir=args.output)

    timeseries_plot = plot_timeseries_summary(data, coords, args.member,
                                             variable=args.variable,
                                             output_dir=args.output)

    print(f"\n{'='*80}")
    print("✅ Processing Complete!")
    print(f"{'='*80}")
    print(f"Member: {args.member}")
    print(f"Variable: {args.variable}")
    print(f"Timesteps: {coords['n_timesteps']}")
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
