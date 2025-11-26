#!/usr/bin/env python3
"""
ECMWF Stage 3 Parquet Reader - Xarray Dataset Creation

This script extracts data from ECMWF Stage 3 parquet files and creates
proper xarray datasets with full coordinate support and metadata.

Features:
1. Extracts all 85 timesteps using AIFS-ETL method
2. Builds proper xarray Dataset with dimensions and coordinates
3. Extracts model run datetime from parquet
4. Regional subsetting using xarray's .sel() method (clean and intuitive)
5. Saves as NetCDF (recommended) or pickle
6. Includes utilities to read/load saved datasets

Usage:
    # Extract East Africa region (default)
    python read_stage3_to_xarray.py --member ens_02 --variable tp --output e02_precip.nc

    # Extract with different region
    python read_stage3_to_xarray.py --member control --variable 2t --region europe

    # Extract with custom bounds
    python read_stage3_to_xarray.py --member control --variable 2t --custom-region -12 23 21 53

    # Extract global data
    python read_stage3_to_xarray.py --member control --variable 2t --no-subset
"""

import pandas as pd
import numpy as np
import xarray as xr
import json
import os
import argparse
from pathlib import Path
import base64
import tempfile
import time
import re
import datetime
import struct
from typing import Optional, Tuple, Dict
import pickle

# Configuration
DEFAULT_INPUT_DIR = Path("/scratch/notebook/test_ecmwf_three_stage_prebuilt_output")

# Set up anonymous S3 access
os.environ['AWS_NO_SIGN_REQUEST'] = 'YES'

# Predefined regions (same as before)
PREDEFINED_REGIONS = {
    'east-africa': {
        'name': 'East Africa',
        'lat_min': -12, 'lat_max': 23,
        'lon_min': 21, 'lon_max': 53
    },
    'europe-africa': {
        'name': 'Europe + Africa',
        'lat_min': -12, 'lat_max': 55,
        'lon_min': -25, 'lon_max': 65
    },
    'europe': {
        'name': 'Europe',
        'lat_min': 35, 'lat_max': 70,
        'lon_min': -10, 'lon_max': 40
    },
    'north-america': {
        'name': 'North America',
        'lat_min': 15, 'lat_max': 72,
        'lon_min': -170, 'lon_max': -50
    },
    'south-asia': {
        'name': 'South Asia',
        'lat_min': 5, 'lat_max': 40,
        'lon_min': 60, 'lon_max': 100
    },
    'global': {
        'name': 'Global',
        'lat_min': -90, 'lat_max': 90,
        'lon_min': -180, 'lon_max': 180
    },
    'eurasia': {
        'name': 'eurasia',
        'lat_min': -38, 'lat_max': 38,
        'lon_min': -58, 'lon_max': 96
    }
}


# =============================================================================
# CORE EXTRACTION FUNCTIONS (from read_stage3_aifs_all_timesteps.py)
# =============================================================================

def read_parquet_to_refs(parquet_path):
    """Read parquet file and extract zarr references."""
    print(f"  📊 Reading parquet file: {parquet_path.name}")
    df = pd.read_parquet(parquet_path)

    zstore = {}
    for _, row in df.iterrows():
        key = row['key']
        value = row['value']

        if isinstance(value, bytes):
            value = value.decode('utf-8')

        if isinstance(value, str) and (value.startswith('[') or value.startswith('{')):
            try:
                value = json.loads(value)
            except:
                pass

        zstore[key] = value

    if 'version' in zstore:
        del zstore['version']

    print(f"  ✅ Loaded {len(zstore)} references")
    return zstore


def decode_chunk_reference(chunk_ref):
    """Decode a chunk reference."""
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


def fetch_s3_byte_range_fsspec(url, offset, length, max_retries=3, retry_delay=2):
    """Fetch a byte range from S3 using fsspec."""
    for attempt in range(max_retries):
        try:
            import fsspec

            if url.startswith('s3://'):
                s3_path = url
            else:
                s3_path = f"s3://{url}"

            fs = fsspec.filesystem('s3', anon=True)

            with fs.open(s3_path, 'rb') as f:
                f.seek(offset)
                data = f.read(length)

            return data

        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                print(f"    ❌ S3 fetch failed after {max_retries} attempts: {e}")
                return None

    return None


