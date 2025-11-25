#!/usr/bin/env python3
"""
Test Zarr V2 Compatibility with fsspec Reference Filesystem

The previous test revealed:
1. Parquet contains Zarr V2 metadata (zarr_format: 2)
2. We're using Zarr V3 library (3.1.5)
3. xarray.open_zarr() with Zarr V3 expects V3 metadata format

This script tests approaches to handle this compatibility issue.
"""

import fsspec
import zarr
import xarray as xr
import pandas as pd
import numpy as np
import os
import json

os.environ['AWS_NO_SIGN_REQUEST'] = 'YES'

PARQUET_FILE = "/scratch/notebook/test_ecmwf_three_stage_prebuilt_output/stage3_control_final.parquet"


def print_section(title):
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}\n")


def build_refs_dict(parquet_path):
    """Build references dict from parquet, ensuring proper format."""
    df = pd.read_parquet(parquet_path)

    refs = {}
    for _, row in df.iterrows():
        key = row['key']
        value = row['value']

        if isinstance(value, bytes):
            value = value.decode('utf-8')

        # Parse JSON values
        if isinstance(value, str) and (value.startswith('[') or value.startswith('{')):
            try:
                value = json.loads(value)
            except:
                pass

        refs[key] = value

    return refs


def test_zarr_format_check(refs):
    """Check the zarr format in the references."""
    print_section("1. ZARR FORMAT CHECK")

    # Check .zgroup at root
    if '.zgroup' in refs:
        zgroup = refs['.zgroup']
        print(f"Root .zgroup: {zgroup}")
        if isinstance(zgroup, dict):
            zarr_format = zgroup.get('zarr_format', 'unknown')
            print(f"Zarr format version: {zarr_format}")

    # Check sample .zarray
    for key in refs:
        if key.endswith('.zarray'):
            print(f"\nSample .zarray ({key}):")
            print(f"  {refs[key]}")
            break


def test_zarr2_api(refs):
    """Test using zarr with zarr_format=2 parameter."""
    print_section("2. TESTING ZARR V2 COMPATIBILITY MODE")

    try:
        print("Creating fsspec reference filesystem...")
        fs = fsspec.filesystem(
            "reference",
            fo=refs,
            remote_protocol="s3",
            remote_options={"anon": True},
            asynchronous=True  # Add async flag
        )

        store = zarr.storage.FsspecStore(fs, read_only=True)
        print(f"Store created: {type(store)}")

        # Try open_group with zarr_format specified
        print("\nTrying zarr.open_group with zarr_format=2...")
        try:
            zgroup = zarr.open_group(store, mode='r', zarr_format=2)
            print(f"  ✅ SUCCESS! Group opened")
            print(f"  Keys: {list(zgroup.keys())[:10]}")
            return zgroup
        except Exception as e:
            print(f"  ❌ Failed: {e}")

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
    return None


