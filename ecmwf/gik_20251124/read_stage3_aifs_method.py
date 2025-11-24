#!/usr/bin/env python3
"""
ECMWF Stage 3 Parquet Reader - AIFS-ETL Method

This script EXACTLY replicates the aifs-etl.py extraction flow:
1. read_parquet_to_refs() - Read parquet to zarr references
2. extract_variable_hybrid() - Extract with S3 fetching and GRIB2 decoding
3. Proper chunk reassembly into numpy arrays

Works on the aggregated arrays in Stage 3 output (currently 2 timesteps).

Usage:
    python read_stage3_aifs_method.py --member control --variable t2m
"""

import pandas as pd
import numpy as np
import json
import os
import argparse
from pathlib import Path
import base64
import tempfile
import time

# Configuration
DEFAULT_INPUT_DIR = Path("/scratch/notebook/test_ecmwf_three_stage_prebuilt_output")

# Set up anonymous S3 access
os.environ['AWS_NO_SIGN_REQUEST'] = 'YES'


def read_parquet_to_refs(parquet_path):
    """
    Read parquet file and extract zarr references.
    EXACT copy from aifs-etl.py
    """
    print(f"  📊 Reading parquet file: {parquet_path}")
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
    """
    Decode a chunk reference. Returns (type, data).
    EXACT copy from aifs-etl.py
    """
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


