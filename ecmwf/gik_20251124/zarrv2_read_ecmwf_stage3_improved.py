#!/usr/bin/env python3
"""
ECMWF Stage 3 Parquet Reader - Zarr V2 Method (IMPROVED)

Improved version combining:
- Direct numpy extraction from aifs-etl.py (avoids xarray datatree issues)
- Visualization capabilities from original zarrv2_read_ecmwf_stage3.py
- Proper variable path mapping and group opening

This script demonstrates the traditional Zarr V2 approach:
- Uses zarr library (via fsspec reference filesystem)
- Can use xarray for specific groups OR direct numpy extraction
- Robust handling of missing _ARRAY_DIMENSIONS metadata
- Good for prototyping and analysis

Usage:
    python zarrv2_read_ecmwf_stage3_improved.py --member control
    python zarrv2_read_ecmwf_stage3_improved.py --member ens_01 --output plots/
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
import base64

warnings.filterwarnings('ignore')

# Set up anonymous S3 access
os.environ['AWS_NO_SIGN_REQUEST'] = 'YES'

# Configuration
DEFAULT_INPUT_DIR = Path("/scratch/notebook/test_ecmwf_three_stage_prebuilt_output")
DEFAULT_VARIABLE = 't2m'  # Use t2m instead of 2t

# ECMWF has 85 forecast hours
N_TIMESTEPS = 85

# Regional extraction (global or subset)
USE_REGIONAL_SUBSET = True
LAT_MIN, LAT_MAX = -12, 55  # Europe + Africa
LON_MIN, LON_MAX = -25, 65

# Extraction method: 'xarray' or 'numpy'
EXTRACTION_METHOD = 'xarray'  # Change to 'numpy' for pure numpy extraction


def get_variable_path_mapping():
    """
    Map parameter names to their paths in the zarr store.
    Based on aifs-etl.py mapping.
    """
    return {
        # Surface parameters
        '10u': 'u10/instant/heightAboveGround/u10',
        '10v': 'v10/instant/heightAboveGround/v10',
        't2m': 't2m/instant/heightAboveGround/t2m',
        '2t': 't2m/instant/heightAboveGround/t2m',  # Alias
        '2d': 'd2m/instant/heightAboveGround/d2m',
        'msl': 'msl/instant/meanSea/msl',
        'sp': 'sp/instant/surface/sp',
        'skt': 'skt/instant/surface/skt',
        'tcw': 'tcw/instant/entireAtmosphere/tcw',
        'tp': 'tp/accum/surface/tp',
        # Fixed fields
        'lsm': 'lsm/instant/surface/lsm',
        # Pressure level parameters
        'gh': 'gh/instant/isobaricInhPa/gh',
        't': 't/instant/isobaricInhPa/t',
        'u': 'u/instant/isobaricInhPa/u',
        'v': 'v/instant/isobaricInhPa/v',
        'w': 'w/instant/isobaricInhPa/w',
        'q': 'q/instant/isobaricInhPa/q',
    }


def read_parquet_to_zarr_store(parquet_file):
    """
    Read Stage 3 parquet file and convert to zarr store.
    Based on aifs-etl.py implementation.
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


def decode_chunk_reference(chunk_ref):
    """Decode a chunk reference. Returns (type, data)."""
    if isinstance(chunk_ref, str):
        if chunk_ref.startswith('base64:'):
            base64_str = chunk_ref[7:]
            try:
                decoded = base64.b64decode(base64_str)
                return 'base64', decoded
            except Exception as e:
                print(f"    ⚠️ Error decoding base64: {e}")
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


