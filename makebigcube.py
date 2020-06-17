#!/usr/bin/env python
"""Make big QUOCKA cubes"""

from IPython import embed
import schwimmbad
import sys
from glob import glob
from tqdm import tqdm
import matplotlib.pyplot as plt
from radio_beam import Beam, Beams
from radio_beam.utils import BeamError
from astropy import units as u
from astropy.io import fits
from astropy.wcs import WCS
import au2
import scipy.signal
import numpy as np
from functools import partial
import reproject as rpj
import warnings
from astropy.utils.exceptions import AstropyWarning
warnings.simplefilter('ignore', category=AstropyWarning)


def round_up(n, decimals=0):
    multiplier = 10 ** decimals
    return np.ceil(n * multiplier) / multiplier


def getmaxbeam(file_dict, tolerance=0.0001, nsamps=200, epsilon=0.0005, verbose=False):
    """Find common beam

    Arguments:
        file_dict {dict} -- Filenames for each bandcube.

    Keyword Arguments:
        tolerance {float} -- See common_beam (default: {0.0001})
        nsamps {int} -- See common_beam (default: {200})
        epsilon {float} -- See common_beam (default: {0.0005})
        verbose {bool} -- Verbose output (default: {False})

    Returns:
        cmn_beam {Beam} -- Common beam
    """
    if verbose:
        print('Finding common beam...')
    stokes = ['i', 'q', 'u', 'v']
    beam_dict = {}
    beams = []
    for stoke in stokes:
        for file in file_dict[stoke]:
            header = fits.getheader(file, memmap=True)
            beam = Beam.from_fits_header(header)
            beams.append(beam)
    beams = Beams(
        [beam.major.value for beam in beams]*u.deg,
        [beam.minor.value for beam in beams]*u.deg,
        [beam.pa.value for beam in beams]*u.deg
    )

    try:
        cmn_beam = beams.common_beam(
            tolerance=tolerance, epsilon=epsilon, nsamps=nsamps)
    except BeamError:
        if verbose:
            print("Couldn't find common beam with defaults")
            print("Trying again with smaller tolerance")
        cmn_beam = beams.common_beam(
            tolerance=tolerance*0.1, epsilon=epsilon, nsamps=nsamps)
    cmn_beam = Beam(
        major=round_up(cmn_beam.major.to(u.arcsec), decimals=1),
        minor=round_up(cmn_beam.minor.to(u.arcsec), decimals=1),
        pa=round_up(cmn_beam.pa.to(u.deg), decimals=1)
    )
    return cmn_beam


def writecube(data, beam, stoke, field, outdir, verbose=False):
    """Write cubes to disk

    Arguments:
        data {dict} -- Image and frequency data and metadata
        beam {Beam} -- New common resolution
        stoke {str} -- Stokes parameter
        field {str} -- Field name
        outdir {str} -- Output directory

    Keyword Arguments:
        verbose {bool} -- Verbose output (default: {False})
    """
    # Make filename
    outfile = f"{field}.{stoke}.cutout.bigcube.fits"

    # Make header
    d_freq = np.nanmedian(np.diff(data['freqs']))
    header = data['target header']
    header = beam.attach_to_header(header)
    header['CRVAL3'] = data['freqs'][0].to_value()
    header['CDELT3'] = d_freq.to_value()

    # Save the data
    fits.writeto(f'{outdir}/{outfile}', data['cube'],
                 header=header, overwrite=True)
    if verbose:
        print("Saved cube to", f'{outdir}/{outfile}')

    if stoke == 'i':
        freqfile = f"{field}.bigcube.frequencies.txt"
        np.savetxt(f"{outdir}/{freqfile}", data['freqs'].to_value())
        if verbose:
            print("Saved frequencies to", f"{outdir}/{freqfile}")


