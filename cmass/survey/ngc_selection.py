"""
Applies BOSS survey mask to a lightcone-shaped volume of galaxies.

Requires:
    - pymangle
    - astropy

Input:
    - pos: (N, 3) array of galaxy positions
    - vel: (N, 3) array of galaxy velocities
"""

import os
os.environ['OPENBLAS_NUM_THREADS'] = '4'  # noqa, must be set before jax

import numpy as np
import logging
import h5py
from os.path import join as pjoin
import jax
from cuboid_remap import Cuboid, remap_Lbox
import hydra
from omegaconf import DictConfig, OmegaConf

from .tools import (
    xyz_to_sky, sky_to_xyz, rotate_to_z, random_rotate_translate,
    BOSS_angular, BOSS_veto, BOSS_redshift, BOSS_fiber,
    save_lightcone)
from ..utils import (get_source_path, timing_decorator)
from ..nbody.tools import parse_nbody_config


@timing_decorator
def load_galaxies_sim(source_dir, a, seed):
    filepath = pjoin(source_dir, 'hod', f'galaxies{seed}.h5')
    with h5py.File(filepath, 'r') as f:
        key = f'{a:.6f}'
        pos = f[key]['pos'][...]
        vel = f[key]['vel'][...]
    return pos, vel


@timing_decorator
def remap(ppos, pvel, L, u1, u2, u3):
    # remap the particles to the cuboid
    new_size = list(L*np.array(remap_Lbox(u1, u2, u3)))
    logging.info(f'Remapping from {[L]*3} to {new_size}.')

    c = Cuboid(u1, u2, u3)
    ppos = jax.vmap(c.Transform)(ppos/L)*L
    pvel = jax.vmap(c.TransformVelocity)(pvel)
    return np.array(ppos), np.array(pvel)


@timing_decorator
def move_to_footprint(pos, vel, mid_rdz, cosmo, L):
    pos, vel = pos.copy(), vel.copy()

    # shift to origin
    pos -= pos.mean(axis=0)

    # find footprint center in comoving coordinates, conditioned on cosmo
    mid_xyz = sky_to_xyz(mid_rdz, cosmo)

    # rotate to same orientation as footprint
    _, rot_inv = rotate_to_z(mid_xyz, cosmo)
    pos = rot_inv.apply(pos)
    vel = rot_inv.apply(vel)

    # shift to center of footprint
    pos += mid_xyz

    return pos, vel


@timing_decorator
def apply_mask(rdz, wdir, fibermode=0):
    logging.info('Applying redshift cut...')
    mask = BOSS_redshift(rdz[:, -1])
    rdz = rdz[mask]
    logging.info(f'Removed {len(mask)-len(rdz)}/{len(mask)} galaxies')

    logging.info('Applying angular mask...')
    inpoly = BOSS_angular(*rdz[:, :-1].T, wdir=wdir)
    rdz = rdz[inpoly]
    logging.info(f'Removed {len(inpoly)-len(rdz)}/{len(inpoly)} galaxies')

    logging.info('Applying veto mask...')
    inveto = BOSS_veto(*rdz[:, :-1].T, wdir=wdir)
    rdz = rdz[~inveto]
    logging.info(f'Removed {len(inveto)-len(rdz)}/{len(inveto)} galaxies')

    if fibermode != 0:
        logging.info('Applying fiber collisions...')
        mask = BOSS_fiber(
            *rdz[:, :-1].T,
            sep=0.01722,  # ang. sep. for CMASS in deg
            mode=fibermode)
        rdz = rdz[mask]
        logging.info(f'Removed {len(mask)-len(rdz)}/{len(mask)} galaxies')

    return rdz