def decode_grib2_data(data: bytes) -> Optional[np.ndarray]:
    """Decode GRIB2 data to numpy array."""
    if data[:4] != b'GRIB':
        return None

    try:
        import cfgrib
        import xarray as xr

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
        return None

    except Exception as e:
        print(f"    ⚠️ GRIB2 decode failed: {e}")
        return None


def extract_single_timestep(zstore: Dict, variable: str, step_hour: int,
                           member: str = 'control', use_obstore: bool = False) -> Optional[np.ndarray]:
    """Extract a single timestep from step_XXX arrays."""
    step_key = f"step_{step_hour:03d}/{variable}/sfc/{member}/0.0.0"

    if step_key not in zstore:
        for level in ['sf', 'surface']:
            alt_key = f"step_{step_hour:03d}/{variable}/{level}/{member}/0.0.0"
            if alt_key in zstore:
                step_key = alt_key
                break
        else:
            return None

    chunk_ref = zstore[step_key]
    ref_type, ref_data = decode_chunk_reference(chunk_ref)

    if ref_type == 'base64':
        data = ref_data
        try:
            import numcodecs
            array = np.frombuffer(data, dtype='<f4')
            if array.size == 721 * 1440:
                return array.reshape((721, 1440))
        except:
            pass
        return None

    elif ref_type == 's3':
        url, offset, length = ref_data

        # FIX: Add .grib2 extension if missing
        if not url.endswith('.grib2') and not url.endswith('.grb') and not url.endswith('.grb2'):
            url = url + '.grib2'

        chunk_data = fetch_s3_byte_range_fsspec(url, offset, length)

        if chunk_data is None:
            return None

        array_2d = decode_grib2_data(chunk_data)
        return array_2d

    return None


# =============================================================================
# DATETIME EXTRACTION
# =============================================================================

def get_model_run_time(zstore: Dict, variable: str = 't2m') -> Optional[datetime.datetime]:
    """
    Extract model run datetime from zarr store.

    Tries two methods:
    1. Time coordinate in parquet (recommended)
    2. S3 URL pattern parsing (fallback)

    Args:
        zstore: Zarr store dictionary
        variable: Variable name (used to find coordinate path)

    Returns:
        datetime object or None
    """
    # Method 1: Try to extract from time coordinate
    var_paths = {
        't2m': 't2m/instant/heightAboveGround',
        '2t': 't2m/instant/heightAboveGround',
        'tp': 'tp/accum/surface',
        '10u': 'u10/instant/heightAboveGround',
        '10v': 'v10/instant/heightAboveGround',
    }

    if variable in var_paths:
        time_key = f"{var_paths[variable]}/time/0"
        if time_key in zstore:
            value = zstore[time_key]
            if isinstance(value, str) and value.startswith('base64:'):
                try:
                    decoded = base64.b64decode(value[7:])
                    time_val = struct.unpack('<q', decoded)[0]  # int64
                    return datetime.datetime.utcfromtimestamp(time_val)
                except:
                    pass

    # Method 2: Parse S3 URL (fallback)
    for key in zstore.keys():
        if key.startswith('step_000/'):
            ref = zstore[key]
            if isinstance(ref, list) and len(ref) >= 1:
                url = ref[0]
                # URL format: s3://ecmwf-forecasts/YYYYMMDD/HHz/...
                match = re.search(r'/(\d{8})/(\d{2})z/', url)
                if match:
                    date_str = match.group(1)
                    hour = int(match.group(2))
                    year = int(date_str[:4])
                    month = int(date_str[4:6])
                    day = int(date_str[6:8])
                    return datetime.datetime(year, month, day, hour)

    return None


# =============================================================================
# XARRAY DATASET CREATION
# =============================================================================