def extract_variable_numpy(zstore, variable_path):
    """
    Extract a variable using direct numpy reconstruction.
    Based on aifs-etl.py extract_variable_hybrid() but simplified for base64 data.
    """
    # Get metadata
    zarray_key = f"{variable_path}/.zarray"
    if zarray_key not in zstore:
        print(f"    ⚠️ No metadata found for {variable_path}")
        return None

    metadata = json.loads(zstore[zarray_key]) if isinstance(zstore[zarray_key], str) else zstore[zarray_key]

    shape = tuple(metadata['shape'])
    dtype = np.dtype(metadata['dtype'])
    chunks = tuple(metadata['chunks'])
    compressor = metadata.get('compressor', None)

    print(f"   Variable metadata: shape={shape}, dtype={dtype}, chunks={chunks}")

    # Collect chunks
    chunks_data = {}

    for key in sorted(zstore.keys()):
        if key.startswith(variable_path + "/") and not key.endswith(('.zarray', '.zattrs', '.zgroup')):
            chunk_ref = zstore[key]
            ref_type, ref_data = decode_chunk_reference(chunk_ref)

            if ref_type == 'base64':
                data = ref_data
                if compressor is not None:
                    try:
                        import numcodecs
                        codec = numcodecs.get_codec(compressor)
                        data = codec.decode(data)
                    except:
                        try:
                            import blosc
                            data = blosc.decompress(data)
                        except:
                            pass
                chunks_data[key] = data

            elif ref_type == 's3':
                print(f"    ⚠️ S3 reference found - not implemented in simplified version")
                # Could implement S3 fetching here if needed
                continue

    if not chunks_data:
        print(f"    ⚠️ No chunks found for {variable_path}")
        return None

    # Reconstruct array
    try:
        if len(chunks_data) == 1:
            chunk_data = list(chunks_data.values())[0]

            if isinstance(chunk_data, np.ndarray):
                array = chunk_data
            else:
                array = np.frombuffer(chunk_data, dtype=dtype)

            if array.size == np.prod(shape):
                array = array.reshape(shape)

            return array

        else:
            # Multiple chunks - reassemble
            first_chunk = list(chunks_data.values())[0]
            if isinstance(first_chunk, np.ndarray):
                actual_dtype = first_chunk.dtype
            else:
                actual_dtype = dtype

            array = np.zeros(shape, dtype=actual_dtype)

            for chunk_key, chunk_data in chunks_data.items():
                chunk_idx_str = chunk_key.replace(variable_path + "/", "")
                chunk_indices = tuple(int(x) for x in chunk_idx_str.split('.'))

                if isinstance(chunk_data, np.ndarray):
                    chunk_array = chunk_data
                else:
                    chunk_array = np.frombuffer(chunk_data, dtype=actual_dtype)

                # Standard zarr chunk reassembly
                chunk_shape = []
                for i, (idx, chunk_size, dim_size) in enumerate(zip(chunk_indices, chunks, shape)):
                    if (idx + 1) * chunk_size <= dim_size:
                        chunk_shape.append(chunk_size)
                    else:
                        chunk_shape.append(dim_size - idx * chunk_size)

                if chunk_array.size == np.prod(chunk_shape):
                    chunk_array = chunk_array.reshape(tuple(chunk_shape))

                slices = []
                for idx, chunk_size, dim_size in zip(chunk_indices, chunks, shape):
                    start = idx * chunk_size
                    end = min(start + chunk_size, dim_size)
                    slices.append(slice(start, end))

                array[tuple(slices)] = chunk_array

            return array

    except Exception as e:
        print(f"    ❌ Error reconstructing array: {e}")
        import traceback
        traceback.print_exc()
        return None


