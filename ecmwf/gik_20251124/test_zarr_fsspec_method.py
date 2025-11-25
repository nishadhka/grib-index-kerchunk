#!/usr/bin/env python3
"""
Test Zarr V3 + fsspec Reference Filesystem Method

This script tests the alternative method for reading ECMWF Stage 3 parquet files
using Zarr V3 with fsspec's reference filesystem, similar to the earthaccess example:

    fs = fsspec.filesystem("reference",
                           fo=refs,
                           remote_protocol="https",
                           ...)
    store = zarr.storage.FsspecStore(fs, read_only=True)
    ds = xr.open_zarr(store, consolidated=False)

Key differences from current approach (read_stage3_to_xarray.py):
- Current: Manual parquet reading -> S3 byte fetching -> GRIB2 decode -> numpy
- This method: fsspec reference FS -> Zarr store -> xarray zarr engine

"""

import fsspec
import zarr
import xarray as xr
import pandas as pd
import os
import sys
import json

# Set up anonymous S3 access
os.environ['AWS_NO_SIGN_REQUEST'] = 'YES'

# Input parquet file
PARQUET_FILE = "/scratch/notebook/test_ecmwf_three_stage_prebuilt_output/stage3_control_final.parquet"


def print_section(title):
    """Print section header."""
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}\n")


def inspect_parquet_structure(parquet_path):
    """Inspect the parquet file structure to understand the format."""
    print_section("1. INSPECTING PARQUET STRUCTURE")

    print(f"Reading parquet: {parquet_path}")
    df = pd.read_parquet(parquet_path)

    print(f"\nDataFrame shape: {df.shape}")
    print(f"Columns: {list(df.columns)}")
    print(f"\nFirst 10 rows:")
    print(df.head(10))

    print(f"\nSample keys:")
    for i, key in enumerate(df['key'].head(20)):
        print(f"  {i}: {key}")

    # Check value types
    print(f"\nValue types distribution:")
    def get_value_type(val):
        if isinstance(val, bytes):
            val = val.decode('utf-8')
        if isinstance(val, str):
            if val.startswith('base64:'):
                return 'base64'
            elif val.startswith('['):
                return 'list (S3 ref)'
            elif val.startswith('{'):
                return 'dict'
            else:
                return 'string'
        return type(val).__name__

    value_types = df['value'].apply(get_value_type).value_counts()
    print(value_types)

    # Check for .zarray and .zattrs keys
    print(f"\nMetadata keys (.zarray, .zattrs, .zgroup):")
    metadata_keys = df[df['key'].str.contains(r'\.(zarray|zattrs|zgroup)$', regex=True)]['key']
    for key in metadata_keys.head(20):
        print(f"  {key}")

    return df


