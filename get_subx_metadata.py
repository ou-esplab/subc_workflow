import sys
import xarray as xr

def main(dataset_url):
    try:
        ds = xr.open_dataset(dataset_url, decode_times=True)
    except Exception as e:
        print(f"Failed to open dataset URL: {dataset_url}")
        print(e)
        sys.exit(1)

    dims = ds.dims
    coords = ds.coords

    # Print dimension sizes needed for size estimate
    L = dims.get('L', 1)
    M = dims.get('M', 1)
    P = dims.get('P', 1)
    S = dims.get('S', 1)
    X = dims.get('X', 1)
    Y = dims.get('Y', 1)

    print(f"DIMS {L} {M} {P} {S} {X} {Y}")

    # Extract start times as YYYYMMDD strings
    if 'S' in coords:
        times = ds['S'].values
        try:
            import numpy as np
            dates = [
                np.datetime_as_string(t, unit='h')
                  .replace('-', '')
                  .replace(':', '')
                  .replace('T', '')
                for t in times
            ]
        except Exception:
            dates = [
                 str(t)[:13]
                 .replace('-', '')
                 .replace(':', '')
                 .replace('T', '')
                for t in times
            ]
    else:
        dates = []
    for date in dates:
        print(f"DATE {date}")

    ds.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python get_subx_metadata.py <dataset_url>")
        sys.exit(1)
    main(sys.argv[1])