def load_ecmwf_data_xarray(parquet_file, variable='t2m'):
    """
    Load ECMWF data using Zarr V2 method with xarray on specific group.

    This method:
    - Opens a specific zarr group instead of full datatree
    - Avoids the _ARRAY_DIMENSIONS error by targeting correct path
    - Uses proper variable name mapping

    Args:
        parquet_file: Path to Stage 3 output parquet file
        variable: Variable to extract (default: 't2m')

    Returns:
        tuple: (numpy_array, coordinates_dict)
    """
    member = parquet_file.stem.replace('stage3_', '').replace('_final', '')
    print(f"\n📊 Loading {member} using Zarr V2 + xarray method...")

    try:
        # Read zarr store from parquet
        zstore = read_parquet_to_zarr_store(parquet_file)

        # Get variable path mapping
        var_paths = get_variable_path_mapping()

        if variable not in var_paths:
            print(f"   ⚠️ Variable {variable} not in mapping. Available: {list(var_paths.keys())[:10]}")
            return None, None

        full_var_path = var_paths[variable]

        # Split path to get group and variable
        # e.g., 't2m/instant/heightAboveGround/t2m' -> group='t2m/instant/heightAboveGround', var='t2m'
        path_parts = full_var_path.split('/')
        group_path = '/'.join(path_parts[:-1])
        var_name = path_parts[-1]

        print(f"   Variable path: {full_var_path}")
        print(f"   Opening group: {group_path}")

        # Create reference filesystem
        print(f"   Creating fsspec reference filesystem...")
        fs = fsspec.filesystem(
            "reference",
            fo={'refs': zstore, 'version': 1},
            remote_protocol='s3',
            remote_options={'anon': True}
        )
        mapper = fs.get_mapper("")

        # Open specific group with xarray (avoids _ARRAY_DIMENSIONS error)
        print(f"   Opening with xarray.open_dataset(group='{group_path}')...")
        ds = xr.open_dataset(mapper, engine="zarr", group=group_path, consolidated=False)

        print(f"   Dataset variables: {list(ds.data_vars)}")
        print(f"   Dataset coordinates: {list(ds.coords)}")

        # Extract variable
        if var_name not in ds.data_vars:
            print(f"   ⚠️ Variable {var_name} not found in dataset. Available: {list(ds.data_vars)}")
            return None, None

        data_var = ds[var_name]
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
            lats = np.arange(data_var.shape[-2]) if len(data_var.shape) >= 2 else None
            lons = np.arange(data_var.shape[-1]) if len(data_var.shape) >= 1 else None

        # Get time coordinate
        if 'valid_time' in data_var.coords:
            times = data_var.valid_time.values
        elif 'time' in data_var.coords:
            times = data_var.time.values
        elif 'step' in data_var.coords:
            times = data_var.step.values
        else:
            times = np.arange(data_var.shape[0]) if len(data_var.shape) > 0 else None

        # Regional subset (if enabled)
        if USE_REGIONAL_SUBSET and lats is not None and lons is not None:
            print(f"   Selecting regional subset: lat[{LAT_MIN}:{LAT_MAX}], lon[{LON_MIN}:{LON_MAX}]...")

            # Handle both latitude and lat coordinate names
            lat_coord = 'latitude' if 'latitude' in data_var.coords else 'lat'
            lon_coord = 'longitude' if 'longitude' in data_var.coords else 'lon'

            regional_data = data_var.sel(
                {lat_coord: slice(LAT_MAX, LAT_MIN),
                 lon_coord: slice(LON_MIN, LON_MAX)}
            )
        else:
            print(f"   Loading full global array...")
            regional_data = data_var

        # Compute to numpy array
        print(f"   Computing data to numpy array...")
        numpy_data = regional_data.compute()

        # Get final coordinates
        lat_coord = 'latitude' if 'latitude' in regional_data.coords else 'lat'
        lon_coord = 'longitude' if 'longitude' in regional_data.coords else 'lon'
        final_lats = regional_data[lat_coord].values
        final_lons = regional_data[lon_coord].values

        # Handle different dimension structures
        # Expected: (time, step, lat, lon) or similar
        if numpy_data.ndim >= 2:
            # Squeeze out size-1 dimensions except lat/lon
            # Keep the step dimension if it exists and has size > 1
            if numpy_data.ndim == 4:
                # (time, step, lat, lon)
                if numpy_data.shape[0] == 1:
                    numpy_data = numpy_data[0]  # Remove time dimension -> (step, lat, lon)
            elif numpy_data.ndim == 3:
                # Could be (time, lat, lon) or (step, lat, lon)
                pass

        coords = {
            'latitude': final_lats,
            'longitude': final_lons,
            'time': times,
            'n_timesteps': numpy_data.shape[0] if numpy_data.ndim >= 1 else 1
        }

        print(f"✅ {member} loaded successfully!")
        print(f"   Data shape: {numpy_data.shape}")
        print(f"   Timesteps: {coords['n_timesteps']}")
        print(f"   Memory: ~{numpy_data.nbytes / 1024 / 1024:.1f} MB")

        return numpy_data.values if hasattr(numpy_data, 'values') else numpy_data, coords

    except Exception as e:
        print(f"❌ Error loading {member}: {e}")
        import traceback
        traceback.print_exc()
        return None, None