def test_fsspec_reference_method(parquet_path):
    """Test fsspec reference filesystem method with parquet."""
    print_section("2. TESTING FSSPEC REFERENCE FILESYSTEM METHOD")

    print("Method 1: Direct parquet path to fsspec reference")
    print("-" * 60)

    try:
        # The earthaccess example uses remote URL, we have local file
        # fsspec reference filesystem needs a "references" dict or compatible format

        print("Attempting: fsspec.filesystem('reference', fo=parquet_path, ...)")

        # Try with local parquet path
        fs = fsspec.filesystem(
            "reference",
            fo=parquet_path,
            remote_protocol="s3",
            remote_options={"anon": True}
        )

        print(f"  ✅ Created reference filesystem")
        print(f"  Type: {type(fs)}")

        # Try to list contents
        print(f"\n  Listing root directory:")
        try:
            contents = fs.ls('')
            for item in contents[:20]:
                print(f"    {item}")
        except Exception as e:
            print(f"    ⚠️ ls() failed: {e}")

        # Try zarr store
        print(f"\n  Creating Zarr store...")
        store = zarr.storage.FsspecStore(fs, read_only=True)
        print(f"  Store type: {type(store)}")

        # Try xarray
        print(f"\n  Opening with xarray.open_zarr()...")
        ds = xr.open_zarr(store, consolidated=False)

        print(f"\n  ✅ SUCCESS!")
        print(f"\n  Dataset:")
        print(ds)

        return ds

    except Exception as e:
        print(f"\n  ❌ Method 1 failed: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_manual_refs_method(parquet_path):
    """Test building references dict manually from parquet."""
    print_section("3. TESTING MANUAL REFS DICT METHOD")

    print("Building references dict from parquet...")
    print("-" * 60)

    try:
        # Read parquet
        df = pd.read_parquet(parquet_path)

        # Build refs dict in the format expected by fsspec reference FS
        # Format: {"key": ["url", offset, length]} or {"key": "base64:..."}
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

        # Check if we have the expected format
        print(f"Built refs dict with {len(refs)} keys")

        # Show sample refs
        print(f"\nSample refs:")
        for i, (key, val) in enumerate(list(refs.items())[:10]):
            val_str = str(val)[:80] + "..." if len(str(val)) > 80 else str(val)
            print(f"  {key}: {val_str}")

        # Add version key if missing (required by some fsspec versions)
        if 'version' not in refs:
            refs['version'] = 1

        print(f"\n  Creating fsspec reference filesystem from refs dict...")
        fs = fsspec.filesystem(
            "reference",
            fo=refs,
            remote_protocol="s3",
            remote_options={"anon": True}
        )

        print(f"  ✅ Created reference filesystem")

        # List contents
        print(f"\n  Listing root directory:")
        try:
            contents = fs.ls('')
            for item in contents[:20]:
                print(f"    {item}")
        except Exception as e:
            print(f"    ⚠️ ls() failed: {e}")

        # Try zarr
        print(f"\n  Creating Zarr store...")

        # Check zarr version
        print(f"  Zarr version: {zarr.__version__}")

        store = zarr.storage.FsspecStore(fs, read_only=True)
        print(f"  Store type: {type(store)}")

        # Try xarray
        print(f"\n  Opening with xarray.open_zarr()...")
        ds = xr.open_zarr(store, consolidated=False)

        print(f"\n  ✅ SUCCESS!")
        print(f"\n  Dataset:")
        print(ds)

        return ds

    except Exception as e:
        print(f"\n  ❌ Method 2 failed: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_zarr_group_direct(parquet_path):
    """Test opening zarr group directly."""
    print_section("4. TESTING ZARR GROUP DIRECT")

    print("Opening zarr group directly from refs...")
    print("-" * 60)

    try:
        # Read parquet
        df = pd.read_parquet(parquet_path)

        # Build refs dict
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

        if 'version' not in refs:
            refs['version'] = 1

        # Create fsspec mapper
        print("Creating fsspec.filesystem('reference', fo=refs, ...)")
        fs = fsspec.filesystem(
            "reference",
            fo=refs,
            remote_protocol="s3",
            remote_options={"anon": True}
        )

        # Get mapper
        mapper = fs.get_mapper("")
        print(f"  Mapper type: {type(mapper)}")

        # Open zarr group
        print(f"\n  Opening zarr.open_group()...")
        zgroup = zarr.open_group(mapper, mode='r')

        print(f"  ✅ Opened zarr group!")
        print(f"  Group keys: {list(zgroup.keys())[:20]}")

        # Explore structure
        print(f"\n  Exploring group structure:")
        def explore_group(g, prefix="", depth=0, max_depth=3):
            if depth > max_depth:
                return
            try:
                for key in list(g.keys())[:5]:
                    item = g[key]
                    if hasattr(item, 'keys'):
                        print(f"  {'  '*depth}{prefix}{key}/ (group)")
                        explore_group(item, f"{prefix}{key}/", depth+1, max_depth)
                    else:
                        print(f"  {'  '*depth}{prefix}{key} (array, shape={item.shape})")
            except Exception as e:
                print(f"  {'  '*depth}{prefix}[error: {e}]")

        explore_group(zgroup)

        return zgroup

    except Exception as e:
        print(f"\n  ❌ Method 3 failed: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_xarray_open_datatree(parquet_path):
    """Test xarray.open_datatree() method."""
    print_section("5. TESTING XARRAY.OPEN_DATATREE()")

    print("Trying xarray.open_datatree() with reference FS...")
    print("-" * 60)

    try:
        # Read parquet
        df = pd.read_parquet(parquet_path)

        # Build refs dict
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

        if 'version' not in refs:
            refs['version'] = 1

        # Create fsspec mapper
        fs = fsspec.filesystem(
            "reference",
            fo=refs,
            remote_protocol="s3",
            remote_options={"anon": True}
        )
        mapper = fs.get_mapper("")

        print(f"  Trying xr.open_datatree()...")

        # xarray.open_datatree requires xarray 2024.x or xarray-datatree
        try:
            dt = xr.open_datatree(mapper, engine='zarr', consolidated=False)
            print(f"\n  ✅ SUCCESS with open_datatree!")
            print(f"\n  DataTree:")
            print(dt)
            return dt
        except AttributeError:
            print(f"  xr.open_datatree not available (xarray version: {xr.__version__})")

            # Try xarray-datatree package
            try:
                from datatree import open_datatree
                dt = open_datatree(mapper, engine='zarr', consolidated=False)
                print(f"\n  ✅ SUCCESS with datatree.open_datatree!")
                print(f"\n  DataTree:")
                print(dt)
                return dt
            except ImportError:
                print(f"  xarray-datatree package not installed")
                return None

    except Exception as e:
        print(f"\n  ❌ Method 4 failed: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return None


def main():
    """Main function."""
    print("\n" + "="*80)
    print("  ZARR V3 + FSSPEC REFERENCE FILESYSTEM TEST")
    print("  Testing alternative method for reading ECMWF Stage 3 parquet")
    print("="*80)

    if not os.path.exists(PARQUET_FILE):
        print(f"\n❌ Parquet file not found: {PARQUET_FILE}")
        return

    print(f"\nInput: {PARQUET_FILE}")
    print(f"Zarr version: {zarr.__version__}")
    print(f"Xarray version: {xr.__version__}")
    print(f"Fsspec version: {fsspec.__version__}")

    # Run tests
    df = inspect_parquet_structure(PARQUET_FILE)

    result1 = test_fsspec_reference_method(PARQUET_FILE)
    result2 = test_manual_refs_method(PARQUET_FILE)
    result3 = test_zarr_group_direct(PARQUET_FILE)
    result4 = test_xarray_open_datatree(PARQUET_FILE)

    # Summary
    print_section("SUMMARY")
    print(f"Method 1 (Direct parquet to fsspec reference): {'✅ SUCCESS' if result1 else '❌ FAILED'}")
    print(f"Method 2 (Manual refs dict): {'✅ SUCCESS' if result2 else '❌ FAILED'}")
    print(f"Method 3 (Zarr group direct): {'✅ SUCCESS' if result3 else '❌ FAILED'}")
    print(f"Method 4 (xarray.open_datatree): {'✅ SUCCESS' if result4 else '❌ FAILED'}")

    if any([result1, result2, result3, result4]):
        print("\n🎉 At least one method succeeded!")
    else:
        print("\n⚠️ All methods failed. See errors above.")
        print("\nPossible reasons:")
        print("  - Parquet format may not be compatible with fsspec reference FS")
        print("  - May need kerchunk format conversion")
        print("  - GRIB2 references in parquet require special handling")


if __name__ == "__main__":
    main()