def create_xarray_dataset(data_3d: np.ndarray,
                          lats: np.ndarray,
                          lons: np.ndarray,
                          forecast_hours: list,
                          variable: str,
                          member: str,
                          model_run_time: Optional[datetime.datetime] = None,
                          metadata: Optional[Dict] = None) -> xr.Dataset:
    """
    Create a proper xarray Dataset from extracted numpy data.

    Args:
        data_3d: 3D numpy array (time, lat, lon)
        lats: Latitude coordinates
        lons: Longitude coordinates
        forecast_hours: List of forecast hours [0, 3, 6, ..., 360]
        variable: Variable name
        member: Ensemble member name
        model_run_time: Model initialization time
        metadata: Additional metadata dict

    Returns:
        xarray.Dataset with proper coordinates and attributes
    """
    print(f"\n  🔧 Creating xarray Dataset...")

    # Create time coordinate
    if model_run_time is not None:
        # Create proper datetime64 time coordinate
        times = [model_run_time + datetime.timedelta(hours=int(h)) for h in forecast_hours]
        time_coord = pd.DatetimeIndex(times)
    else:
        # Use forecast hours as time coordinate
        time_coord = forecast_hours

    # Variable name mapping
    var_name_map = {
        '2t': 't2m',
        't2m': 't2m',
        'tp': 'tp',
        '10u': 'u10',
        '10v': 'v10',
        'msl': 'msl',
        'sp': 'sp',
    }
    var_name = var_name_map.get(variable, variable)

    # Variable attributes
    var_attrs = {
        't2m': {
            'long_name': '2 metre temperature',
            'units': 'K',
            'standard_name': 'air_temperature'
        },
        'tp': {
            'long_name': 'Total precipitation',
            'units': 'mm',
            'standard_name': 'precipitation_amount'
        },
        'u10': {
            'long_name': '10 metre U wind component',
            'units': 'm/s',
            'standard_name': 'eastward_wind'
        },
        'v10': {
            'long_name': '10 metre V wind component',
            'units': 'm/s',
            'standard_name': 'northward_wind'
        },
        'msl': {
            'long_name': 'Mean sea level pressure',
            'units': 'Pa',
            'standard_name': 'air_pressure_at_mean_sea_level'
        },
    }

    # Create DataArray
    data_array = xr.DataArray(
        data_3d,
        coords={
            'time': time_coord,
            'latitude': lats,
            'longitude': lons,
        },
        dims=['time', 'latitude', 'longitude'],
        attrs=var_attrs.get(var_name, {'long_name': variable})
    )

    # Create Dataset
    ds = xr.Dataset({
        var_name: data_array
    })

    # Add forecast_hour as a coordinate variable
    ds['forecast_hour'] = ('time', forecast_hours)
    ds['forecast_hour'].attrs = {
        'long_name': 'Forecast hour',
        'units': 'hours',
        'description': 'Hours since model initialization'
    }

    # Add global attributes
    ds.attrs['title'] = f'ECMWF {var_name} forecast'
    ds.attrs['institution'] = 'ECMWF'
    ds.attrs['source'] = 'ECMWF IFS'
    ds.attrs['ensemble_member'] = member
    ds.attrs['variable'] = variable

    if model_run_time is not None:
        ds.attrs['model_initialization_time'] = model_run_time.strftime('%Y-%m-%d %H:%M:%S UTC')
        ds.attrs['model_initialization_time_unix'] = int(model_run_time.timestamp())

    if metadata:
        if metadata.get('regional_subset'):
            ds.attrs['regional_subset'] = 'True'
            bounds = metadata.get('subset_bounds', {})
            ds.attrs['subset_lat_min'] = bounds.get('lat_min', '')
            ds.attrs['subset_lat_max'] = bounds.get('lat_max', '')
            ds.attrs['subset_lon_min'] = bounds.get('lon_min', '')
            ds.attrs['subset_lon_max'] = bounds.get('lon_max', '')

    ds.attrs['creation_time'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')
    ds.attrs['creator'] = 'read_stage3_to_xarray.py'

    # Set coordinate attributes
    ds['latitude'].attrs = {
        'long_name': 'Latitude',
        'units': 'degrees_north',
        'standard_name': 'latitude'
    }
    ds['longitude'].attrs = {
        'long_name': 'Longitude',
        'units': 'degrees_east',
        'standard_name': 'longitude'
    }
    ds['time'].attrs = {
        'long_name': 'Valid time',
        'standard_name': 'time'
    }

    print(f"  ✅ Created xarray Dataset: {ds.dims}")

    return ds


def apply_regional_subset_xarray(ds: xr.Dataset,
                                 lat_min: float, lat_max: float,
                                 lon_min: float, lon_max: float) -> xr.Dataset:
    """
    Apply regional subset using xarray's .sel() method.

    This is much cleaner than numpy indexing!

    Args:
        ds: xarray Dataset
        lat_min, lat_max: Latitude bounds
        lon_min, lon_max: Longitude bounds

    Returns:
        Subsetted xarray Dataset
    """
    print(f"\n  🌍 Applying regional subset using xarray.sel()...")
    print(f"     Bounds: lat[{lat_min}:{lat_max}], lon[{lon_min}:{lon_max}]")

    original_size = ds.nbytes / (1024 * 1024)
    print(f"     Original size: {original_size:.1f} MB")

    # Xarray's .sel() with slice is clean and intuitive!
    ds_subset = ds.sel(
        latitude=slice(lat_max, lat_min),  # Note: reversed for descending lat
        longitude=slice(lon_min, lon_max)
    )

    subset_size = ds_subset.nbytes / (1024 * 1024)
    print(f"     Subset size: {subset_size:.1f} MB")
    print(f"     Reduction: {(1 - subset_size/original_size)*100:.1f}%")
    print(f"  ✅ Subset shape: {dict(ds_subset.dims)}")

    # Add subsetting metadata
    ds_subset.attrs['regional_subset'] = 'True'
    ds_subset.attrs['subset_lat_min'] = lat_min
    ds_subset.attrs['subset_lat_max'] = lat_max
    ds_subset.attrs['subset_lon_min'] = lon_min
    ds_subset.attrs['subset_lon_max'] = lon_max

    return ds_subset


# =============================================================================
# MAIN EXTRACTION WITH XARRAY
# =============================================================================

def extract_to_xarray(parquet_file: Path,
                      variable: str = '2t',
                      member: str = 'control',
                      region: Optional[Dict] = None,
                      use_obstore: bool = False) -> xr.Dataset:
    """
    Extract data from parquet and create xarray Dataset.

    Args:
        parquet_file: Path to parquet file
        variable: Variable to extract
        member: Ensemble member
        region: Dictionary with lat_min, lat_max, lon_min, lon_max (None = global)
        use_obstore: Use obstore for S3 fetching

    Returns:
        xarray.Dataset
    """
    print(f"\n{'='*80}")
    print(f"Extracting to xarray Dataset: {variable} - {member}")
    print(f"{'='*80}")

    # Step 1: Read parquet
    zstore = read_parquet_to_refs(parquet_file)

    # Step 2: Extract model run datetime
    print(f"\n  📅 Extracting model run datetime...")
    model_run_time = get_model_run_time(zstore, variable)
    if model_run_time:
        print(f"  ✅ Model run: {model_run_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    else:
        print(f"  ⚠️ Could not extract model run time")

    # Step 3: Find timesteps (same logic as before)
    member_normalized = member.replace('_', '')
    patterns_to_try = [
        rf'step_(\d+)/{variable}/sfc/{member}/0\.0\.0',
        rf'step_(\d+)/{variable}/sfc/{member_normalized}/0\.0\.0',
        rf'step_(\d+)/{variable}/sf/{member}/0\.0\.0',
        rf'step_(\d+)/{variable}/sf/{member_normalized}/0\.0\.0',
    ]

    available_steps = []
    actual_member_name = member

    for pattern_str in patterns_to_try:
        step_pattern = re.compile(pattern_str)
        for key in zstore.keys():
            match = step_pattern.match(key)
            if match:
                step_hour = int(match.group(1))
                available_steps.append(step_hour)
                parts = key.split('/')
                if len(parts) >= 4:
                    actual_member_name = parts[3]

        if available_steps:
            print(f"  Using member name: '{actual_member_name}' (from parquet)")
            break

    available_steps = sorted(list(set(available_steps)))
    n_steps = len(available_steps)

    print(f"\n  Found {n_steps} timesteps")
    print(f"  Range: {min(available_steps)}h to {max(available_steps)}h")

    if n_steps == 0:
        raise ValueError(f"No timesteps found for {variable}/{member}")

    # Step 4: Extract each timestep
    print(f"\n  Extracting timesteps...")
    timestep_arrays = []

    for i, step_hour in enumerate(available_steps):
        if i % 10 == 0 or i == n_steps - 1:
            print(f"    Progress: {i+1}/{n_steps} (step {step_hour}h)")

        array_2d = extract_single_timestep(zstore, variable, step_hour, actual_member_name, use_obstore)

        if array_2d is not None:
            timestep_arrays.append(array_2d)
        else:
            print(f"    ⚠️ Failed: step {step_hour}h")

    if not timestep_arrays:
        raise ValueError("No timesteps successfully extracted")

    # Step 5: Stack into 3D array
    print(f"\n  Stacking {len(timestep_arrays)} timesteps...")
    data_3d = np.stack(timestep_arrays, axis=0)
    print(f"  ✅ Data shape: {data_3d.shape}")

    # Step 6: Extract coordinates
    print(f"\n  Extracting coordinates...")
    var_mapping = {
        '2t': 't2m/instant/heightAboveGround',
        't2m': 't2m/instant/heightAboveGround',
        'tp': 'tp/accum/surface',
        '10u': 'u10/instant/heightAboveGround',
        '10v': 'v10/instant/heightAboveGround',
    }

    lats = None
    lons = None

    if variable in var_mapping:
        coord_path = var_mapping[variable]

        lat_key = f"{coord_path}/latitude/.zarray"
        if lat_key in zstore:
            lat_data_key = f"{coord_path}/latitude/0"
            if lat_data_key in zstore:
                lat_ref = zstore[lat_data_key]
                if isinstance(lat_ref, str) and lat_ref.startswith('base64:'):
                    try:
                        decoded = base64.b64decode(lat_ref[7:])
                        lats = np.frombuffer(decoded, dtype='<f8')
                    except:
                        pass

        lon_key = f"{coord_path}/longitude/.zarray"
        if lon_key in zstore:
            lon_data_key = f"{coord_path}/longitude/0"
            if lon_data_key in zstore:
                lon_ref = zstore[lon_data_key]
                if isinstance(lon_ref, str) and lon_ref.startswith('base64:'):
                    try:
                        decoded = base64.b64decode(lon_ref[7:])
                        lons = np.frombuffer(decoded, dtype='<f8')
                    except:
                        pass

    if lats is None or lons is None:
        print(f"    Using default ECMWF 0.25° grid")
        lats = np.linspace(90, -90, 721)
        lons = np.linspace(0, 360, 1440)
        # Convert to -180 to 180
        lons = np.where(lons > 180, lons - 360, lons)
    else:
        print(f"    ✅ Extracted coordinates from parquet")
        # Convert lons to -180 to 180 if needed
        if lons.max() > 180:
            lons = np.where(lons > 180, lons - 360, lons)

    # Step 7: Create xarray Dataset
    ds = create_xarray_dataset(
        data_3d, lats, lons, available_steps[:len(timestep_arrays)],
        variable, member, model_run_time
    )

    # Step 8: Apply regional subset if specified (using xarray!)
    if region is not None:
        ds = apply_regional_subset_xarray(
            ds,
            region['lat_min'], region['lat_max'],
            region['lon_min'], region['lon_max']
        )

    return ds


# =============================================================================
# SAVE AND LOAD UTILITIES
# =============================================================================

def save_xarray_dataset(ds: xr.Dataset, output_file: Path, format: str = 'netcdf'):
    """
    Save xarray Dataset to file.

    Args:
        ds: xarray Dataset
        output_file: Output file path
        format: 'netcdf' (recommended) or 'pickle'
    """
    output_file = Path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n💾 Saving dataset...")

    if format == 'netcdf' or output_file.suffix == '.nc':
        # Save as NetCDF (recommended for xarray)
        ds.to_netcdf(output_file, engine='netcdf4')
        print(f"  Format: NetCDF")
    elif format == 'pickle' or output_file.suffix == '.pkl':
        # Save as pickle
        with open(output_file, 'wb') as f:
            pickle.dump(ds, f)
        print(f"  Format: Pickle")
    else:
        raise ValueError(f"Unsupported format: {format}")

    file_size = output_file.stat().st_size / (1024 * 1024)
    print(f"  ✅ Saved to: {output_file}")
    print(f"  📊 File size: {file_size:.2f} MB")


def load_xarray_dataset(input_file: Path) -> xr.Dataset:
    """
    Load xarray Dataset from file.

    Args:
        input_file: Path to saved dataset (.nc or .pkl)

    Returns:
        xarray.Dataset
    """
    input_file = Path(input_file)

    if not input_file.exists():
        raise FileNotFoundError(f"File not found: {input_file}")

    print(f"📂 Loading dataset: {input_file}")

    if input_file.suffix == '.nc':
        # Load NetCDF
        ds = xr.open_dataset(input_file, engine='netcdf4')
        print(f"  Format: NetCDF")
    elif input_file.suffix == '.pkl':
        # Load pickle
        with open(input_file, 'rb') as f:
            ds = pickle.load(f)
        print(f"  Format: Pickle")
    else:
        # Try to infer
        try:
            ds = xr.open_dataset(input_file)
            print(f"  Format: NetCDF (inferred)")
        except:
            with open(input_file, 'rb') as f:
                ds = pickle.load(f)
            print(f"  Format: Pickle (inferred)")

    print(f"  ✅ Loaded dataset: {dict(ds.dims)}")
    return ds


# =============================================================================
# COMMAND LINE INTERFACE
# =============================================================================

def main():
    """Main function."""
    parser = argparse.ArgumentParser(
        description='Extract ECMWF Stage 3 data to xarray Dataset',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Available predefined regions:
  east-africa      East Africa (-12 to 23°N, 21 to 53°E)
  europe-africa    Europe + Africa (-12 to 55°N, -25 to 65°E)
  europe           Europe (35 to 70°N, -10 to 40°E)
  north-america    North America (15 to 72°N, -170 to -50°E)
  south-asia       South Asia (5 to 40°N, 60 to 100°E)
  global           Global coverage

Examples:
  # Extract East Africa to NetCDF (recommended)
  python read_stage3_to_xarray.py --member ens_02 --variable tp --output e02_precip.nc

  # Extract to pickle format
  python read_stage3_to_xarray.py --member control --variable 2t --output control_t2m.pkl

  # Extract with custom region
  python read_stage3_to_xarray.py --member control --variable 2t --custom-region -12 23 21 53

  # Extract global data
  python read_stage3_to_xarray.py --member control --variable 2t --no-subset --output global_t2m.nc

  # Load and inspect saved dataset
  python -c "import xarray as xr; ds = xr.open_dataset('e02_precip.nc'); print(ds)"
        '''
    )
    parser.add_argument('--member', type=str, default='control',
                       help='Ensemble member (default: control)')
    parser.add_argument('--variable', type=str, default='2t',
                       help='Variable to extract (default: 2t)')
    parser.add_argument('--input-dir', type=Path, default=DEFAULT_INPUT_DIR,
                       help='Input directory')
    parser.add_argument('--output', type=Path, required=True,
                       help='Output file (.nc for NetCDF or .pkl for pickle)')
    parser.add_argument('--use-obstore', action='store_true',
                       help='Use obstore for faster S3 fetching')
    parser.add_argument('--region', type=str, default='east-africa',
                       choices=list(PREDEFINED_REGIONS.keys()),
                       help='Predefined region for extraction (default: east-africa)')
    parser.add_argument('--custom-region', nargs=4, type=float,
                       metavar=('LAT_MIN', 'LAT_MAX', 'LON_MIN', 'LON_MAX'),
                       help='Custom region bounds (overrides --region)')
    parser.add_argument('--no-subset', action='store_true',
                       help='Extract full global data (no regional subset)')
    args = parser.parse_args()

    # Determine region
    region = None
    if not args.no_subset:
        if args.custom_region:
            region = {
                'lat_min': args.custom_region[0],
                'lat_max': args.custom_region[1],
                'lon_min': args.custom_region[2],
                'lon_max': args.custom_region[3],
                'name': 'Custom'
            }
        else:
            region = PREDEFINED_REGIONS[args.region].copy()

    # Print configuration
    print("="*80)
    print("ECMWF Stage 3 Extraction to Xarray Dataset")
    print("="*80)
    print(f"Member: {args.member}")
    print(f"Variable: {args.variable}")
    print(f"S3 method: {'obstore' if args.use_obstore else 'fsspec'}")
    print(f"Regional subset: {'Yes' if region else 'No (global)'}")
    if region:
        print(f"  Region: {region.get('name', 'Custom')}")
        print(f"  Bounds: lat[{region['lat_min']}:{region['lat_max']}], "
              f"lon[{region['lon_min']}:{region['lon_max']}]")
    print(f"Output: {args.output}")
    print("="*80)

    # Find parquet file
    parquet_file = args.input_dir / f"stage3_{args.member}_final.parquet"

    if not parquet_file.exists():
        print(f"❌ Error: Parquet file not found: {parquet_file}")
        print(f"\nAvailable files:")
        for f in sorted(args.input_dir.glob("stage3_*_final.parquet")):
            print(f"   - {f.name}")
        return False

    # Extract to xarray
    start_time = time.time()
    try:
        ds = extract_to_xarray(
            parquet_file,
            args.variable,
            args.member,
            region,
            args.use_obstore
        )
    except Exception as e:
        print(f"\n❌ Extraction failed: {e}")
        import traceback
        traceback.print_exc()
        return False

    elapsed = time.time() - start_time

    # Save dataset
    output_format = 'netcdf' if args.output.suffix == '.nc' else 'pickle'
    save_xarray_dataset(ds, args.output, output_format)

    # Print summary
    print(f"\n{'='*80}")
    print("✅ EXTRACTION COMPLETE!")
    print(f"{'='*80}")
    print(f"\nDataset Summary:")
    print(f"  Dimensions: {dict(ds.dims)}")
    print(f"  Variables: {list(ds.data_vars)}")
    print(f"  Coordinates: {list(ds.coords)}")
    print(f"  Size in memory: {ds.nbytes / (1024*1024):.1f} MB")
    print(f"  Extraction time: {elapsed:.1f} seconds")

    if 'model_initialization_time' in ds.attrs:
        print(f"\n📅 Model Run: {ds.attrs['model_initialization_time']}")

    print(f"\n📊 Data Info:")
    var_name = list(ds.data_vars)[0]
    data_var = ds[var_name]
    print(f"  Variable: {var_name}")
    if 'long_name' in data_var.attrs:
        print(f"  Long name: {data_var.attrs['long_name']}")
    if 'units' in data_var.attrs:
        print(f"  Units: {data_var.attrs['units']}")
    print(f"  Shape: {data_var.shape}")
    print(f"  Min: {float(data_var.min()):.4f}")
    print(f"  Max: {float(data_var.max()):.4f}")
    print(f"  Mean: {float(data_var.mean()):.4f}")

    print(f"\n📂 Output file: {args.output}")
    print(f"\n💡 To load this dataset in Python:")
    print(f"   import xarray as xr")
    print(f"   ds = xr.open_dataset('{args.output}')")
    print(f"   print(ds)")

    print(f"{'='*80}")

    return True


if __name__ == "__main__":
    success = main()
    if not success:
        exit(1)