def load_ecmwf_data_numpy(parquet_file, variable='t2m'):
    """
    Load ECMWF data using direct numpy extraction (no xarray).

    This method:
    - Directly reconstructs numpy arrays from zarr chunks
    - Doesn't require xarray at all
    - More control but less convenience

    Args:
        parquet_file: Path to Stage 3 output parquet file
        variable: Variable to extract (default: 't2m')

    Returns:
        tuple: (numpy_array, coordinates_dict)
    """
    member = parquet_file.stem.replace('stage3_', '').replace('_final', '')
    print(f"\n📊 Loading {member} using direct numpy method...")

    try:
        # Read zarr store from parquet
        zstore = read_parquet_to_zarr_store(parquet_file)

        # Get variable path mapping
        var_paths = get_variable_path_mapping()

        if variable not in var_paths:
            print(f"   ⚠️ Variable {variable} not in mapping. Available: {list(var_paths.keys())[:10]}")
            return None, None

        full_var_path = var_paths[variable]
        print(f"   Extracting from path: {full_var_path}")

        # Extract variable array
        array = extract_variable_numpy(zstore, full_var_path)

        if array is None:
            return None, None

        # Extract coordinates
        path_parts = full_var_path.split('/')
        group_path = '/'.join(path_parts[:-1])

        # Get latitude
        lat_path = f"{group_path}/latitude"
        lats = extract_variable_numpy(zstore, lat_path)
        if lats is not None:
            lats = lats.flatten()

        # Get longitude
        lon_path = f"{group_path}/longitude"
        lons = extract_variable_numpy(zstore, lon_path)
        if lons is not None:
            lons = lons.flatten()

        # Get time/step
        step_path = f"{group_path}/step"
        steps = extract_variable_numpy(zstore, step_path)

        print(f"   Variable shape: {array.shape}")
        print(f"   Latitude shape: {lats.shape if lats is not None else 'N/A'}")
        print(f"   Longitude shape: {lons.shape if lons is not None else 'N/A'}")

        # Handle different dimension structures
        if array.ndim == 4:
            # (time, step, lat, lon)
            if array.shape[0] == 1:
                array = array[0]  # -> (step, lat, lon)

        coords = {
            'latitude': lats,
            'longitude': lons,
            'time': steps if steps is not None else np.arange(array.shape[0]),
            'n_timesteps': array.shape[0] if array.ndim >= 1 else 1
        }

        print(f"✅ {member} loaded successfully!")
        print(f"   Data shape: {array.shape}")
        print(f"   Timesteps: {coords['n_timesteps']}")
        print(f"   Memory: ~{array.nbytes / 1024 / 1024:.1f} MB")

        return array, coords

    except Exception as e:
        print(f"❌ Error loading {member}: {e}")
        import traceback
        traceback.print_exc()
        return None, None