def main(pool, args, verbose=False):
    """Main script
    """
    # Set up variables
    bands = [2100, 5500, 7500]
    stokes = ['i', 'q', 'u', 'v']
    datadir = args.datadir
    field = args.field

    if datadir is not None:
        if datadir[-1] == '/':
            datadir = datadir[:-1]

    outdir = args.outdir
    if outdir is not None:
        if outdir[-1] == '/':
            outdir = outdir[:-1]
    elif outdir is None:
        outdir = datadir

    # Glob out files
    file_dict = {}
    for stoke in stokes:
        file_dict.update(
            {
                stoke: sorted(
                    glob(f'{datadir}/{field}.*.{stoke}.cutout.bandcube.fits')
                )
            }
        )
    file_dict.update(
        {
            'freqs': sorted(
                glob(f'{datadir}/{field}.*.bandcube.frequencies.txt')
            )
        }
    )

    # Get common beam
    if args.target is None:
        new_beam = getmaxbeam(file_dict,
                              tolerance=args.tolerance,
                              nsamps=args.nsamps,
                              epsilon=args.epsilon,
                              verbose=verbose)
    elif args.target is not None:
        new_beam = Beam(major=args.target*u.arcsec,
                        minor=args.target*u.arcsec,
                        pa=0*u.deg)

    if verbose:
        print('Common beam is', new_beam)

    # Start computation - work on each Stokes
    stoke_dict = {}
    for stoke in stokes:
        print(f'Working on Stokes {stoke}...')
        datadict = {}

        # Get data from files
        for band in tqdm(bands, desc='Reading data', disable=(not verbose)):
            with fits.open(f'{datadir}/{field}.{band}.{stoke}.cutout.bandcube.fits',
                           memmap=True,
                           mode='denywrite') as hdulist:
                data = hdulist[0].data
                head = hdulist[0].header
                freq = np.loadtxt(
                    f'{datadir}/{field}.{band}.bandcube.frequencies.txt')
                datadict.update(
                    {
                        band: {
                            'data': data,
                            'head': head,
                            'wcs': WCS(head),
                            'freq': freq,
                            'beam': Beam.from_fits_header(head)
                        }
                    }
                )

        # Regrid
        target_wcs = datadict[2100]['wcs']
        for band in tqdm(bands, desc='Regridding data', disable=(not verbose)):
            worker = partial(
                rpj.reproject_exact,
                output_projection=target_wcs.celestial,
                shape_out=datadict[2100]['data'][0].shape,
                parallel=False,
                return_footprint=False
            )
            input_wcs = datadict[band]['wcs'].celestial
            inputs = [(image, input_wcs) for image in datadict[band]['data']]
            newcube = np.zeros_like(datadict[band]['data'])*np.nan
            out = list(
                tqdm(
                    pool.imap(
                        worker, inputs
                    ),
                    total=len(datadict[band]['data']),
                    desc='Regridding channels',
                    disable=(not verbose)
                )
            )
            newcube[:] = out[:]
            datadict[band].update(
                {
                    "newdata": newcube
                }
            )

        # Get scaling factors and convolution kernels
        target_header = datadict[2100]['head']
        for band in tqdm(bands, desc='Computing scaling factors', disable=(not verbose)):
            con_beam = new_beam.deconvolve(datadict[band]['beam'])
            dx = target_header['CDELT1']*-1*u.deg
            dy = target_header['CDELT2']*u.deg
            fac, amp, outbmaj, outbmin, outbpa = au2.gauss_factor(
                [
                    con_beam.major.to(u.arcsec).value,
                    con_beam.minor.to(u.arcsec).value,
                    con_beam.pa.to(u.deg).value
                ],
                beamOrig=[
                    datadict[band]['beam'].major.to(u.arcsec).value,
                    datadict[band]['beam'].minor.to(u.arcsec).value,
                    datadict[band]['beam'].pa.to(u.deg).value
                ],
                dx1=dx.to(u.arcsec).value,
                dy1=dy.to(u.arcsec).value
            )
            pix_scale = dy
            gauss_kern = con_beam.as_kernel(pix_scale)
            conbm = gauss_kern.array/gauss_kern.array.max()
            datadict[band].update(
                {
                    'conbeam': conbm,
                    'fac': fac,
                    'target header': target_header
                }
            )
            datadict.update(
                {
                    'target header': target_header
                }
            )

        # Convolve data
        for band in tqdm(bands, desc='Smoothing data', disable=(not verbose)):
            smooth = partial(
                scipy.signal.convolve,
                in2=datadict[band]['conbeam'],
                mode='same'
            )
            sm_data = np.zeros_like(datadict[band]['newdata'])*np.nan
            cube = np.copy(datadict[band]['newdata'])
            cube[~np.isfinite(cube)] = 0
            out = list(tqdm(
                pool.imap(
                    smooth, cube
                ),
                total=len(datadict[band]['newdata']),
                desc='Smoothing channels',
                disable=(not verbose)
            ))
            sm_data[:] = out[:]
            sm_data[~np.isfinite(cube)] = np.nan
            datadict[band].update(
                {
                    'smdata': sm_data,
                }
            )
        stoke_dict.update(
            {
                stoke: datadict
            }
        )

        # Show plots
        if args.debug:
            plt.figure()
            i_mom = np.nansum(datadict[2100]['smdata'], axis=0)
            idx = np.unravel_index(np.argmax(i_mom), i_mom.shape)
            for band in bands:
                x = datadict[band]['freq']
                y = datadict[band]['fac'] * \
                    datadict[band]['smdata'][:, idx[0], idx[1]]
                plt.plot(x, y, '.', label=f'Stokes {stoke} -- band {band}')
            plt.xscale('log')
            plt.yscale('log')
            plt.xlabel('Frequency [Hz]')
            plt.ylabel('Flux density [Jy/beam]')
            plt.legend()
            plt.show()

    # Make cubes
    for stoke in tqdm(stokes, desc='Making cubes', disable=(not verbose)):
        cube = np.vstack([stoke_dict[stoke][band]['smdata'] * stoke_dict[stoke][band]['fac'] for band in bands])
        freq_cube = np.concatenate([stoke_dict[stoke][band]['freq'] for band in bands]) * u.Hz
        stoke_dict[stoke].update(
            {
                'cube': cube,
                'freqs': freq_cube
            }
        )

    # Show plots
    if args.debug:
        i_mom = np.nansum(stoke_dict['i']['cube'], axis=0)
        idx = np.unravel_index(np.argmax(i_mom), i_mom.shape)
        plt.figure()     
        for stoke in stokes:
            x = stoke_dict[stoke]['freqs']
            y = stoke_dict[stoke]['cube'][:, idx[0], idx[1]]
            plt.plot(x, y, '.', label=f'Stokes {stoke}') 
        if stoke == 'i':
            plt.xscale('log')
            plt.yscale('log')
        plt.xlabel('Frequency [Hz]')
        plt.ylabel('Flux density [Jy/beam]')
        plt.legend()
        plt.show()

        plt.figure()     
        for stoke in stokes:
            x = (299792458 / stoke_dict[stoke]['freqs'])**2
            y = stoke_dict[stoke]['cube'][:, idx[0], idx[1]]
            plt.plot(x, y, '.', label=f'Stokes {stoke}') 
        plt.xlabel('$\lambda^2$ [m$^2$]')
        plt.ylabel('Flux density [Jy/beam]')
        plt.legend()
        plt.show()

    if not args.dryrun:
        # Save the cubes
        for stoke in tqdm(stokes, desc='Writing cubes', disable=(not verbose)):
            writecube(stoke_dict[stoke],
                      new_beam,
                      stoke,
                      field,
                      outdir,
                      verbose=verbose)

    if verbose:
        print('Done!')