@timing_decorator
def custom_cuts(rdz, cfg):
    logging.info('Applying custom cuts...')
    if 'ra_range' in cfg.survey:
        ra_range = cfg.survey.ra_range
        mask = (rdz[:, 0] > ra_range[0]) & (rdz[:, 0] < ra_range[1])
        rdz = rdz[mask]
    if 'dec_range' in cfg.survey:
        dec_range = cfg.survey.dec_range
        mask = (rdz[:, 1] > dec_range[0]) & (rdz[:, 1] < dec_range[1])
        rdz = rdz[mask]
    if 'z_range' in cfg.survey:
        z_range = cfg.survey.z_range
        mask = (rdz[:, 2] > z_range[0]) & (rdz[:, 2] < z_range[1])
        rdz = rdz[mask]
    return rdz


@timing_decorator
def reweight(rdz, wdir='./data'):
    # load observed n(z)
    n_z = np.load(
        pjoin(wdir, 'obs', 'n-z_DR12v5_CMASS_North.npy'),
        allow_pickle=True).item()
    be, hobs = n_z['be'], n_z['h']

    # load simulated n(z)
    hsim, _ = np.histogram(rdz[:, -1], bins=be)
    hobs, hsim = hobs.astype(int), hsim.astype(int)
    for i in range(len(hsim)):
        if hsim[i] < hobs[i]:
            logging.warning(
                f'hsim ({hsim[i]}) < hobs ({hobs[i]}) in bin: '
                f'{be[i]:.5f}<z<{be[i+1]:.5f},\n'
                'More simulated galaxies than observed. '
                'Reweighting may not be accurate.')

    # sample at most as many as observed
    hsamp = np.minimum(hobs, hsim)

    # mask
    mask = np.zeros(len(rdz), dtype=bool)
    bind = np.digitize(rdz[:, -1], be) - 1
    for i in range(len(be) - 1):
        in_bin = np.argwhere(bind == i).flatten()
        in_samp = np.random.choice(in_bin, size=hsamp[i], replace=False)
        mask[in_samp] = True

    return rdz[mask]


@timing_decorator
@hydra.main(version_base=None, config_path="../conf", config_name="config")
def main(cfg: DictConfig) -> None:
    # Filtering for necessary configs
    cfg = OmegaConf.masked_copy(
        cfg, ['meta', 'sim', 'nbody', 'bias', 'survey'])

    # Build run config
    cfg = parse_nbody_config(cfg)
    logging.info('Running with config:\n' + OmegaConf.to_yaml(cfg))
    source_path = get_source_path(cfg, cfg.sim)

    # Check that we are not in snapshot_mode
    if hasattr(cfg.nbody, 'snapshot_mode') and cfg.nbody.snapshot_mode:
        raise ValueError('snapshot_mode config is true, but ngc_selection'
                         ' is only for non snapshot mode.')

    # Load galaxies
    pos, vel = load_galaxies_sim(source_path, cfg.nbody.af, cfg.bias.hod.seed)

    # [Optionally] rotate and shuffle cubic volume
    pos, vel = random_rotate_translate(
        pos, L=cfg.nbody.L, vel=vel, seed=cfg.survey.rot_seed)

    # Apply cuboid remapping
    pos, vel = remap(
        pos, vel, cfg.nbody.L,
        cfg.survey.u1, cfg.survey.u2, cfg.survey.u3)

    # Rotate and shift to align with CMASS
    pos, vel = move_to_footprint(
        pos, vel, cfg.survey.mid_rdz, cfg.nbody.cosmo, cfg.nbody.L)

    # Calculate sky coordinates
    rdz = xyz_to_sky(pos, vel, cfg.nbody.cosmo)

    # Apply mask
    rdz = apply_mask(rdz, cfg.meta.wdir, cfg.survey.fibermode)

    # Custom cuts
    rdz = custom_cuts(rdz, cfg)

    # Reweight (Todo: fiber collisions should iterate with this?)
    rdz = reweight(rdz, cfg.meta.wdir)

    # Save
    outdir = pjoin(source_path, 'obs')
    os.makedirs(outdir, exist_ok=True)
    save_lightcone(
        outdir,
        ra=rdz[:, 0], dec=rdz[:, 1], z=rdz[:, 2],
        galsnap=np.zeros(len(rdz), dtype=int),
        galidx=np.arange(len(rdz)),
        hod_seed=cfg.bias.hod.seed)


if __name__ == "__main__":
    main()