def plot_timesteps_panel(data, coords, member, variable='t2m', output_dir=None):
    """
    Create multi-panel plot showing multiple timesteps for single member.

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
    plot_steps = list(range(0, n_timesteps, max(1, n_timesteps // 12)))  # ~12 plots
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
    if variable in ['2t', 't2m']:
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

        # Create plot
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

        # Add map features
        ax.add_feature(cfeature.COASTLINE, linewidth=0.5)
        ax.add_feature(cfeature.BORDERS, linewidth=0.3, alpha=0.5)
        ax.add_feature(cfeature.LAND, alpha=0.1)

        # Set extent
        if USE_REGIONAL_SUBSET:
            ax.set_extent([LON_MIN, LON_MAX, LAT_MIN, LAT_MAX])

        # Title
        ax.set_title(f'Step {step}/{n_timesteps-1}',
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
                f'All {n_timesteps} Forecast Steps\n'
                f'Zarr V2 Method (Improved)',
                fontsize=16, y=0.98)

    # Save figure
    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(exist_ok=True, parents=True)
        output_file = output_dir / f'zarrv2_improved_{member}_{variable}_timesteps.png'
    else:
        output_file = f'zarrv2_improved_{member}_{variable}_timesteps.png'

    plt.tight_layout(rect=[0, 0, 0.9, 0.96])
    plt.savefig(str(output_file), dpi=150, bbox_inches='tight')
    print(f"✅ Panel plot saved: {output_file}")
    plt.close()

    return str(output_file)


def plot_timeseries_summary(data, coords, member, variable='t2m', output_dir=None):
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

    # Use step indices
    steps = np.arange(n_timesteps)

    # Create plot
    fig, ax = plt.subplots(figsize=(12, 6))

    # Plot mean with shaded min/max
    ax.plot(steps, mean_vals, 'b-', linewidth=2, label='Mean')
    ax.fill_between(steps, min_vals, max_vals, alpha=0.3, color='blue', label='Min-Max Range')

    # Variable-specific labels
    if variable in ['2t', 't2m']:
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

    ax.set_xlabel('Forecast Step', fontsize=12)
    ax.set_ylabel(f'{var_label} ({var_units})', fontsize=12)
    ax.set_title(f'ECMWF {var_label} - {member.upper()}\n'
                f'Spatial Statistics over All {n_timesteps} Forecast Steps\n'
                f'Zarr V2 Method (Improved)',
                fontsize=14)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)

    # Save figure
    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(exist_ok=True, parents=True)
        output_file = output_dir / f'zarrv2_improved_{member}_{variable}_timeseries.png'
    else:
        output_file = f'zarrv2_improved_{member}_{variable}_timeseries.png'

    plt.tight_layout()
    plt.savefig(str(output_file), dpi=150, bbox_inches='tight')
    print(f"✅ Timeseries plot saved: {output_file}")
    plt.close()

    return str(output_file)


def main():
    """Main function."""
    parser = argparse.ArgumentParser(description='Read ECMWF Stage 3 output using Zarr V2 method (Improved)')
    parser.add_argument('--member', type=str, default='control',
                       help='Ensemble member to process (default: control)')
    parser.add_argument('--variable', type=str, default='t2m',
                       help='Variable to extract (default: t2m)')
    parser.add_argument('--input-dir', type=Path, default=DEFAULT_INPUT_DIR,
                       help='Input directory with Stage 3 parquet files')
    parser.add_argument('--output', type=Path, default=None,
                       help='Output directory for plots')
    parser.add_argument('--method', type=str, default='xarray', choices=['xarray', 'numpy'],
                       help='Extraction method: xarray or numpy (default: xarray)')
    args = parser.parse_args()

    print("="*80)
    print("ECMWF Stage 3 Parquet Reader - Zarr V2 Method (IMPROVED)")
    print("="*80)
    print(f"Member: {args.member}")
    print(f"Variable: {args.variable}")
    print(f"Input: {args.input_dir}")
    print(f"Method: Zarr V2 ({args.method})")
    print("="*80)

    # Find parquet file
    parquet_file = args.input_dir / f"stage3_{args.member}_final.parquet"

    if not parquet_file.exists():
        print(f"❌ Error: Parquet file not found: {parquet_file}")
        print(f"\nAvailable files in {args.input_dir}:")
        for f in sorted(args.input_dir.glob("stage3_*_final.parquet")):
            print(f"   - {f.name}")
        return False

    # Load data using selected method
    if args.method == 'xarray':
        data, coords = load_ecmwf_data_xarray(parquet_file, variable=args.variable)
    else:
        data, coords = load_ecmwf_data_numpy(parquet_file, variable=args.variable)

    if data is None:
        print("❌ Failed to load data")
        return False

    # Get number of timesteps
    n_timesteps = data.shape[0]
    print(f"\n✅ Successfully loaded {n_timesteps} timesteps")

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
