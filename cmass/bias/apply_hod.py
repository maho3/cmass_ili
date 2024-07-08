"""
Sample an HOD realization from the halo catalog using the Zheng+(2007) model.

Requires:
    - halotools
    - astropy

Input:
    - pos: halo positions
    - vel: halo velocities
    - mass: halo masses
    - seed: random seed for sampling HOD parameters

Output:
    - pos: galaxy positions
    - vel: galaxy velocities
"""

import numpy as np
import logging
import os
from os.path import join as pjoin
import hydra
import h5py
from omegaconf import DictConfig, OmegaConf, open_dict
from .tools.hod import (
    thetahod_literature, build_halo_catalog, build_HOD_model)
from ..utils import (
    get_source_path, timing_decorator, load_params, cosmo_to_astropy)


def parse_config(cfg):
    with open_dict(cfg):
        # HOD parameters
        cfg.bias.hod.theta = get_hod_params(cfg.bias.hod.seed)

        # Cosmology
        cfg.nbody.cosmo = load_params(
            cfg.nbody.lhid, cfg.meta.cosmofile)
    return cfg


def get_hod_params(seed=0):
    theta = thetahod_literature('reid2014_cmass')
    # sample theta based on priors set by Reid+(2014)
    if seed != 0:
        np.random.seed(seed)
        hod_lower_bound = np.array([12.0, 0.1, 13.0, 13.0, 0.])
        hod_upper_bound = np.array([14.0, 0.6, 15.0, 15.0, 1.5])
        keys = ['logMmin', 'sigma_logM', 'logM0', 'logM1', 'alpha']
        theta = np.random.uniform(hod_lower_bound, hod_upper_bound, size=(5))
        theta = [float(x) for x in theta]
        theta = dict(zip(keys, theta))
    return theta


@ timing_decorator
def populate_hod(
    hpos, hvel, hmass,
    cosmo, cfg, seed=0, mdef='vir'
):
    cosmo = cosmo_to_astropy(cosmo)

    BoxSize = cfg.nbody.L*np.ones(3)
    catalog = build_halo_catalog(
        hpos, hvel, 10**hmass, cfg.nbody.zf, BoxSize, cosmo,
        mdef=mdef
    )

    hod_params = cfg.bias.hod.theta

    hod = build_HOD_model(
        cosmo, cfg.nbody.zf, hod_model='zheng07', mdef=mdef,
        **hod_params
    )

    hod.populate_mock(catalog, seed=seed)

    galcat = hod.mock.galaxy_table.as_array()

    return galcat


def run_snapshot(pos, vel, mass, cfg):
    # Populate HOD
    logging.info('Populating HOD...')
    hod = populate_hod(
        pos, vel, mass,
        cfg.nbody.cosmo, cfg,
        seed=cfg.bias.hod.seed
    )

    # Organize outputs
    gpos = np.array([hod['x'], hod['y'], hod['z']]).T
    gvel = np.array([hod['vx'], hod['vy'], hod['vz']]).T
    meta = {'gal_type': hod['gal_type'], 'hostid': hod['halo_id']}
    return gpos, gvel, meta


def load_snapshot(source_path, a):
    with h5py.File(pjoin(source_path, 'halos.h5'), 'r') as f:
        group = f[f'{a:.6f}']
        hpos = group['pos'][...]
        hvel = group['vel'][...]
        hmass = group['mass'][...]
    return hpos, hvel, hmass


def delete_outputs(outpath):
    if os.path.isfile(outpath):
        os.remove(outpath)


def save_snapshot(outpath, a, gpos, gvel, **meta):
    with h5py.File(outpath, 'a') as f:
        group = f.create_group(f'{a:.6f}')
        group.create_dataset('pos', data=gpos)
        group.create_dataset('vel', data=gvel)
        for key, value in meta.items():
            group.create_dataset(key, data=value)


@ timing_decorator
@ hydra.main(version_base=None, config_path="../conf", config_name="config")
def main(cfg: DictConfig) -> None:
    # Filtering for necessary configs
    cfg = OmegaConf.masked_copy(cfg, ['meta', 'sim', 'nbody', 'bias'])

    # Build run config
    cfg = parse_config(cfg)
    logging.info('Running with config:\n' + OmegaConf.to_yaml(cfg))

    # Setup save directory
    source_path = get_source_path(cfg, cfg.sim)
    save_path = pjoin(source_path, 'hod')
    os.makedirs(save_path, exist_ok=True)
    save_file = pjoin(save_path, f'galaxies{cfg.bias.hod.seed}.h5')
    logging.info(f'Saving to {save_file}...')

    # Delete existing outputs
    delete_outputs(save_file)

    # Run each snapshot
    for i, a in enumerate(cfg.nbody.asave):
        logging.info(f'Running snapshot {i} at a={a:.6f}...')

        # Load snapshot
        hpos, hvel, hmass = load_snapshot(source_path, a)

        # Populate HOD
        gpos, gvel, meta = run_snapshot(hpos, hvel, hmass, cfg)

        # Save snapshot
        save_snapshot(save_file, a, gpos, gvel, **meta)

    logging.info('Done!')


if __name__ == '__main__':
    main()