def fetch_s3_byte_range_fsspec(url, offset, length, max_retries=3, retry_delay=2):
    """
    Fetch a byte range from S3 using fsspec with retry logic.
    EXACT copy from aifs-etl.py
    """
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
                print(f"    ⚠️  fsspec attempt {attempt + 1}/{max_retries} failed: {e}")
                print(f"    🔄 Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
            else:
                print(f"    ❌ Error fetching from S3 with fsspec after {max_retries} attempts: {e}")
                return None

    return None


def fetch_s3_byte_range_obstore(url, offset, length):
    """
    Fetch a byte range from S3 using obstore (if available).
    EXACT copy from aifs-etl.py
    """
    try:
        import obstore as obs
        from obstore.store import from_url

        # Parse bucket and key from URL
        if url.startswith('s3://'):
            url_parts = url[5:].split('/', 1)
            bucket = url_parts[0]
            key = url_parts[1] if len(url_parts) > 1 else ''
        else:
            raise ValueError(f"Invalid S3 URL: {url}")

        # ECMWF buckets are in EU regions
        bucket_regions = {
            'ecmwf-forecasts': 'eu-central-1',
        }
        region = bucket_regions.get(bucket, 'eu-central-1')

        # Create S3 store
        bucket_url = f"s3://{bucket}"
        store = from_url(bucket_url, region=region, skip_signature=True)

        # Fetch byte range
        result = obs.get_range(store, key, start=offset, end=offset + length)
        data = bytes(result)

        return data

    except ImportError:
        return fetch_s3_byte_range_fsspec(url, offset, length)
    except Exception as e:
        print(f"    ⚠️ obstore error: {e}, falling back to fsspec")
        return fetch_s3_byte_range_fsspec(url, offset, length)


def extract_variable_hybrid(zstore, variable_path, use_obstore=False):
    """
    Extract a variable handling both base64 and S3 references.
    EXACT copy from aifs-etl.py with minor adaptations
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

    print(f"    Variable metadata:")
    print(f"      Shape: {shape}")
    print(f"      Chunks: {chunks}")
    print(f"      Dtype: {dtype}")

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
                url, offset, length = ref_data

                print(f"      Fetching chunk {key.split('/')[-1]} from S3...")

                # Fetch from S3
                if use_obstore:
                    data = fetch_s3_byte_range_obstore(url, offset, length)
                else:
                    data = fetch_s3_byte_range_fsspec(url, offset, length)

                if data is None:
                    print(f"      ⚠️  Skipping chunk {key} - could not fetch from S3")
                    continue

                if data is not None:
                    # Check if it's GRIB2 data
                    if data[:4] == b'GRIB':
                        # Decode GRIB2 message
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
                                chunks_data[key] = var_data
                                print(f"      ✅ Decoded GRIB2 chunk: shape={var_data.shape}")

                            os.unlink(tmp_path)
                            ds.close()

                        except ImportError:
                            print(f"      ⚠️ cfgrib not available, trying eccodes")
                            try:
                                import eccodes
                                gid = eccodes.codes_new_from_message(data)
                                values = eccodes.codes_get_array(gid, 'values')
                                eccodes.codes_release(gid)
                                chunks_data[key] = values
                                print(f"      ✅ Decoded with eccodes: size={len(values)}")
                            except:
                                print(f"      ❌ Cannot decode GRIB2 data")
                        except Exception as e:
                            print(f"      ❌ Error decoding GRIB2: {e}")
                    else:
                        # Try decompression if needed
                        if compressor is not None:
                            try:
                                import numcodecs
                                codec = numcodecs.get_codec(compressor)
                                data = codec.decode(data)
                                chunks_data[key] = data
                            except:
                                pass
                        else:
                            chunks_data[key] = data

    if not chunks_data:
        print(f"    ❌ No chunks successfully loaded")
        return None

    print(f"    Successfully loaded {len(chunks_data)} chunks")

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

                # GRIB2 data comes as 2D, but metadata expects 4D
                if chunk_array.ndim == 2 and len(shape) == 4:
                    time_idx = chunk_indices[0] if len(chunk_indices) > 0 else 0
                    step_idx = chunk_indices[1] if len(chunk_indices) > 1 else 0
                    array[time_idx, step_idx, :, :] = chunk_array
                    print(f"      Placed 2D chunk at [{time_idx}, {step_idx}, :, :]")
                else:
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


def get_variable_path_mapping():
    """
    Map parameter names to their paths in the zarr store.
    Based on aifs-etl.py but adapted for Stage 3 structure
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


def extract_ecmwf_stage3(parquet_file, variable='t2m', use_obstore=False):
    """
    Extract ECMWF data from Stage 3 parquet using AIFS-ETL method.

    Args:
        parquet_file: Path to Stage 3 parquet file
        variable: Variable to extract
        use_obstore: Use obstore for S3 fetching (faster)

    Returns:
        tuple: (numpy_array, metadata_dict)
    """
    member = parquet_file.stem.replace('stage3_', '').replace('_final', '')

    print(f"\n{'='*60}")
    print(f"Extracting {variable} for {member} using AIFS-ETL method")
    print(f"{'='*60}")

    # Step 1: Read parquet to zarr refs
    zstore = read_parquet_to_refs(parquet_file)

    # Step 2: Get variable path
    var_paths = get_variable_path_mapping()
    if variable not in var_paths:
        print(f"  ❌ Variable {variable} not found in mapping")
        print(f"  Available: {list(var_paths.keys())[:10]}")
        return None, None

    var_path = var_paths[variable]
    print(f"  Variable path: {var_path}")

    # Step 3: Extract variable using hybrid method
    print(f"\n  Extracting variable...")
    array = extract_variable_hybrid(zstore, var_path, use_obstore=use_obstore)

    if array is None:
        return None, None

    print(f"\n  ✅ Successfully extracted!")
    print(f"  Array shape: {array.shape}")
    print(f"  Array dtype: {array.dtype}")
    print(f"  Memory: ~{array.nbytes / 1024 / 1024:.1f} MB")

    # Extract coordinates
    path_parts = var_path.split('/')
    group_path = '/'.join(path_parts[:-1])

    # Try to get lat/lon
    lat_path = f"{group_path}/latitude"
    lon_path = f"{group_path}/longitude"
    step_path = f"{group_path}/step"

    print(f"\n  Extracting coordinates...")
    lats = extract_variable_hybrid(zstore, lat_path, use_obstore=False)
    lons = extract_variable_hybrid(zstore, lon_path, use_obstore=False)
    steps = extract_variable_hybrid(zstore, step_path, use_obstore=False)

    if lats is not None:
        lats = lats.flatten()
        print(f"    Latitude: {lats.shape}")
    if lons is not None:
        lons = lons.flatten()
        print(f"    Longitude: {lons.shape}")
    if steps is not None:
        steps = steps.flatten()
        print(f"    Steps: {steps.shape} - values: {steps}")

    metadata = {
        'variable': variable,
        'member': member,
        'latitude': lats,
        'longitude': lons,
        'steps': steps,
        'shape': array.shape
    }

    return array, metadata


def save_to_pickle(data, metadata, output_file):
    """Save extracted data to pickle file (like aifs-etl.py)."""
    import pickle

    output_data = {
        'data': data,
        'metadata': metadata
    }

    with open(output_file, 'wb') as f:
        pickle.dump(output_data, f)

    file_size = Path(output_file).stat().st_size / (1024 * 1024)
    print(f"\n💾 Saved to: {output_file}")
    print(f"📊 File size: {file_size:.2f} MB")


def main():
    """Main function."""
    parser = argparse.ArgumentParser(description='Extract ECMWF Stage 3 using AIFS-ETL method')
    parser.add_argument('--member', type=str, default='control',
                       help='Ensemble member (default: control)')
    parser.add_argument('--variable', type=str, default='t2m',
                       help='Variable to extract (default: t2m)')
    parser.add_argument('--input-dir', type=Path, default=DEFAULT_INPUT_DIR,
                       help='Input directory')
    parser.add_argument('--output', type=Path, default=None,
                       help='Output pickle file')
    parser.add_argument('--use-obstore', action='store_true',
                       help='Use obstore for S3 fetching')
    args = parser.parse_args()

    print("="*80)
    print("ECMWF Stage 3 Extraction - AIFS-ETL Method")
    print("="*80)
    print(f"Member: {args.member}")
    print(f"Variable: {args.variable}")
    print(f"S3 method: {'obstore' if args.use_obstore else 'fsspec'}")
    print("="*80)

    # Find parquet file
    parquet_file = args.input_dir / f"stage3_{args.member}_final.parquet"

    if not parquet_file.exists():
        print(f"❌ Error: Parquet file not found: {parquet_file}")
        return False

    # Extract data
    data, metadata = extract_ecmwf_stage3(parquet_file, args.variable, args.use_obstore)

    if data is None:
        print("❌ Failed to extract data")
        return False

    # Save to pickle if requested
    if args.output:
        save_to_pickle(data, metadata, args.output)

    print(f"\n{'='*80}")
    print("✅ Extraction Complete!")
    print(f"{'='*80}")
    print(f"Shape: {data.shape}")
    print(f"Timesteps extracted: {data.shape[1] if len(data.shape) > 1 else 1}")

    return True


if __name__ == "__main__":
    success = main()
    if not success:
        exit(1)
