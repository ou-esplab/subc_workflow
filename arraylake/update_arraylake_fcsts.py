"""
This script updates the Arraylake forecast repository with new forecast data files weekly.
The script checks for new files based on the latest start time in the current repository
and then reads, processes, and appends any new data while ensuring data integrity for each model.
"""

# Import needed packages
import argparse
from pathlib import Path
from datetime import datetime
import numpy as np
import xarray as xr
import pandas as pd
import os
import time
import traceback
import glob
from arraylake import Client
import zarr
import logging
import yaml
import dask

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable

# Supress FutureWarnings from xarray and zarr for cleaner logs
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
# Prevent dask from materializing oversized arrays during slicing operations.
dask.config.set({"array.slicing.split_large_chunks": True})

def _load_token_from_env_file(env_file: Path) -> str | None:
    if not env_file.exists():
        return None
    with open(env_file, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            if key.strip() == 'ARRAYLAKE_TOKEN':
                return value.strip().strip('"').strip("'")
    return None


def _build_runtime_config() -> dict:
    parser = argparse.ArgumentParser(description='Update ArrayLake SubX forecasts')
    parser.add_argument('--config', required=True, help='Path to workflow config.yaml')
    parser.add_argument('--date', default=None, help='Optional target init date YYYYMMDD')
    parser.add_argument('--dry-run', action='store_true', help='Scan and report pending updates without writing')
    args = parser.parse_args()

    with open(args.config, encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}

    al_cfg = cfg.get('arraylake') or {}
    models = cfg.get('models') or []
    if not models:
        raise RuntimeError('No models found in config.yaml under models')

    token = os.environ.get('ARRAYLAKE_TOKEN')
    if not token:
        token = _load_token_from_env_file(Path(__file__).parent / '.env')
    if not token:
        raise RuntimeError('ARRAYLAKE_TOKEN not found in environment or arraylake/.env')

    date_arg = args.date.strip() if isinstance(args.date, str) and args.date.strip() else None
    if date_arg is not None:
        try:
            datetime.strptime(date_arg, '%Y%m%d')
        except ValueError as e:
            raise RuntimeError(f'Invalid --date value {date_arg}; expected YYYYMMDD') from e

    runtime = {
        'token': token,
        'org_name': al_cfg.get('repo', 'ou-subc/subc-forecasts'),
        'input_path': al_cfg.get('input_root') or ((cfg.get('paths') or {}).get('rt_root')),
        'datatype': al_cfg.get('datatype', 'forecast'),
        'branch_name': al_cfg.get('branch', 'main'),
        'variables': al_cfg.get('variables') or ['pr', 'tas', 'rlut', 'ts', 'ua', 'va', 'zg'],
        'model_vars': al_cfg.get('model_vars') or {},
        'skip_models': al_cfg.get('skip_models') or [],
        'model_name_map': cfg.get('model_name_map') or {},
        'models': models,
        'date_filter': date_arg,
        'dry_run': bool(args.dry_run or os.environ.get('ARRAYLAKE_DRY_RUN') == '1'),
        'config_path': os.path.abspath(args.config),
    }
    if not runtime['input_path']:
        raise RuntimeError('arraylake.input_root or paths.rt_root must be set in config.yaml')
    return runtime


runtime = _build_runtime_config()

### Set up the logger
# The workflow runner captures stdout/stderr into stage logs, so stream logging keeps
# ArrayLake logs consistent with all other workflow stages.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
# Create a logger instance
logger = logging.getLogger(__name__)

# Log the start of the process with a clear header and timestamp
logger.info("="*70)
logger.info(f"Starting forecast data append at {datetime.now()}")
logger.info("="*70)

### Configuration for the script
token = runtime['token']
org_name = runtime['org_name']
input_path = str(runtime['input_path']).rstrip('/')
datatype = runtime['datatype']
branch_name = runtime['branch_name']
target_date = runtime['date_filter']
dry_run = runtime['dry_run']
cfg_variables = list(runtime['variables'])
allowed_variables = set(cfg_variables)
arraylake_model_vars = runtime.get('model_vars') or {}
skip_models = set(runtime.get('skip_models') or [])
model_name_map = runtime.get('model_name_map') or {}

# Build model list from workflow config so this stage stays in sync with config.yaml.
models_to_process = []
for model in runtime['models']:
    group = model.get('group')
    name = model.get('name')
    if not group or not name:
        logger.warning(f"Skipping model entry missing group/name: {model}")
        continue

    model_label = f"{group}-{name}"

    if model_label in skip_models:
        logger.info(f"Skipping {model_label}: listed in arraylake.skip_models")
        continue

    raw_model_vars = arraylake_model_vars.get(model_label, arraylake_model_vars.get(name, model.get('vars')))

    if raw_model_vars is None:
        effective_vars = list(cfg_variables)
    else:
        if isinstance(raw_model_vars, str):
            raw_model_vars = [raw_model_vars]
        elif not isinstance(raw_model_vars, list):
            logger.warning(
                f"Skipping vars override for {model_label}: expected list or string, got {type(raw_model_vars).__name__}; "
                "using arraylake.variables"
            )
            raw_model_vars = list(cfg_variables)

        invalid_vars = [v for v in raw_model_vars if v not in allowed_variables]
        if invalid_vars:
            logger.warning(
                f"Model {model_label} has unknown vars {invalid_vars}; allowed vars are {sorted(allowed_variables)}; "
                "skipping unknown vars"
            )

        effective_vars = [v for v in raw_model_vars if v in allowed_variables]

        if not effective_vars:
            logger.warning(
                f"Skipping model {model_label}: no valid vars remain after filtering. "
                "Set models[].vars to valid entries or remove models[].vars to use arraylake.variables defaults."
            )
            continue

    models_to_process.append({'model': name, 'group': group, 'variables': effective_vars})

if models_to_process:
    logger.info("Effective ArrayLake model-variable mapping:")
    for model_cfg in models_to_process:
        logger.info(f"  - {model_cfg['group']}-{model_cfg['model']}: {model_cfg['variables']}")
else:
    raise RuntimeError(
        "No valid models to process after applying models[].vars and arraylake.variables filtering"
    )

logger.info(f"Config: {runtime['config_path']}")
if target_date:
    logger.info(f"Date filter enabled: {target_date}")
if dry_run:
    logger.info("Dry-run enabled: no ArrayLake writes or commits will be performed")

### Instantiate and authenticate the Arraylake client
# Try to,
try:
    # Initialize the client with the provided token and log success
    logger.info("Initializing Arraylake client...")
    client = Client(token=token)
    logger.info("✓ Client initialized successfully")
# If any exception occurs during client initialization,
except Exception as e:
    # Log the error and exit
    logger.error(f"✗ Failed to initialize client: {str(e)}")
    logger.error(traceback.format_exc())
    exit(1)

### Process each model and append new data to the repository
# Create a list to store results for each model for the final summary
model_results = []

### For each subC model in the models to process
for subx_model in models_to_process:
    # Try to
    try:
        # Log the start of processing for this model with clear formatting
        logger.info("")
        logger.info("-"*70)
        logger.info(f"Processing model: {subx_model['model']} ({subx_model['group']})")
        logger.info("-"*70)
        
        # Get this group and model (group-model is how a model is identified)
        this_model = subx_model['model']
        this_group = subx_model['group']
        # Resolve the local directory name (may differ from the server model name via model_name_map)
        this_local_model = model_name_map.get(f"{this_group}-{this_model}", this_model)
        
        ### Open the repository and check the state of the group for this model
        logger.info("STEP 1: Opening repository and checking group state...")
        
        # Construct the group name based on the group, model, and datatype
        group_name = f"{this_group.lower()}-{this_model.lower()}-{datatype}"
        # Log the group name being processed
        logger.info(f"Group name: {group_name}")
        
        # Try to,
        try:
            # Open the repository and log success
            repo = client.get_repo(org_name)
            logger.info(f"✓ Opened repository: {org_name}")
        # If any exception occurs while opening the repository,
        except Exception as e:
            # Log the error and skip to the next model
            logger.error(f"✗ Failed to open repository: {str(e)}")
            model_results.append((this_model, False, f"Failed to open repo: {str(e)}"))
            continue
        
        # Try to, 
        try:
            # List the already existing repository branches and log them
            branches = repo.list_branches()
            logger.info(f"Existing branches: {branches}")
            
            if dry_run:
                if branch_name not in branches:
                    logger.info(f"[DRY RUN] Would create branch '{branch_name}' from main")
                else:
                    logger.info(f"[DRY RUN] Would reuse existing branch '{branch_name}'")
            # If the branch for this datatype does not exist
            elif branch_name not in branches:
                # Create the branch from main and log success
                logger.info(f"Creating branch '{branch_name}'...")
                main_session = repo.readonly_session("main")
                snapshot_id = main_session.snapshot_id
                repo.create_branch(branch_name, snapshot_id)
                logger.info(f"✓ Branch created successfully")
            # If the branch already exists
            else:
                # Log that the branch already exists and will be used for this update
                logger.info(f"✓ Branch '{branch_name}' already exists")
        # If any exception occurs during branch creation,
        except Exception as e:
            # Log the error and skip to the next model
            logger.error(f"✗ Branch creation failed: {str(e)}")
            model_results.append((this_model, False, f"Branch error: {str(e)}"))
            continue
        
        # Check if the group for this model already exists
        logger.info(f"Checking if group '{group_name}' exists...")
        
        # Try to, 
        try:
            # Create a read only session to read the data in isolation without impacting the data on the repository
            read_session = repo.readonly_session('main')
            read_store = read_session.store
            
            # Try to,
            try:
                # Open the existing dataset for this group and log its details
                existing_ds = xr.open_zarr(read_store, group=group_name, consolidated=False)
                logger.info(f"✓ Group exists in repository")
                logger.info(f"  Dataset:\n{existing_ds}")
                
                # Set flag that the group exists
                group_exists = True
                
                # Extract the existing variables and start times from the dataset and log them
                existing_vars = list(existing_ds.data_vars)
                existing_times = pd.Index(existing_ds['S'].values)
                logger.info(f"  Existing variables: {existing_vars}")
                logger.info(f"  Existing start times: {len(existing_times)}")
                
            # If the group does not exist,
            except FileNotFoundError:
                # Log that the group does not exist and will be created, and set flag that the group does not exist
                logger.info(f"✓ Group does not exist (will create new)")
                group_exists = False
                existing_vars = []
                existing_times = pd.Index([])
                
        # If any unexpected exception occurs during the group check,
        except Exception as e:
            # Log the error and skip to the next model
            logger.error(f"✗ Unexpected error checking group: {str(e)}")
            logger.error(traceback.format_exc())
            model_results.append((this_model, False, f"Group check error: {str(e)}"))
            continue
        
        ### Determine the file filtering strategy based on the existing data in the repository
        # Log that the file filtering strategy is being determined
        logger.info("")
        logger.info("STEP 2: Determining file filtering strategy...")
        
        logger.info(f"DEBUG: group_exists={group_exists}, subx_model['variables']={subx_model['variables']}, existing_vars={existing_vars}")
        logger.info(f"DEBUG: Check result: {group_exists and set(subx_model['variables']).issubset(set(existing_vars))}")

        # If the group exists and the defined variables are the same as the existing variables
        if group_exists and set(subx_model['variables']).issubset(set(existing_vars)):
            # Get the most recently added start time in the repository
            max_existing_time = existing_times.max()
            # Convert the most recent start time to a string for filtering
            max_date_str = pd.Timestamp(max_existing_time).strftime('%Y%m%d')
            read_all_files = False
            # Log that all variables exist and the files will be filtered based on the most recent start time
            logger.info(f"✓ Section 3 scenario: All variables exist")
            logger.info(f"  Reading files with dates > {max_date_str}")
        # If the group exists but some of the defined variables are missing, or if the group doesn't
        # exist, all files will be read and other scenarios play out as expected
        else:
            max_date_str = None
            read_all_files = True
            # Log that some or all variables are missing and all files will be read
            logger.info(f"✓ Section 1/2 scenario: Reading all files")
        
        ### Read in and process the data files for each variable
        # Log that the data files are being read and processed
        logger.info("")
        logger.info("STEP 3: Reading and processing data files...")
        
        # Start reading in the SubC data by creating an empty dataset list to append to
        ds_list = []
        # Track which start times each variable actually has a source file for, so we
        # can later restrict writes to times where every configured variable has data.
        # Appending a start time for only some variables leaves the zarr group's arrays
        # with mismatched S-dimension lengths, which permanently breaks all future reads
        # of the group (see arraylake data-integrity incident, 2026-07-02).
        var_time_sets = {}

        # Initialize counters for file processing summary
        total_files_found = 0
        total_files_read = 0
        total_files_skipped = 0
        
        # For each variable in the defined variable list for this model
        for variable in subx_model['variables']:
            # Log the variable being processed
            logger.info(f"")
            logger.info(f"Processing variable: {variable}")
            
            # # Define the path to the variable dataset of interest on qlcs and log it
            url = f"{input_path}/{this_group}-{this_local_model}/{datatype}/{variable}/{variable}_{this_group}-{this_local_model}_????????.daily.nc"
            logger.info(f"  Search path: {url}")
            
            # Try to, 
            try:
                # Get all the sorted files from the defined dataset path
                files = sorted(glob.glob(url))
                # Log the number of files found before filtering and update the total files found counter
                logger.info(f"  Found {len(files)} total files")
                total_files_found += len(files)
                
                # Filter the files to only read in files with new start times if all variables exist in the repository and
                # only new times are being appended, or read all files if new variables are being added or if the group doesn't exist
                if max_date_str is not None:
                    files = [f for f in files if f.rsplit('_', 1)[1].split('.')[0] > max_date_str]

                # Log the number of files remaining after filtering
                logger.info(f"  After filtering: {len(files)} new files")

                # When backfilling missing variables (read_all_files=True), only load files for dates
                # that already exist in the repository, to avoid loading unnecessary old data that won't be written.
                # This drastically reduces memory usage while still providing the backfill data needed.
                if read_all_files and group_exists:
                    existing_dates_set = set(pd.Timestamp(t).strftime('%Y%m%d') for t in existing_times)
                    files = [f for f in files if f.rsplit('_', 1)[1].split('.')[0] in existing_dates_set]
                    logger.info(f"  Filtered to {len(files)} files matching {len(existing_dates_set)} existing repo dates")

                # If no files remain after filtering,
                if not files:
                    # Log that there are no new files for this variable and skip to the next variable
                    logger.info(f"  ℹ No new files for {variable}. Skipping.")
                    continue
                
                # Create empty lists of the valid datasets and files that are skipped due to being empty
                valid_ds_list = []
                skipped_files = []
                
                # For each file in the sorted files 
                for f in tqdm(files, desc=f"  {variable}"):
                    # Try to,
                    try:
                        # Check file size, if the file is empty
                        if os.path.getsize(f) == 0:
                            # Log that the file is empty and add it to the skipped files list, skipping to the next file
                            skipped_files.append((f, "empty file"))
                            continue
                        
                        # Open the file with xarray using the netcdf4 engine and chunking
                        ds = xr.open_dataset(f, engine='netcdf4', chunks={})
                        # Select only the variable of interest from the dataset
                        ds = ds[[variable]]

                        # For CFSv2 forecasts: expand malformed files with fewer than 4 start times
                        if this_model == 'CFSv2' and datatype == 'forecast':
                            if len(ds['S']) < 4:
                                # Get the existing start time
                                existing_s = ds['S'].values[0]
        
                                # Determine day and all 4 expected start times for that day
                                day = pd.Timestamp(existing_s).normalize()
                                expected_s = [day + pd.Timedelta(hours=h) for h in [0, 6, 12, 18]]
        
                                # Reindex to all 4 start times (NaN-pads missing ones)
                                ds = ds.reindex(S=expected_s)
        
                        logger.debug(
                        f"  CFSv2: Expanded malformed file {Path(f).name} "
                        f"from {len(ds['S'].values)} to 4 start times (NaN-padded missing)")
                        
                        # Check if the first member, lead, and pressure slice has any non-NaN values
                        if 'P' in ds[variable].dims:
                            valid = ~ds[variable].isel(M=0, L=0, P=0).isnull().all(dim=('Y','X'))
                        else:
                            valid = ~ds[variable].isel(M=0, L=0).isnull().all(dim=('Y','X'))
                        
                        # If there are any valid values,
                        if valid.any():
                            # Add the dataset to the list of valid datasets to be concatenated together later
                            valid_ds_list.append(ds)
                        # If there are no valid values
                        else:
                            # Add the file to the list of skipped files and add the reason it was skipped
                            skipped_files.append((f, "no valid data (all NaN)"))
                    
                    # If any exception occurs while processing the file,
                    except Exception as e:
                        # Log the error and add the file to the list of skipped files with the reason it was skipped
                        skipped_files.append((f, str(e)))
                
                # Update the total files read and skipped counters
                total_files_read += len(valid_ds_list)
                total_files_skipped += len(skipped_files)
                
                # Log the number of valid files and skipped files for this variable
                logger.info(f"  ✓ Valid files: {len(valid_ds_list)}")
                logger.info(f"  ✓ Skipped files: {len(skipped_files)}")
                
                # If there are skipped files,
                if skipped_files:
                    # Log examples of some of them including the filenames and the reasons they were skipped
                    logger.info(f"  Example skipped files:")
                    for f, reason in skipped_files[:3]:
                        logger.info(f"    - {os.path.basename(f)}: {reason}")
                
                # If there are valid datasets in the list to be concatenated together,
                if valid_ds_list:
                    # Try to,
                    try:
                        # Reconcile the 'L' (lead-time) coordinate across this variable's
                        # own per-date files before concatenating. The same provider can
                        # flip its nominal lead-time offset convention for a variable
                        # between one date and the next (e.g. a period-average field
                        # labeled half a day later on some dates, whole-day on others) --
                        # xr.concat would otherwise treat these as distinct L values and
                        # union (double) the L axis for this single variable, before the
                        # cross-variable reconciliation in STEP 4 ever runs. Align every
                        # file's L coordinate positionally to the first file's L values
                        # whenever lengths match.
                        reference_L = valid_ds_list[0]['L'].values
                        for i in range(1, len(valid_ds_list)):
                            if len(valid_ds_list[i]['L']) == len(reference_L) and not np.array_equal(valid_ds_list[i]['L'].values, reference_L):
                                logger.info(
                                    f"  ℹ Reconciling L coordinate across dates for {variable} "
                                    f"(offset differs from the reference date, same length {len(reference_L)})"
                                )
                                valid_ds_list[i] = valid_ds_list[i].assign_coords(L=reference_L)

                        # Concatenate the list of valid datasets together into one valid dataset and log success
                        ds_valid = xr.concat(valid_ds_list, dim='S')
                        logger.info(f"  ✓ Concatenated {len(valid_ds_list)} files into single dataset")
                        
                        # Get the start times of each non-NaN forecast/hindcast and index them via Pandas
                        S = pd.Index(ds_valid['S'].values)
                        
                        # Check to see if there are any duplicate start times
                        dups = S[S.duplicated()]
                        
                        # If there are duplicate start times,
                        if len(dups) > 0:
                            # Log a warning that there are duplicate start times and how many there are, 
                            # then remove the duplicates by selecting only the unique start times and log removal success
                            logger.warning(f"  ⚠ Found {len(dups)} duplicate S values")
                            ds_valid = ds_valid.sel(S=~S.duplicated())
                            logger.info(f"  ✓ Removed duplicates")
                        
                        # Remove the missing value metadata attribute
                        ds_valid[variable].attrs.pop('missing_value', None)
                        # Clear all metadata and encoding so the dataset can be written cleanly
                        ds_valid[variable].encoding = {}
                        
                        # Append the new dataset to the dataset list and log that it was added to the merge list
                        ds_list.append(ds_valid)
                        var_time_sets[variable] = set(pd.Index(ds_valid['S'].values))
                        logger.info(f"  ✓ Added to merge list")
                    
                    # If any exception occurs while concatenating the datasets,
                    except Exception as e:
                        # Log the error that occurred during concatenation and skip to the next variable
                        logger.error(f"  ✗ Error concatenating {variable}: {str(e)}")
                        logger.error(traceback.format_exc())
            
            # If any exception occurs while processing the files for this variable,
            except Exception as e:
                # Log the error that occurred during file processing for this variable and skip to the next variable
                logger.error(f"  ✗ Error processing {variable}: {str(e)}")
                logger.error(traceback.format_exc())
        
        # Log the file processing summary for all variables in this model
        logger.info(f"")
        logger.info(f"File processing summary:")
        logger.info(f"  Total files found: {total_files_found}")
        logger.info(f"  Total files read: {total_files_read}")
        logger.info(f"  Total files skipped: {total_files_skipped}")
        
        # If there are no valid datasets to merge and append to the repository,
        if not ds_list:
            # Log that there are no new data to append for this model and skip to the next model
            logger.info(f"")
            logger.info(f"✓ No new data found across all variables")
            logger.info(f"  Nothing to append to repository for {group_name}")
            model_results.append((this_model, True, "No new data"))
            continue
        
        ### Begin merging the datasets together and preparing them for writing to the repository
        # Log that the merging and preparation step is beginning
        logger.info("")
        logger.info("STEP 4: Merging and preparing dataset...")
        
        # Try to,
        try:
            # Reconcile the 'L' (lead-time) coordinate across variables before merging.
            # Different variables' raw source files can carry subtly different nominal
            # lead-time offsets for the same underlying sequence of forecast output
            # steps (e.g. an instantaneous field sampled at whole-day marks vs a
            # period-average field labeled half a day later) -- xr.merge treats these
            # as distinct L values and unions (doubles) the L axis instead of aligning
            # by forecast step, which breaks the write since the archive stores one
            # shared L axis per model. All configured variables for a model share the
            # same number of lead steps, so align every variable's L coordinate
            # positionally to the first variable's L values whenever lengths match.
            if ds_list:
                reference_L = ds_list[0]['L'].values
                for i in range(1, len(ds_list)):
                    if len(ds_list[i]['L']) == len(reference_L) and not np.array_equal(ds_list[i]['L'].values, reference_L):
                        logger.info(
                            f"  ℹ Reconciling L coordinate for a variable in the merge list "
                            f"(offset differs from the reference variable, same length {len(reference_L)})"
                        )
                        ds_list[i] = ds_list[i].assign_coords(L=reference_L)

            # Merge all the seperate model variable datasets in the dataset list together into one dataset
            ds_allvars = xr.merge(ds_list)
            # Log that the merge was successful and log the shape of the merged dataset
            logger.info(f"✓ Merged {len(ds_list)} variable datasets")
            logger.info(f"  Shape: {ds_allvars.dims}")
            
            # Make sure the dimensions are in the correct order after merging
            if 'P' in ds_allvars.dims:
                dim_order = ("S", "M", "L", "P", "Y", "X")
            else:
                dim_order = ("S", "M", "L", "Y", "X")
            
            for v in ds_allvars.data_vars:
                ds_allvars[v] = ds_allvars[v].transpose(*[d for d in dim_order if d in ds_allvars[v].dims])
            
            # Log that the dimensions were reordered successfully
            logger.info(f"✓ Reordered dimensions")
            
            # For each variable in the all variable model dataset
            for v in ds_allvars.data_vars:
                # Remove the missing value metadata attribute
                ds_allvars[v].attrs.pop('missing_value', None)
                # Clear all metadata and encoding so the dataset can be written cleanly
                ds_allvars[v].encoding = {}
            
            # Log that the encodings were cleaned successfully
            logger.info(f"✓ Cleaned encodings")
            
            ### If pressure level is a dimension in the data being added to the group, set the pressure levels to include all needed levels depending on the
            ### forecast model (CFS has more pressure levels for ua and va variables, GMAO-GEOS_V2p1_5daily and ESRL-FIMr1p1 have slighly different pressure 
            ### levels between zg and ua/va variables)
            # If pressure level is a dimension and the datatype is forecast data,
            if 'P' in ds_allvars.dims and datatype == 'forecast':
                # Log that the pressure levels are being dynamically determined from the data
                logger.info(f"Dynamically determining pressure levels from data...")
                
                # Create an empty set to hold the data pressure levels
                all_pressure_levels = set()
                # For each variable in the data to be uploaded
                for var in ds_allvars.data_vars:
                    # If the variable has pressure level as a dimension
                    if 'P' in ds_allvars[var].dims:
                        # Update the set of all pressure levels with those from the variable
                        all_pressure_levels.update(ds_allvars[var].coords['P'].values)

                # Also union in whatever pressure levels the group already has on disk.
                # A provider's currently-configured var_levels can supply a narrower
                # set of levels than the archive's historical P axis (e.g. a provider
                # config change that only fetches 1-2 levels going forward, while the
                # archive holds 4 levels from earlier history) -- reindexing only to
                # this run's own levels would shrink the P axis and break the append.
                # Levels no longer supplied are simply NaN for these new dates.
                if group_exists and 'P' in existing_ds.dims:
                    all_pressure_levels.update(existing_ds['P'].values)

                # Convert the set of all pressure levels to a sorted numpy array
                # and define it as the master pressure levels
                master_P = np.array(sorted(all_pressure_levels))
                
                # Reindex the pressure level dimension of the incoming data to include all the pressure levels
                ds_allvars = ds_allvars.reindex(P=master_P)
                # Log the master pressure levels that were determined and that the reindexing was successful
                logger.info(f"✓ Pressure levels: {master_P}")

        # If any exception occurs during the merging and preparation step,
        except Exception as e:
            # Log the error that occurred during merging and preparation and skip to the next model
            logger.error(f"✗ Error merging datasets: {str(e)}")
            logger.error(traceback.format_exc())
            model_results.append((this_model, False, f"Merge error: {str(e)}"))
            continue

        ### Determine whether every configured variable has at least one source file
        ### this run. Section 3 (appending brand-new start times) writes ds_allvars
        ### via to_zarr(..., append_dim="S"): any variable entirely absent from
        ### ds_allvars.data_vars would simply never get touched by that write, so
        ### its zarr array's S length would fall behind the other variables' --
        ### permanently breaking every future read of the group. (Variables that
        ### ARE present but only cover some of the new start times are already
        ### safe: xr.merge's outer join fills the gaps with NaN, so every present
        ### variable's array still grows by the same amount.) For variables with
        ### zero files this run, NaN-fill them below (see STEP 4b) so every
        ### configured variable's array always grows together; the NaN-to-real
        ### backfill check (a separate, later pass) fills in real data once a
        ### source file becomes available for that date.
        required_vars = subx_model['variables']
        vars_with_no_files_this_run = [v for v in required_vars if v not in ds_allvars.data_vars]

        ### Compare the variables and times in the merged dataset from qlcs to those in the model group to see what is missing
        # Log that the comparison of variables and times is beginning
        logger.info("")
        logger.info("STEP 5: Determining missing variables and times...")
        
        # Try to, 
        try:
            # Get all the start times in the merged dataset from qlcs
            all_times = pd.Index(ds_allvars['S'].values)
            
            # Compare the variables and times in the merged dataset from qlcs to those in the model group to see what is missing.
            # Only include variables that are both absent from the repo AND present in ds_allvars (i.e. had source files).
            missing_vars = [v for v in subx_model['variables'] if v not in existing_vars and v in ds_allvars.data_vars]
            missing_times = all_times.difference(existing_times)
            
            # Log the missing variables and the number of new start times that will be appended to the repository
            logger.info(f"Missing variables: {missing_vars}")
            logger.info(f"Number of new start times: {len(missing_times)}")
            if dry_run:
                logger.info(f"[DRY RUN] Would update {group_name} with {len(missing_vars)} missing variable(s) and {len(missing_times)} new start time(s)")
        
        # If any exception occurs during the comparison of variables and times,
        except Exception as e:
            # Log the error that occurred during the comparison and skip to the next model
            logger.error(f"✗ Error determining missing data: {str(e)}")
            logger.error(traceback.format_exc())
            model_results.append((this_model, False, f"Missing data check error: {str(e)}"))
            continue

        ### STEP 5b: NaN-fill any configured variable that had zero source files
        ### this run, for the new start times about to be appended. This keeps
        ### every variable's S-dimension growing in lockstep -- the actual
        ### constraint zarr enforces -- rather than holding the whole date back
        ### (the previous workaround). A later, separate backfill pass replaces
        ### the NaN with real data once a source file becomes available for
        ### that date. Only variables already present in the group are handled
        ### here; a variable missing from the group entirely is Section 2's job.
        if vars_with_no_files_this_run and len(missing_times) > 0:
            logger.info("")
            logger.info(
                f"ℹ No source file(s) this run for {vars_with_no_files_this_run}; "
                f"NaN-filling {len(missing_times)} new start time(s) for now. "
                f"Real data will replace the NaN automatically once a source "
                f"file is available for these dates."
            )
            for v in vars_with_no_files_this_run:
                if v not in existing_vars:
                    # Brand-new variable with zero files this run -- nothing to
                    # append for it yet either way; Section 2 picks it up once
                    # a file exists for at least one already-existing time.
                    continue
                needs_p = 'P' in existing_ds[v].dims
                # Use an already-present variable in ds_allvars as the shape/coord
                # template so Y/X/M/L/P match exactly what's being written this
                # run for the rest of the model -- avoids any cross-source
                # coordinate precision mismatch between ds_allvars (freshly read
                # local files) and existing_ds (the group's stored data).
                template_name = next(
                    (tv for tv in ds_allvars.data_vars if ('P' in ds_allvars[tv].dims) == needs_p),
                    None,
                )
                if template_name is None:
                    logger.warning(
                        f"  ⚠ No suitable template variable this run to NaN-fill {v} "
                        f"(needs_p={needs_p}); deferring to a later run."
                    )
                    continue
                nan_da = xr.full_like(ds_allvars[template_name], np.nan).astype(existing_ds[v].dtype)
                nan_da.name = v
                ds_allvars[v] = nan_da
                logger.info(f"  NaN-filled {v} (template={template_name})")

        ### Write or append the forecast data based on the data already existing in the model group on the repository
        # Log that the writing/appending step is beginning
        logger.info("")
        logger.info("STEP 6: Writing to repository...")

        if dry_run:
            if not group_exists:
                logger.info(f"[DRY RUN] Would create new group {group_name}")
            if missing_vars and len(existing_times) > 0:
                logger.info(f"[DRY RUN] Would add missing variables for existing times: {missing_vars}")
            if len(missing_times) > 0 and len(existing_times) > 0:
                logger.info(f"[DRY RUN] Would append {len(missing_times)} new start time(s)")
            if len(missing_times) == 0 and not missing_vars and group_exists:
                logger.info("[DRY RUN] No repo changes would be made")
            model_results.append((this_model, True, "Dry run: no writes performed"))
            continue
        
        # Try to,
        try:
            # Create a writable session to write and append the data in isolation without impacting the data on the repository
            write_session = repo.writable_session(branch_name)
            write_store = write_session.store
            # Log that the writable session was created successfully
            logger.info(f"✓ Created writable session")
            # Commit messages are accumulated here and only committed once, after
            # STEP 7 verifies the staged writes are structurally consistent. This
            # keeps a failed verification from leaving a partial write permanently
            # on the branch (see STEP 7 below).
            commit_messages = []

        # If any exception occurs during the creation of the writable session,
        except Exception as e:
            # Log the error that occurred during writable session creation and skip to the next model
            logger.error(f"✗ Failed to create writable session: {str(e)}")
            logger.error(traceback.format_exc())
            model_results.append((this_model, False, f"Write session error: {str(e)}"))
            continue
        
        ##### 1. In the case that the model group does not exist yet on arraylake #####
        if not group_exists:
            # Log that the group does not exist and the initial write of all variables and start times will be performed
            logger.info("")
            logger.info("SECTION 1: Initial write of all variables and start times")
            
            # Try to,
            try:
                # Write the model group edit message that the initial writing of all variables and start times is being 
                # done and log it
                message = f"Initial write of all variables and start times for {group_name}"
                logger.info(f"Message: {message}")
                
                # Start a timer for the inital writing of the merged qlcs dataset to the repository
                repo_write_start = time.time()
                
                ### Rechunk the all variable dataset into chunks depending on the data dimensions for writing
                ### L: Lead time, S: Start time, M: Model member, P: Pressure level, X: Longitude, Y: Latitude
                
                ### If the model being uploaded in the NCEP-CFS version 2, chunk by day since there are 4 start times per day
                if this_model == "CFSv2":
                    chunks = {'L': len(ds_allvars['L']), 'Y': len(ds_allvars['Y']), 'X': len(ds_allvars['X']), 'M': 1, 'S': 4}
                    # If pressure is a dimension, chunk by pressure level as well. If it is not, then chunk by member and start time only
                    if 'P' in ds_allvars.dims:
                        chunks['P'] = 1
                    # Log the chunking strategy being used for CFSv2
                    logger.info(f"Using CFSv2 chunking: S=4")
                
                ### If the model being uploaded is any other than the CFSv2
                else:
                    chunks = {'L': len(ds_allvars['L']), 'Y': len(ds_allvars['Y']), 'X': len(ds_allvars['X']), 'M': 1, 'S': 1}
                    # If pressure is a dimension, chunk by pressure level as well. If it is not, then chunk by member and start time only
                    if 'P' in ds_allvars.dims:
                        chunks['P'] = 1
                    # Log the chunking strategy being used for standard models
                    logger.info(f"Using standard chunking: S=1")
                
                # Rechunk the dataset with the defined chunking strategy
                ds_allvars = ds_allvars.chunk(chunks=chunks)
                

                # When doing a fresh write of the data, clear all metadata and encoding so the dataset can be
                # written cleanly without any issues of metadata or encoding from the original files
                for v in ds_allvars.data_vars:
                    ds_allvars[v].encoding = {}
                for coord in ds_allvars.coords:
                    ds_allvars[coord].encoding = {}
                
                # Write the merged dataset with all variables and start times to the repository
                ds_allvars.to_zarr(write_store, zarr_format=3, consolidated=False, mode="w", group=group_name)
                # Defer the commit until STEP 7 verifies this write is structurally sound
                commit_messages.append(message)

                # Log that the initial write was successful and how long it took
                logger.info(f"✓ Initial write complete in {time.time() - repo_write_start:.1f} seconds")
            
            # If any exception occurs during the initial write of the merged dataset to the repository,
            except Exception as e:
                # Log the error that occurred during the initial write and skip to the next model
                logger.error(f"✗ Section 1 failed: {str(e)}")
                logger.error(traceback.format_exc())
                model_results.append((this_model, False, f"Section 1 error: {str(e)}"))
                continue
        
        ##### 2. In the case that the model group does already exist and new variables need to be added #####
        # If there are missing variables and at least one already existing start time in the model group
        if missing_vars and len(existing_times) > 0:
            # Log that there are missing variables and the missing variables will be added for the existing start times
            logger.info("")
            logger.info("SECTION 2: Adding missing variables to existing times")
            
            # Try to, 
            try:
                # Write the model group edit message that certain missing variables are being added to the model group and log it
                var_message = f"Adding missing variables {missing_vars} for {len(existing_times)} existing times"
                logger.info(f"Message: {var_message}")
                
                # Subset the merged dataset to select the missing variable data.
                # Only write times we actually have data for (intersection with existing times),
                # to avoid expanding to all existing_times when only a single init date was loaded.
                available_times = ds_allvars['S'].values
                times_to_write = existing_times[np.isin(existing_times, available_times)]

                if len(times_to_write) == 0:
                    logger.info("  No overlapping start times for missing variables. Skipping Section 2.")
                    continue
                
                # Start a timer for the appending of the new variables to the model group
                var_append_start = time.time()

                # Keep the normal multi-variable write path, but use smaller CFSv2 time batches
                # to avoid materializing too many start times in memory during the append.
                time_batch_size = 1 if this_model == "CFSv2" else len(times_to_write)
                write_batches = 0

                if this_model == "CFSv2":
                    logger.info("Using CFSv2 missing-variable batching: all vars, S<=1")
                else:
                    logger.info("Using standard missing-variable append: all vars")

                missing_var_dataset = ds_allvars[missing_vars].sel(S=times_to_write)

                for start_idx in range(0, len(missing_var_dataset['S']), time_batch_size):
                    end_idx = min(start_idx + time_batch_size, len(missing_var_dataset['S']))
                    subset_missing_vars = missing_var_dataset.isel(S=slice(start_idx, end_idx))

                    chunks = {
                        'L': len(subset_missing_vars['L']),
                        'Y': len(subset_missing_vars['Y']),
                        'X': len(subset_missing_vars['X']),
                        'M': 1,
                        'S': min(time_batch_size, len(subset_missing_vars['S'])) if this_model == "CFSv2" else 1,
                    }
                    if 'P' in subset_missing_vars.dims:
                        chunks['P'] = 1

                    subset_missing_vars = subset_missing_vars.chunk(chunks=chunks)

                    for v in subset_missing_vars.data_vars:
                        subset_missing_vars[v].encoding = {}
                    for coord in subset_missing_vars.coords:
                        subset_missing_vars[coord].encoding = {}

                    if this_model == "CFSv2":
                        subset_missing_vars.to_zarr(
                            write_store,
                            zarr_format=3,
                            consolidated=False,
                            mode="a",
                            group=group_name,
                            append_dim="S",
                            align_chunks=True,
                            safe_chunks=False,
                        )
                    else:
                        subset_missing_vars.to_zarr(
                            write_store,
                            zarr_format=3,
                            consolidated=False,
                            mode="a",
                            group=group_name,
                            append_dim="S",
                        )

                    write_batches += 1

                # Defer the commit until STEP 7 verifies this write is structurally sound
                commit_messages.append(var_message)

                # Log that the missing variables were added successfully and how long it took
                logger.info(f"✓ Missing variables added in {time.time() - var_append_start:.1f} seconds across {write_batches} write batches")
                
            # If any exception occurs during the appending of the new variables to the existing model group,
            except Exception as e:
                # Log the error that occurred during the append of new variables and skip to the next model
                logger.error(f"✗ Section 2 failed: {str(e)}")
                logger.error(traceback.format_exc())
                model_results.append((this_model, False, f"Section 2 error: {str(e)}"))
                continue
        
        ##### 3. In the case that the model group already exists and new start times need to be added #####
        # Any variable missing a file this run has already been NaN-filled (see STEP 5b
        # above) so that every configured variable's S dimension grows together here,
        # regardless of which providers were slow/flaky this run.
        if len(missing_times) > 0 and len(existing_times) > 0:
            # Log that there are new start times to be added and the new start times will be appended to the existing model group
            logger.info("")
            logger.info("SECTION 3: Appending new start times")
            
            # Try to, 
            try:
                # Sort the missing times to ensure correct order
                missing_times = missing_times.sort_values()
                
                # Define the range of the new start dates being added
                first_missing_time = missing_times[0]
                last_missing_time = missing_times[-1]
                
                # Write the model group edit message that a number of new start times are being added for all already existing variables and log it
                time_message = f"Appending {len(missing_times)} new start times from {first_missing_time} to {last_missing_time}"
                logger.info(f"Message: {time_message}")
                
                # Subset the merged dataset from qlcs to select all variables for all the new start times
                subset_missing_times = ds_allvars.reindex(S=missing_times)
                
                # Start a timer for the appending of the new start times to the model group
                time_append_start = time.time()
                
                ### Rechunk the subsetted missing variables dataset into chunks depending on the data dimensions for writing
                ### L: Lead time, S: Start time, M: Model member, P: Pressure level, X: Longitude, Y: Latitude
                
                ### If the model being uploaded in the NCEP-CFS version 2, chunk by day since there are 4 start times per day
                if this_model == "CFSv2":
                    chunks = {'L': len(subset_missing_times['L']), 'Y': len(subset_missing_times['Y']), 'X': len(subset_missing_times['X']), 'M': 1, 'S': 4}
                    # If pressure is a dimension, chunk by pressure level as well. If it is not, then chunk by member and start time only
                    if 'P' in subset_missing_times.dims:
                        chunks['P'] = 1
                
                ### If the model being uploaded is any other than the CFSv2
                else:
                    chunks = {'L': len(subset_missing_times['L']), 'Y': len(subset_missing_times['Y']), 'X': len(subset_missing_times['X']), 'M': 1, 'S': 1}
                    # If pressure is a dimension, chunk by pressure level as well. If it is not, then chunk by member and start time only
                    if 'P' in subset_missing_times.dims:
                        chunks['P'] = 1
                
                # Rechunk the dataset with the defined chunking strategy
                subset_missing_times = subset_missing_times.chunk(chunks=chunks)
                
                # When appending new start times, clear all metadata and encoding so the dataset can be
                # written cleanly without any issues of metadata or encoding from the original files
                for v in subset_missing_times.data_vars:
                    subset_missing_times[v].encoding = {}
                for coord in subset_missing_times.coords:
                    subset_missing_times[coord].encoding = {}
                
                # If the model being appended to is CFSv2
                if this_model == "CFSv2":
                    # Log that the CFSv2 append with align_chunks=True is being used since there are 4 start times per day and sometimes new start times being added may not be a full day of 4 start times
                    logger.info(f"Using CFSv2 append with align_chunks=True")
                    
                    # Append it with aligning chunks since there are 4 start times per day and sometimes 
                    # new start times being added may not be a full day of 4 start times
                    subset_missing_times.to_zarr(write_store, zarr_format=3, consolidated=False, mode="a", append_dim="S",
                                                 group=group_name, align_chunks=True, safe_chunks=False)
                
                # If the model is not CFSv2
                else:
                    # Append the model group dataset normally along the start time dimension (S) with the new start times from qlcs for all already existing variables
                    subset_missing_times.to_zarr(write_store, zarr_format=3, consolidated=False, mode="a", append_dim="S",
                                                 group=group_name)
                
                # Defer the commit until STEP 7 verifies this write is structurally sound
                commit_messages.append(time_message)

                # Log that the new start times were added successfully and how long it took
                logger.info(f"✓ {len(missing_times)} new start times appended in {time.time() - time_append_start:.1f} seconds")
                
            # If any exception occurs during the appending of the new start times to the existing model group,
            except Exception as e:
                # Log the error that occurred during the append of new start times and skip to the next model
                logger.error(f"✗ Section 3 failed: {str(e)}")
                logger.error(traceback.format_exc())
                model_results.append((this_model, False, f"Section 3 error: {str(e)}"))
                continue
        
        ##### 4. Check recent existing start times for NaN slots that can now be backfilled with real data #####
        # Independent of whether any new start times were appended above -- this looks
        # backward over dates already committed to the group (as of the read-only
        # existing_ds snapshot from STEP 1), in case a source file that was missing on
        # an earlier run has since become available (e.g. a slow/rotating provider
        # caught up, or a manual recovery fetch was performed). Writes go directly
        # into the already-correctly-shaped existing array via a raw zarr region
        # write (no resize/append needed, unlike Sections 1-3) and are covered by
        # the same STEP 7 verify-before-commit gate below.
        backfill_window = 4
        if group_exists and len(existing_times) > 0:
            logger.info("")
            logger.info("SECTION 4: Checking recent existing start times for NaN backfill")

            try:
                sorted_existing_times = existing_times.sort_values()
                recent_times = sorted_existing_times[-backfill_window:]
                backfill_count = 0

                for var in subx_model['variables']:
                    if var not in existing_vars:
                        continue

                    existing_var_da = existing_ds[var]

                    for s_val in recent_times:
                        # Position in the array's own storage order -- append-only
                        # writes never reorder previously-existing S entries, so
                        # this position stays valid even after Section 3 appended
                        # brand-new dates beyond it this same run.
                        pos = int(np.flatnonzero(existing_ds['S'].values == s_val)[0])

                        is_nan = bool(existing_var_da.isel(S=pos).isnull().all().compute())
                        if not is_nan:
                            continue

                        date_str = pd.Timestamp(s_val).strftime('%Y%m%d')
                        f = (f"{input_path}/{this_group}-{this_local_model}/{datatype}/{var}/"
                             f"{var}_{this_group}-{this_local_model}_{date_str}.daily.nc")
                        if not os.path.exists(f) or os.path.getsize(f) == 0:
                            continue

                        try:
                            src_ds = xr.open_dataset(f, engine='netcdf4', chunks={})
                            src_da = src_ds[var]
                            if 'P' in src_da.dims:
                                valid = ~src_da.isel(S=0, M=0, L=0, P=0).isnull().all(dim=('Y', 'X'))
                            else:
                                valid = ~src_da.isel(S=0, M=0, L=0).isnull().all(dim=('Y', 'X'))
                            if not bool(valid.compute()):
                                logger.info(f"  {var} {date_str}: local file still all-NaN, skipping")
                                continue

                            # Align to the group's existing shape/coords for this
                            # variable (dim order, P levels, dtype) before the raw
                            # region write below.
                            src_da = src_da.transpose(*[d for d in existing_var_da.dims if d in src_da.dims])
                            if 'P' in existing_var_da.dims:
                                src_da = src_da.reindex(P=existing_var_da['P'].values)
                            src_da = src_da.isel(S=0).astype(existing_var_da.dtype)
                            values = src_da.values

                            za = zarr.open_array(write_store, path=f"{group_name}/{var}")
                            za[pos:pos + 1, ...] = values[np.newaxis, ...]

                            backfill_count += 1
                            commit_messages.append(f"Backfilled {var} for {date_str} with real data")
                            logger.info(f"  ✓ Backfilled {var} for {date_str}")

                        except Exception as e:
                            logger.warning(f"  ⚠ Failed to backfill {var} {date_str}: {str(e)}")

                if backfill_count == 0:
                    logger.info("  No NaN slots found with newly-available real data")

            except Exception as e:
                logger.error(f"✗ Section 4 failed: {str(e)}")
                logger.error(traceback.format_exc())

        ### Check the edits made to the data in the model group before committing ###
        # Log that the verification step is beginning
        logger.info("")
        logger.info("STEP 7: Verifying changes before final commit...")
        
        # Try to, 
        try:
            # Make sure that the changes that will be made to the model group are correct before committing them
            staged_ds = xr.open_zarr(write_store, group=group_name, consolidated=False)
            
            # Get a list of all the new variables and the new times for the now appended/written data
            new_vars = list(staged_ds.data_vars)
            new_times = pd.Index(staged_ds["S"].values)
            
            # Log the variables and times that are going to be committed to the repository
            logger.info(f"✓ Final variable count: {len(new_vars)}")
            logger.info(f"  Variables: {new_vars}")
            logger.info(f"✓ Final start time count: {len(new_times)}")
            logger.info(f"  Range: {new_times[0]} to {new_times[-1]}")
            
        # If any exception occurs during the verification of the changes before committing,
        except Exception as e:
            # Log the error that occurred during verification and skip to the next model. Nothing
            # was committed above (writes were only staged in commit_messages), so the branch is
            # left exactly as it was before this model was processed -- no partial write persists.
            logger.error(f"✗ Verification failed: {str(e)}")
            logger.error(traceback.format_exc())
            model_results.append((this_model, False, f"Verification error: {str(e)}"))
            continue

        # Verification passed: commit the staged write(s) now, as a single commit.
        if commit_messages:
            try:
                write_session.commit("; ".join(commit_messages))
                logger.info(f"✓ Committed: {'; '.join(commit_messages)}")
            except Exception as e:
                logger.error(f"✗ Commit failed: {str(e)}")
                logger.error(traceback.format_exc())
                model_results.append((this_model, False, f"Commit error: {str(e)}"))
                continue

        ### Finally commit the new changes with messages describing what was changed and update the main branch ###
        # Log that the final commit and main branch update step is beginning
        logger.info("")
        logger.info("STEP 8: Updating main branch...")
        
        # Try to,
        try:
            # Merge the changes on the seperate branch to the main branch to make them visible and prevent any committing conflicts
            if branch_name != "main":
                # Get snapshot of the branch you just wrote to
                source_session = repo.readonly_session(branch_name)
                source_snapshot = source_session.snapshot_id
                
                # Move main branch pointer to that snapshot
                repo.reset_branch("main", source_snapshot)
                
                # Log that the main branch was updated successfully
                logger.info(f"✓ Main branch updated successfully")
            
            # Log that the model was completed successfully and add it to the model results list as a success
            logger.info(f"✓ {this_model} completed successfully!")
            model_results.append((this_model, True, "Success"))

        # If any exception occurs during the final commit and main branch update, 
        except Exception as e:
            # Log the error that occurred during the main branch update and add the model to the 
            # model results list as a failure with the error message
            logger.error(f"✗ Failed to update main branch: {str(e)}")
            logger.error(traceback.format_exc())
            model_results.append((this_model, False, f"Branch update error: {str(e)}"))
            continue
    
    # If any unexpected exception occurs during the processing of the model that was not caught by 
    # the specific try except blocks,
    except Exception as e:
        # Log the unexpected error that occurred during the processing of the model and add it to the model 
        # results list as a failure with the error message
        logger.error(f"✗ Unexpected error processing {this_model}: {str(e)}")
        logger.error(traceback.format_exc())
        model_results.append((this_model, False, f"Unexpected error: {str(e)}"))

### Log the final summary of the forecast appending process for all models
logger.info("")
logger.info("="*70)
logger.info("FORECAST APPEND SUMMARY")
logger.info("="*70)

# Calculate the number of successful and failed model updates
successful = sum(1 for _, success, _ in model_results if success)
failed = len(model_results) - successful

# Log the total number of models processed, the number of successful updates, and the number of failed updates
logger.info(f"Total models processed: {len(model_results)}")
logger.info(f"Successful: {successful}")
logger.info(f"Failed: {failed}")
logger.info("")

# For each model result in the model results list,
for model_name, success, message in model_results:
    # Log the model name, whether it was a success or failure, and the message describing the result
    status = "✓" if success else "✗"
    logger.info(f"{status} {model_name}: {message}")

# Log the completion of the forecast appending process with the log file location and the completion time
logger.info("")
logger.info("Logs captured by workflow stage log file")
logger.info("="*70)
logger.info(f"Completed at {datetime.now()}")
logger.info("="*70)