def test_kerchunk_combine_refs():
    """Test using kerchunk to handle the reference file."""
    print_section("3. TESTING KERCHUNK APPROACH")

    try:
        from kerchunk.df import refs_to_dataframe
        from kerchunk.combine import MultiZarrToZarr

        print("Kerchunk available!")

        # Read parquet
        df = pd.read_parquet(PARQUET_FILE)
        print(f"Parquet shape: {df.shape}")

        # Convert to refs format expected by kerchunk
        refs = build_refs_dict(PARQUET_FILE)

        # Add version if missing
        if 'version' not in refs:
            refs['version'] = 1

        # kerchunk uses "refs" key for nested references
        full_refs = {
            'version': 1,
            'refs': refs
        }

        print("\nCreating fsspec filesystem with kerchunk format...")
        fs = fsspec.filesystem(
            "reference",
            fo=full_refs,
            remote_protocol="s3",
            remote_options={"anon": True}
        )

        print("Listing root...")
        try:
            contents = fs.ls('')
            for item in contents[:10]:
                print(f"  {item}")
        except Exception as e:
            print(f"  ls failed: {e}")

        return fs

    except ImportError:
        print("kerchunk not installed, skipping")
        return None
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_xarray_zarr2_engine():
    """Test xarray with explicit zarr_format=2."""
    print_section("4. TESTING XARRAY WITH ZARR_FORMAT=2")

    try:
        refs = build_refs_dict(PARQUET_FILE)

        # Ensure proper version
        if 'version' not in refs:
            refs['version'] = 1

        print("Creating fsspec reference filesystem...")
        fs = fsspec.filesystem(
            "reference",
            fo=refs,
            remote_protocol="s3",
            remote_options={"anon": True},
            asynchronous=True
        )

        store = zarr.storage.FsspecStore(fs, read_only=True)

        print("\nTrying xr.open_zarr with zarr_format=2...")
        try:
            ds = xr.open_zarr(store, consolidated=False, zarr_format=2)
            print(f"  ✅ SUCCESS!")
            print(ds)
            return ds
        except TypeError as e:
            print(f"  zarr_format parameter not supported: {e}")
        except Exception as e:
            print(f"  ❌ Failed: {e}")

        # Try with open_dataset and engine kwargs
        print("\nTrying xr.open_dataset with engine='zarr' and kwargs...")
        try:
            mapper = fs.get_mapper("")
            ds = xr.open_dataset(
                mapper,
                engine='zarr',
                consolidated=False,
                backend_kwargs={'zarr_format': 2}
            )
            print(f"  ✅ SUCCESS!")
            print(ds)
            return ds
        except Exception as e:
            print(f"  ❌ Failed: {e}")

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
    return None


def test_direct_group_exploration():
    """Directly explore the zarr structure from refs."""
    print_section("5. DIRECT STRUCTURE EXPLORATION")

    refs = build_refs_dict(PARQUET_FILE)

    print("Top-level groups in refs:")
    top_groups = set()
    for key in refs:
        if '/' in key:
            top = key.split('/')[0]
            top_groups.add(top)
        elif key.startswith('.'):
            top_groups.add(key)

    for g in sorted(top_groups):
        print(f"  {g}")

    # Find data variables (arrays at various paths)
    print("\nData arrays (keys ending with /0 or similar):")
    data_keys = [k for k in refs if k.endswith('/0') or k.endswith('/0.0') or k.endswith('/0.0.0')]
    for k in data_keys[:20]:
        val = refs[k]
        val_type = 'base64' if isinstance(val, str) and val.startswith('base64:') else \
                   'S3 ref' if isinstance(val, list) else type(val).__name__
        print(f"  {k}: {val_type}")


def test_reading_single_array_direct():
    """Test reading a single array directly from refs + S3."""
    print_section("6. DIRECT ARRAY READING (NO ZARR)")

    refs = build_refs_dict(PARQUET_FILE)

    # Find a coordinate array (small, base64 encoded)
    print("Reading coordinate array from refs...")

    # Find latitude array
    lat_key = None
    for k in refs:
        if 'latitude/0' in k and k.endswith('/0'):
            lat_key = k
            break

    if lat_key:
        print(f"\nFound latitude key: {lat_key}")

        # Get zarray metadata
        zarray_key = lat_key.replace('/0', '/.zarray')
        if zarray_key in refs:
            print(f"  .zarray: {refs[zarray_key]}")

        lat_val = refs[lat_key]
        if isinstance(lat_val, str) and lat_val.startswith('base64:'):
            import base64
            decoded = base64.b64decode(lat_val[7:])
            print(f"  Base64 decoded length: {len(decoded)} bytes")

            # Parse based on dtype from zarray
            zarray = refs.get(zarray_key, {})
            dtype = zarray.get('dtype', '<f8')
            print(f"  Dtype: {dtype}")

            lat_array = np.frombuffer(decoded, dtype=dtype)
            print(f"  Latitude array shape: {lat_array.shape}")
            print(f"  Latitude range: {lat_array.min():.2f} to {lat_array.max():.2f}")

    # Find an S3-referenced data chunk
    print("\nReading S3-referenced data chunk...")
    s3_key = None
    for k in refs:
        if k.startswith('step_000/') and k.endswith('/0.0.0'):
            s3_key = k
            break

    if s3_key:
        print(f"\nFound step_000 key: {s3_key}")
        s3_ref = refs[s3_key]
        print(f"  Reference: {s3_ref}")

        if isinstance(s3_ref, list) and len(s3_ref) >= 3:
            url, offset, length = s3_ref[0], s3_ref[1], s3_ref[2]
            print(f"  URL: {url}")
            print(f"  Offset: {offset}, Length: {length}")

            # Fetch byte range
            print(f"\n  Fetching byte range from S3...")
            try:
                if not url.endswith('.grib2'):
                    url = url + '.grib2'

                s3_path = f"s3://{url}" if not url.startswith('s3://') else url
                fs_s3 = fsspec.filesystem('s3', anon=True)

                with fs_s3.open(s3_path, 'rb') as f:
                    f.seek(offset)
                    data = f.read(length)

                print(f"  ✅ Fetched {len(data)} bytes")
                print(f"  First 4 bytes: {data[:4]}")

                if data[:4] == b'GRIB':
                    print(f"  Confirmed: GRIB2 data")

            except Exception as e:
                print(f"  ❌ S3 fetch failed: {e}")


