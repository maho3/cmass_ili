
import logging
import datetime
import os
import fcntl
import time
from os.path import join
from astropy.cosmology import Cosmology, FlatLambdaCDM
from colossus.cosmology import cosmology as csm
from omegaconf import OmegaConf


def get_source_path(wdir, suite, sim, L, N, lhid, check=True):
    # get the path to the source directory, and check at each level
    sim_dir = join(wdir, suite, sim)
    cfg_dir = join(sim_dir, f'L{L}-N{N}')
    lh_dir = join(cfg_dir, str(lhid))

    if check:
        if not os.path.isdir(sim_dir):
            raise ValueError(
                f"Simulation directory {sim_dir} does not exist.")
        if not os.path.isdir(cfg_dir):
            raise ValueError(
                f"Configuration directory {cfg_dir} does not exist.")
        if not os.path.isdir(lh_dir):
            raise ValueError(
                f"Latin hypercube directory {lh_dir} does not exist.")
    return lh_dir


def timing_decorator(func, *args, **kwargs):
    def wrapper(*args, **kwargs):
        logging.info(f"Running {func.__name__}...")
        t0 = datetime.datetime.now()
        out = func(*args, **kwargs)
        dt = (datetime.datetime.now() - t0).total_seconds()
        logging.info(
            f"Finished {func.__name__}... "
            f"({int(dt//60)}m{int(dt%60)}s)")
        return out
    return wrapper


def save_cfg(source_path, cfg, field=None):
    if os.path.isfile(join(source_path, 'config.yaml')):
        old_cfg = OmegaConf.load(join(source_path, 'config.yaml'))
        if field is not None:
            cfg = OmegaConf.masked_copy(cfg, field)
            cfg = OmegaConf.merge(old_cfg, cfg)
    OmegaConf.save(cfg, join(source_path, 'config.yaml'))


def load_params(index, cosmofile):
    # load cosmology parameters
    # [Omega_m, Omega_b, h, n_s, sigma8]
    if index == "fid":
        return [0.3175, 0.049, 0.6711, 0.9624, 0.834]
    with open(cosmofile, 'r') as f:
        content = f.readlines()[index]
    content = [float(x) for x in content.split()]
    return content


def cosmo_to_astropy(params=None, omega_m=None, omega_b=None,
                     h=None, n_s=None, sigma8=None):
    """
    Converts a list of cosmological parameters into an astropy cosmology
    object. Note, ignores s8 and n_s parameters, which are not used in astropy.
    """
    if isinstance(params, Cosmology):
        return params
    try:
        params = list(params)
        return FlatLambdaCDM(H0=params[2]*100, Om0=params[0], Ob0=params[1])
    except TypeError:
        return FlatLambdaCDM(H0=h*100, Om0=omega_m, Ob0=omega_b)


def get_particle_mass(N, L, omega_m, h):
    """
    M_particle = Omega_m * rho_crit * Volume / NumParticles

    Args:
        N (int): number of particles per dimension
        L (float): box side length (Mpc/h)
        omega_m (float): matter density
        h (float): Hubble constant
    """
    cosmo = cosmo_to_astropy(omega_m=omega_m, h=h)
    rho_crit = cosmo.critical_density0.to('Msun/Mpc^3').value
    volume = L**3  # (Mpc/h)^3
    NumParticles = N**3
    return omega_m * rho_crit * volume / (NumParticles * h**2)  # Msun/h


def cosmo_to_colossus(cpars):
    try:
        params = list(cpars)
    except TypeError:
        return params

    params = {'flat': True, 'H0': 100*cpars[2], 'Om0': cpars[0],
              'Ob0': cpars[1], 'sigma8': cpars[4], 'ns': cpars[3]}
    csm.addCosmology('myCosmo', **params)
    cosmo = csm.setCosmology('myCosmo')
    return cosmo



def acquire_lock(lock_file):
    """
    Acquires an exclusive lock on the specified lock file.

    This function will attempt to open or create a lock file and
    acquire an exclusive lock on it. If another process has already
    locked the file, the function will keep retrying until the lock
    is available.

    Parameters:
    - lock_file (str): The path to the lock file.

    Returns:
    - fd (int): The file descriptor of the opened lock file.
    """
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
    while True:
        try:
            # Try to acquire an exclusive lock, non-blocking mode
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            # If another process has the lock, wait and retry
            time.sleep(0.1)

def release_lock(fd):
    """
    Releases the exclusive lock on the specified file descriptor and closes the file.

    This function releases the lock obtained on a file and closes the file descriptor.

    Parameters:
    - fd (int): The file descriptor of the lock file.
    """
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)

