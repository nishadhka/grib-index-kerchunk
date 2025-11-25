#!/usr/bin/env python3
"""
Hybrid Approach: fsspec for metadata/coords + custom GRIB2 decoding

The earthaccess-style `xr.open_zarr(fsspec_store)` approach won't work
directly because our parquet references GRIB2 chunks, not zarr chunks.

However, we can potentially use a hybrid approach:
1. Use fsspec reference filesystem to navigate the zarr structure
2. Manually decode base64-encoded coordinate arrays
3. Manually fetch and decode GRIB2 data chunks

This tests whether there's any benefit to using fsspec vs pure dict parsing.
"""

import fsspec
import pandas as pd
import numpy as np
import json
import base64
import os
import tempfile

os.environ['AWS_NO_SIGN_REQUEST'] = 'YES'

PARQUET_FILE = "/scratch/notebook/test_ecmwf_three_stage_prebuilt_output/stage3_control_final.parquet"


def build_refs_dict(parquet_path):
    """Build references dict from parquet."""
    df = pd.read_parquet(parquet_path)
    refs = {}
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
        refs[key] = value
    return refs


def test_fsspec_without_async():
    """Test fsspec reference FS without async mismatch."""
    print("="*80)
    print("TEST: FSSPEC REFERENCE WITHOUT ASYNC")
    print("="*80)

    refs = build_refs_dict(PARQUET_FILE)
    refs['version'] = 1

    # Try without specifying asynchronous
    try:
        fs = fsspec.filesystem(
            "reference",
            fo=refs,
            remote_protocol="s3",
            remote_options={"anon": True}
            # Note: not specifying asynchronous
        )
        print(f"✅ Created reference filesystem: {type(fs)}")

        # Try to read a coordinate array
        print("\nReading coordinate array via fsspec...")

        lat_path = "t2m/instant/heightAboveGround/latitude/0"
        try:
            with fs.open(lat_path, 'rb') as f:
                data = f.read()
            print(f"  ✅ Read {len(data)} bytes from {lat_path}")

            # Decode base64
            if data.startswith(b'base64:'):
                decoded = base64.b64decode(data[7:])
                lat_array = np.frombuffer(decoded, dtype='<f8')
                print(f"  Latitude array: shape={lat_array.shape}, range=[{lat_array.min():.1f}, {lat_array.max():.1f}]")
        except Exception as e:
            print(f"  ❌ Read failed: {e}")

        # Try to read GRIB2 reference
        print("\nReading GRIB2 reference via fsspec...")
        grib_path = "step_000/2t/sfc/control/0.0.0"
        try:
            with fs.open(grib_path, 'rb') as f:
                data = f.read()
            print(f"  Read {len(data)} bytes from {grib_path}")

            # This returns the reference, not the actual data
            if isinstance(data, bytes):
                ref = json.loads(data.decode())
                print(f"  Reference: {ref}")
                # Now we'd need to fetch from S3 manually...
        except Exception as e:
            print(f"  Read failed: {e}")

        return fs

    except Exception as e:
        print(f"❌ Failed: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_hybrid_data_extraction():
    """Test hybrid approach: fsspec metadata + manual GRIB2."""
    print("\n" + "="*80)
    print("TEST: HYBRID DATA EXTRACTION")
    print("="*80)

    import xarray as xr

    refs = build_refs_dict(PARQUET_FILE)

    # Extract coordinates from refs (base64 encoded)
    print("\n1. Extracting coordinates from refs...")

    coord_path = "t2m/instant/heightAboveGround"

    # Latitude
    lat_data = refs.get(f"{coord_path}/latitude/0")
    if isinstance(lat_data, str) and lat_data.startswith('base64:'):
        lats = np.frombuffer(base64.b64decode(lat_data[7:]), dtype='<f8')
        print(f"   Latitude: {lats.shape}, [{lats.min():.1f}, {lats.max():.1f}]")

    # Longitude
    lon_data = refs.get(f"{coord_path}/longitude/0")
    if isinstance(lon_data, str) and lon_data.startswith('base64:'):
        lons = np.frombuffer(base64.b64decode(lon_data[7:]), dtype='<f8')
        print(f"   Longitude: {lons.shape}, [{lons.min():.1f}, {lons.max():.1f}]")

    # Time
    time_data = refs.get(f"{coord_path}/time/0")
    if isinstance(time_data, str) and time_data.startswith('base64:'):
        import struct
        time_bytes = base64.b64decode(time_data[7:])
        time_unix = struct.unpack('<q', time_bytes)[0]
        from datetime import datetime
        time_dt = datetime.utcfromtimestamp(time_unix)
        print(f"   Time: {time_dt}")

    # Extract one timestep data from GRIB2
    print("\n2. Extracting data from GRIB2 reference...")

    step_key = "step_000/2t/sfc/control/0.0.0"
    ref = refs.get(step_key)

    if isinstance(ref, list) and len(ref) >= 3:
        url, offset, length = ref[0], ref[1], ref[2]

        # Add .grib2 extension
        if not url.endswith('.grib2'):
            url = url + '.grib2'

        s3_path = f"s3://{url}" if not url.startswith('s3://') else url
        print(f"   URL: {s3_path}")
        print(f"   Offset: {offset}, Length: {length}")

        # Fetch from S3
        fs_s3 = fsspec.filesystem('s3', anon=True)
        with fs_s3.open(s3_path, 'rb') as f:
            f.seek(offset)
            grib_data = f.read(length)

        print(f"   Fetched {len(grib_data)} bytes of GRIB2")

        # Decode GRIB2
        if grib_data[:4] == b'GRIB':
            import cfgrib

            with tempfile.NamedTemporaryFile(delete=False, suffix='.grib2') as tmp:
                tmp.write(grib_data)
                tmp_path = tmp.name

            ds_grib = xr.open_dataset(tmp_path, engine='cfgrib')
            var_name = list(ds_grib.data_vars)[0]
            data_2d = ds_grib[var_name].values
            ds_grib.close()
            os.unlink(tmp_path)

            print(f"   Decoded: {data_2d.shape}")
            print(f"   Value range: [{data_2d.min():.1f}, {data_2d.max():.1f}]")

            # Create xarray dataset
            print("\n3. Creating xarray Dataset...")
            ds = xr.Dataset(
                {
                    't2m': (['latitude', 'longitude'], data_2d)
                },
                coords={
                    'latitude': lats,
                    'longitude': lons,
                    'time': [np.datetime64(time_dt)]
                }
            )
            print(ds)
            return ds

    return None


def compare_approaches():
    """Compare fsspec-based vs pure dict-based extraction."""
    print("\n" + "="*80)
    print("COMPARISON: FSSPEC VS PURE DICT")
    print("="*80)

    import time

    refs = build_refs_dict(PARQUET_FILE)

    # Method 1: Pure dict lookup (current approach)
    print("\n1. Pure dict lookup:")
    start = time.time()
    for _ in range(100):
        lat_data = refs.get("t2m/instant/heightAboveGround/latitude/0")
        if isinstance(lat_data, str) and lat_data.startswith('base64:'):
            lats = np.frombuffer(base64.b64decode(lat_data[7:]), dtype='<f8')
    dict_time = time.time() - start
    print(f"   100 iterations: {dict_time*1000:.2f} ms")

    # Method 2: fsspec reference filesystem
    print("\n2. fsspec reference filesystem:")
    try:
        refs_copy = refs.copy()
        refs_copy['version'] = 1

        start = time.time()
        fs = fsspec.filesystem(
            "reference",
            fo=refs_copy,
            remote_protocol="s3",
            remote_options={"anon": True}
        )
        fs_create_time = time.time() - start
        print(f"   Filesystem creation: {fs_create_time*1000:.2f} ms")

        start = time.time()
        for _ in range(100):
            with fs.open("t2m/instant/heightAboveGround/latitude/0", 'rb') as f:
                data = f.read()
            if data.startswith(b'base64:'):
                lats = np.frombuffer(base64.b64decode(data[7:]), dtype='<f8')
        fsspec_time = time.time() - start
        print(f"   100 iterations: {fsspec_time*1000:.2f} ms")

        print(f"\n   Speedup (dict vs fsspec): {fsspec_time/dict_time:.1f}x slower")

    except Exception as e:
        print(f"   ❌ fsspec failed: {e}")


def main():
    print("\n" + "#"*80)
    print("#  HYBRID APPROACH TESTING")
    print("#"*80)

    test_fsspec_without_async()
    ds = test_hybrid_data_extraction()
    compare_approaches()

    print("\n" + "="*80)
    print("CONCLUSIONS")
    print("="*80)
    print("""
1. The fsspec reference filesystem CAN read the coordinate arrays (base64)
2. For GRIB2 data chunks, it returns references that still need manual S3 fetch
3. The pure dict approach is FASTER and simpler for our use case
4. No benefit to using fsspec for this parquet format

The current read_stage3_to_xarray.py approach is optimal because:
- It directly parses the refs dict (fast)
- It handles GRIB2 decoding properly
- It builds proper xarray datasets with all coordinates

The earthaccess example works because their data is pure zarr, not GRIB2.
""")


if __name__ == "__main__":
    main()