def cli():
    """Command-line interface
    """
    import argparse

    # Help string to be shown using the -h option
    descStr = """
    Produce common resolution cubes for QUOCKA data.

    Combines seperate cubes per band into single cube.
    Make sure to run makecube.py first!

    """

    # Parse the command line options
    parser = argparse.ArgumentParser(description=descStr,
                                     formatter_class=argparse.RawTextHelpFormatter)

    parser.add_argument(
        'datadir',
        metavar='datadir',
        type=str,
        help='Directory containing a single QUOCKA field images.')

    parser.add_argument(
        'field',
        metavar='field',
        type=str,
        help='QUOCKA field name.')

    parser.add_argument(
        '-o',
        '--outdir',
        dest='outdir',
        type=str,
        default=None,
        help='(Optional) Save cubes to different directory [datadir].')

    parser.add_argument(
        '--target',
        dest='target',
        type=float,
        default=None,
        help='Target resoltion (circular beam, BMAJ) in arcmin [None].')

    parser.add_argument(
        "-v",
        "--verbose",
        dest="verbose",
        action="store_true",
        help="verbose output [False].")

    parser.add_argument(
        "-d",
        "--dryrun",
        dest="dryrun",
        action="store_true",
        help="Compute common beam and stop [False].")

    parser.add_argument(
        "--debug",
        dest="debug",
        action="store_true",
        help="Show debugging plots [False].")

    parser.add_argument(
        "-t",
        "--tolerance",
        dest="tolerance",
        type=float,
        default=0.0001,
        help="tolerance for radio_beam.commonbeam.")

    parser.add_argument(
        "-e",
        "--epsilon",
        dest="epsilon",
        type=float,
        default=0.0005,
        help="epsilon for radio_beam.commonbeam.")

    parser.add_argument(
        "-n",
        "--nsamps",
        dest="nsamps",
        type=int,
        default=200,
        help="nsamps for radio_beam.commonbeam.")

    group = parser.add_mutually_exclusive_group()

    group.add_argument("--ncores", dest="n_cores", default=1,
                       type=int, help="Number of processes (uses multiprocessing).")
    group.add_argument("--mpi", dest="mpi", default=False,
                       action="store_true", help="Run with MPI.")

    args = parser.parse_args()

    pool = schwimmbad.choose_pool(mpi=args.mpi, processes=args.n_cores)
    if args.mpi:
        if not pool.is_master():
            pool.wait()
            sys.exit(0)

    # make it so we can use imap in serial and mpi mode
    if not isinstance(pool, schwimmbad.MultiPool):
        pool.imap = pool.map

    verbose = args.verbose

    main(pool, args, verbose=verbose)
    pool.close()


if __name__ == "__main__":
    cli()