def test_sub_groups_separately():
    """Test opening sub-groups separately."""
    print_section("7. TESTING SUB-GROUPS SEPARATELY")

    refs = build_refs_dict(PARQUET_FILE)

    # The structure has groups like:
    # - str/accum/surface/
    # - t2m/instant/heightAboveGround/
    # - step_XXX/variable/level/member/

    # Let's try to open a specific sub-group
    target_group = "t2m/instant/heightAboveGround"

    print(f"Trying to isolate sub-group: {target_group}")

    # Filter refs for this sub-group
    sub_refs = {}
    prefix = target_group + "/"

    for key, val in refs.items():
        if key.startswith(prefix):
            # Rebase key to root
            new_key = key[len(prefix):]
            sub_refs[new_key] = val
        elif key == target_group + "/.zgroup":
            sub_refs[".zgroup"] = val
        elif key == target_group + "/.zattrs":
            sub_refs[".zattrs"] = val

    # Add root .zgroup if not present
    if ".zgroup" not in sub_refs:
        sub_refs[".zgroup"] = {"zarr_format": 2}

    sub_refs["version"] = 1

    print(f"Sub-group refs count: {len(sub_refs)}")
    print(f"Sample keys: {list(sub_refs.keys())[:10]}")

    # Try opening this sub-group
    try:
        fs = fsspec.filesystem(
            "reference",
            fo=sub_refs,
            remote_protocol="s3",
            remote_options={"anon": True},
            asynchronous=True
        )

        store = zarr.storage.FsspecStore(fs, read_only=True)

        print("\nTrying xr.open_zarr on sub-group...")
        ds = xr.open_zarr(store, consolidated=False, zarr_format=2)
        print(f"  ✅ SUCCESS!")
        print(ds)
        return ds

    except Exception as e:
        print(f"  ❌ Failed: {e}")
        import traceback
        traceback.print_exc()

    return None


def main():
    print("\n" + "="*80)
    print("  ZARR V2/V3 COMPATIBILITY TEST")
    print("="*80)

    print(f"\nZarr version: {zarr.__version__}")
    print(f"Xarray version: {xr.__version__}")
    print(f"Fsspec version: {fsspec.__version__}")

    refs = build_refs_dict(PARQUET_FILE)

    test_zarr_format_check(refs)
    test_zarr2_api(refs)
    test_kerchunk_combine_refs()
    test_xarray_zarr2_engine()
    test_direct_group_exploration()
    test_reading_single_array_direct()
    test_sub_groups_separately()

    print_section("CONCLUSION")
    print("""
The ECMWF Stage 3 parquet files contain Zarr V2 format metadata but:
1. The current zarr library (3.1.5) is V3 and has limited V2 compatibility
2. The references point to GRIB2 chunks, not raw zarr chunks
3. xarray.open_zarr() expects pure zarr data, not GRIB2

The current approach in read_stage3_to_xarray.py is correct:
- It manually processes the references
- It decodes GRIB2 chunks using cfgrib
- It assembles the data into numpy/xarray manually

The fsspec+zarr approach shown in the earthaccess example works because:
- Those parquet files contain pure zarr chunk references (not GRIB2)
- They use compatible zarr formats

For ECMWF GRIB2-based parquets, the manual approach is necessary.
""")


if __name__ == "__main__":
    main()
