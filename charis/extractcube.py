#!/usr/bin/env python

#########################################################################
# A provisional routine for actually producing and returning data cubes.
#########################################################################

import copy
import glob
import json
import logging
import multiprocessing
import os
import re

import numpy as np
from astropy.io import fits
from astropy.stats import mad_std, sigma_clipped_stats

import charis
from charis import instruments, primitives, utr
from charis.image import Image
from charis.image.image_geometry import resample_image_cube
from charis.tools import (
    fit_background,
    sph_ifs_correct_spectral_xtalk,
    sph_ifs_fix_badpix,
)

log = logging.getLogger('main')


def getcube(dit=None, read_idx=[1, None], filename=None, calibdir=None,
            bgsub=True, bgpath=None, bg_scaling_without_mask=False,
            mask=True,
            gain=2, nonlinear_threshold=40000, noisefac=0,
            saveramp=False, R=30,
            individual_dits=False,
            method='lstsq', refine=True, crosstalk_scale=0.8,
            dc_xtalk_correction=False,
            linear_wavelength=False,
            suppressrn=True, fitshift=True,
            flatfield=True, smoothandmask=True,
            minpct=70, fitbkgnd=True, saveresid=False,
            maxcpus=multiprocessing.cpu_count(),
            instrument=None, resample=True,
            outdir="./",
            static_calibdir=None,
            verbose=True):
    """Provisional routine getcube.  Construct and return a data cube
    from a set of reads.

    Inputs:
    1. filename: name of the file containing the up-the-ramp reads.
                 Should include the full path/to/file.
    2. calibdir: name of the directory containing the calibration files.

    Optional inputs:
    1. read_idx: list of two numbers, the first and last reads to use in
                 the up-the-ramp combination.  Default [2, None], i.e.,
                 discard the first read and use all of the rest.
    2. bgsub:    Subtract the file background.fits in calibdir?  Default
                 True.
    3. mask:     Apply the bad pixel mask mask.fits in the directory
                 calibdir?  Strongly recommended.  Default True.
    4. gain:     Detector gain, used to compute shot noise.  Default 2.
    5. noisefac: Extra factor of noise to account for imperfect lenslet
                 models:
                 variance = readnoise + shotnoise + noisefac*countrate
                 Default zero, values of around 0.05 should give
                 reduced chi squared values of around 1 in the fit.
    6. R:        integer, approximate resolution lam/delta(lam) of the
                 extracted data cube.  Resolutions higher than ~25 or 30
                 are comparable to or finer than the pixel sampling and
                 are not recommended--there is very strong covariance.
                 Default 30.
    7. method:   string, method used to extract data cube.  Should be
                 either 'lstsq' for a least-squares extraction or
                 'optext' for a quasi-optimal extraction.  Default
                 'lstsq'
    8. refine:   Fit the data cube twice to account for nearest neighbor
                 crosstalk?  Approximately doubles runtime.  This option
                 also enables read noise suppression (below).  Default
                 True
    9. suppress_rn: Remove correlated read noise between channels using
                 the residuals of the 50% least illuminated pixels?
                 Default True.
    10. fitshift: Fit a subpixel shift in the psflet locations across
                 the detector?  Recommended except for quicklook.  Cost
                 is modest compared to cube extraction

    Returns:
    1. datacube: an instance of the Image class containing the data cube.

    Steps performed:

    1. Up-the-ramp combination.  As of yet no special read noise
    suppression (just a channel-by-channel correction of the reference
    voltages).  Do a full nonlinear fit for high count pixels, and
    remove an exponential decay of the reference voltage in the first
    read.
    2. Subtraction of thermal background from file in calibration
    directory.
    3. Application of hot pixel mask (as zero inverse variances).
    4. Load calibration files from calibration directory for the data
    cube extraction, optionally fit for subpixel shifts across the
    detector.
    5. Extract the data cube.

    Notes for now: the quasi-optimal extraction isn't really an
    optimal extraction.  Every lenslet's spectrum natively samples a
    different set of wavelengths, so right now they are all simply
    interpolated onto the same wavelength array.  This is certainly
    not optimal, but I don't see any particularly good alternatives
    (other than maybe convolving to a lower, but uniform, resolution).
    The lstsq extraction can include all errors and covariances.
    Errors are included (the diagonal of the covariance matrix), but
    the full covariance matrix is currently discarded.

    """

    ################################################################
    # Initiate the header with critical data about the observation.
    # Then add basic information about the calibration data used to
    # extract a cube.
    ################################################################

    version = charis.__version__

    header = fits.getheader(filename)
    instrument, _, _ = instruments.instrument_from_data(
        header, calibration=False, static_calibdir=static_calibdir,
        interactive=False)

    if instrument.instrument_name == 'CHARIS':
        header = utr.metadata(filename, version=version)
    elif instrument.instrument_name == 'SPHERE':
        header = utr.metadata_SPHERE(filename, dit_idx=dit, version=version)
    else:
        raise ValueError("Only CHARIS and SPHERE instruments implemented.")

    try:
        calhead = fits.getheader(os.path.join(calibdir, 'cal_params.fits'))
        header.append(('comment', ''), end=True)
        header.append(('comment', '*' * 60), end=True)
        header.append(('comment', '*' * 21 + ' Calibration Data ' + '*' * 21), end=True)
        header.append(('comment', '*' * 60), end=True)
        header.append(('comment', ''), end=True)
        for key in calhead:
            header.append((key, calhead[key], calhead.comments[key]), end=True)
    except Exception:
        log.warn('Unable to append calibration parameters to FITS header.')

    ################################################################
    # Read in file and return an instance of the Image class with the
    # up-the-ramp combination of reads.  Subtract the thermal
    # background and apply a bad pixel mask.
    ################################################################

    calibration_path_instrument = instrument.calibration_path_instrument
    calibration_path_mode = instrument.calibration_path_mode

    maxcpus = min(maxcpus, multiprocessing.cpu_count())

    maskarr = None
    if mask:
        maskarr = fits.getdata(
            os.path.join(calibration_path_instrument, 'mask.fits'))
        bpm = np.logical_not(maskarr.astype('bool')).astype('int')

    if instrument.instrument_name == 'CHARIS':
        inImage = utr.calcramp(filename=filename, mask=maskarr, read_idx=read_idx,
                               header=header, gain=gain, noisefac=noisefac,
                               maxcpus=maxcpus)
        file_ending = ''

    elif instrument.instrument_name == 'SPHERE':
        data = fits.getdata(filename).astype('float64')
        readnoise = 6

        if maskarr is None:
            maskarr = np.ones((data.shape[-2], data.shape[-2]))
        if flatfield:
            pixelflat = fits.getdata(
                os.path.join(calibration_path_instrument, 'pixelflat.fits'))
            good_pixels = np.logical_and(pixelflat > 0.9, pixelflat < 1.1)
            maskarr[~good_pixels] = 0
        bpm = np.logical_not(maskarr.astype('bool')).astype('int')

        if nonlinear_threshold is not None:
            nonlinear = data > nonlinear_threshold
        else:
            nonlinear = np.zeros([data.shape[-2], data.shape[-1]]).astype('bool')

        if data.ndim == 3:
            if not individual_dits:
                ndit = len(data)
                data = np.mean(data, axis=0)
                file_ending = ''
            else:
                ndit = 1
                data = data[dit]
                file_ending = '_DIT_{:03d}'.format(dit)
        elif data.ndim == 2:
            file_ending = ''
            ndit = 1
        ivar = 1. / (abs(data) * gain * ndit + (data * noisefac)**2 + readnoise**2 * ndit)

        inImage = Image(data=data, ivar=ivar, header=header,
                        instrument_name=instrument.instrument_name)

    if bgsub:
        if bgpath is not None:
            hdulist = fits.open(bgpath)
            bg = hdulist[0].data
            if bg is None:
                bg = hdulist[1].data
            hdulist.close()
            if len(bg.shape) == 3:
                bg = np.median(bg, axis=0)

        if instrument.instrument_name == 'SPHERE':
            # Region to match background counts for SPHERE IFS
            bgscalemask = fits.getdata(
                os.path.join(calibration_path_instrument, 'background_scaling_mask.fits')).astype('bool')
            if bgpath is not None:
                if bg_scaling_without_mask:
                    norm = inImage.data[maskarr == 1] / bg[maskarr == 1]
                else:
                    norm = inImage.data[bgscalemask] / bg[bgscalemask]

                norm = norm[np.isfinite(norm)].flatten()
                _, norm, _ = sigma_clipped_stats(
                    norm, sigma=2., sigma_lower=None, sigma_upper=None,
                    maxiters=5, cenfunc='median', stdfunc='std',
                    std_ddof=0)
                bg *= norm
                print("Background subtracted")
            else:
                print('Fitting bg')
                components = fits.getdata(
                    os.path.join(calibration_path_instrument, 'background_template.fits'))
                bg, bg_coef = fit_background(
                    image=inImage.data, components=components, bgmask=bgscalemask, outlier_percentiles=[2, 98])
                print("BG coefficients: {}".format(bg_coef))
            inImage.data -= bg

    if instrument.instrument_name == 'SPHERE':
        inImage.data = sph_ifs_fix_badpix(img=inImage.data, bpm=bpm)
        inImage.ivar = sph_ifs_fix_badpix(img=inImage.ivar, bpm=bpm)
        inImage.ivar[bpm.astype('bool')] = 0  # inImage.ivar[bpm.astype('bool')] * 1e-20
    if dc_xtalk_correction:
        # bpm = np.logical_not(
        #     fits.getdata(os.path.join(calibdir, 'mask.fits')).astype('bool'))
        # if instrument.instrument_name == 'SPHERE' and flatfield:
        #     pixelflat = fits.getdata(os.path.join(calibdir, 'pixelflat.fits'))
        #     good_pixels = pixelflat > 0.
        #     inImage.data[good_pixels] /= pixelflat[good_pixels]
        #     inImage.ivar[good_pixels] *= pixelflat[good_pixels]**2
        #     print("Divide by flat for good pixels")
        # print('Fixing bad pixels')

        inImage.data, convolved_image = sph_ifs_correct_spectral_xtalk(
            inImage.data, mask=~(maskarr.astype('bool')))
        fits.writeto(
            re.sub('.fits', '_convolved_image' + file_ending + '.fits',
                   os.path.join(outdir, os.path.basename(filename))),
            convolved_image, overwrite=True)

    header['bgsub'] = (bgsub, 'Subtract background count rate from a dark?')
    if saveramp:
        inImage.write(re.sub('.fits', f'_ramp{file_ending}.fits', os.path.join(
            outdir, os.path.basename(filename))))
        if instrument.instrument_name == 'SPHERE':
            fits.writeto(re.sub('.fits', f'_bg{file_ending}.fits', os.path.join(
                outdir, os.path.basename(filename))), bg, overwrite=True)

    ################################################################
    # Read in necessary calibration files and extract the data cube.
    # Optionally fit for a position-dependent offset
    ################################################################

    header.append(('comment', ''), end=True)
    header.append(('comment', '*' * 60), end=True)
    header.append(('comment', '*' * 22 + ' Cube Extraction ' + '*' * 21), end=True)
    header.append(('comment', '*' * 60), end=True)
    header.append(('comment', ''), end=True)

    if flatfield:
        lensletflat = fits.getdata(
            os.path.join(
                calibration_path_mode, 'lensletflat.fits')).astype('float64')
        pixelflat = fits.getdata(
            os.path.join(calibration_path_instrument, 'pixelflat.fits'))
        good_pixel_mask = np.logical_not(bpm.astype('bool'))
    else:
        lensletflat = None
        good_pixel_mask = np.ones_like(inImage.data).astype('bool')

    header['flatfld'] = (flatfield, 'Flatfield the detector and lenslet array?')
    datacube = None

    if method == 'lstsq' or suppressrn or refine:
        try:
            keyfile = fits.open(os.path.join(calibdir, 'polychromekeyR%d.fits' % (R)))
            R2 = R
        except IOError:
            keyfilenames = glob.glob(calibdir + '*polychromekeyR*.fits')
            if len(keyfilenames) == 0:
                raise IOError("No key file found in " + calibdir)
            R2 = int(re.sub('.*keyR', '', re.sub('.fits', '', keyfilenames[0])))
            keyfile = fits.open(os.path.join(calibdir, 'polychromekeyR%d.fits' % (R2)))
            if verbose:
                print("Warning: calibration files not found at requested resolution of R = %d" % (R))
                print("Found files at R = %d, using these instead." % (R2))

        if fitshift:
            try:
                psflets = np.load(os.path.join(calibdir, 'polychromefullR%d.npy' % (R2)))
            except FileNotFoundError:
                if verbose:
                    print("Oversampled PSFlets for fitshift not found, reverting to not shifting.")
                fitshift = False
        if fitshift:
            offsets = instrument.offsets
            # Unless this is CHARIS, default to a single offset image-wide
            dx = inImage.data.shape[0]
            if instrument.instrument_name == 'CHARIS':
                dx = int(dx / 16)
            try:
                psflets = primitives.calc_offset(
                    psflets, inImage, offsets, dx=dx, maxcpus=maxcpus)
            except Exception as e:
                if verbose:
                    print('Fit shift failed. Continuing without fitting shift.')
                    print(e)
                fitshift = False
        if not fitshift:
            psflets = fits.getdata(os.path.join(calibdir, 'polychromeR%d.fits' % (R2)))

        header.append(('fitshift', fitshift, 'Fit a subpixel shift in PSFlet locations?'), end=True)
        lam_midpts = keyfile[0].data
        lam_psflets = lam_midpts.copy()
        x = keyfile[1].data
        y = keyfile[2].data
        good = keyfile[3].data
        keyfile.close()

        if flatfield:
            # Only apply flat field to pixels with non-anomalous flat field values
            psflets[:, good_pixel_mask] = psflets[:, good_pixel_mask] * pixelflat[good_pixel_mask]

        ############################################################
        # Do an initial least-squares fit to remove correlated read
        # noise if method = optext and suppressrn = True
        ############################################################

        if method != 'lstsq':
            residuals, coefs = primitives.fit_spectra(
                inImage, psflets, lam_midpts, x, y, good,
                instrument=instrument,
                header=inImage.header, lensletflat=lensletflat, refine=refine,
                suppressreadnoise=suppressrn, smoothandmask=smoothandmask,
                minpct=minpct, fitbkgnd=fitbkgnd, maxcpus=maxcpus,
                return_corrnoise=True)
            if suppressrn:
                corrnoise = residuals
                inImage.data -= corrnoise
            elif refine and not dc_xtalk_correction:
                # This is to remove crosstalk: we remove the
                # contribution of each lenslet to its nearest
                # neighbors, scaled by crosstalk_scale.  In this case
                # corrnoise actually refers to the residuals.  We take
                # a mixture of the residuals and the data, with the
                # mixture scaled by crosstalk_scale; the components
                # from the coefficients will be added back in later.
                if crosstalk_scale > 0:
                    coefs *= crosstalk_scale
                    inImage.data += crosstalk_scale*(residuals - inImage.data)
                print("Crosstalk scale: {}".format(crosstalk_scale))
        else:
            result = primitives.fit_spectra(
                inImage, psflets, lam_midpts, x, y, good,
                instrument=instrument,
                header=inImage.header, lensletflat=lensletflat, refine=refine,
                suppressreadnoise=suppressrn, smoothandmask=smoothandmask,
                minpct=minpct, fitbkgnd=fitbkgnd, returnresid=saveresid,
                maxcpus=maxcpus)
            if saveresid:
                datacube, resid = result
                resid.write(
                    re.sub('.fits', '_residuals' + file_ending + '.fits',
                           os.path.join(outdir, os.path.basename(filename))))
                # mask_resid = inImage.data > 0.
                # relative_resid = resid.data.copy()
                # relative_resid[mask_resid] /= inImage.data[mask_resid]
                # relative_resid[~mask_resid] = np.nan
                # relative_resid[np.abs(relative_resid) > mad_std(relative_resid, ignore_nan=True) * 6] = np.nan
                # fits.writeto(
                #     # re.sub('.*/', '',
                #     re.sub('.fits', '_resid_relative' + file_ending + '.fits',
                #            os.path.join(outdir, os.path.basename(filename))),
                #     relative_resid, overwrite=True)
            else:
                datacube = result

    if method == 'optext' or method == 'apphot3' or method == 'apphot5':
        loc = primitives.PSFLets(load=True, infiledir=calibdir)
        # try:
        #     lam_midpts = fits.open(os.path.join(calibdir, '/polychromekeyR%d.fits' % (R)))[0].data
        #     R2 = R
        # except IOError:
        # keyfilenames = glob.glob(calibdir + '*polychromekeyR*.fits')
        # if len(keyfilenames) == 0:
        #     raise IOError("No key file found in " + calibdir)
        # R2 = int(re.sub('.*keyR', '', re.sub('.fits', '', keyfilenames[0])))
        # lam = fits.open(keyfilenames[0])[0].data
        if linear_wavelength:
            lam_midpts = np.linspace(
                instrument.wavelength_range[0].value,
                instrument.wavelength_range[1].value,
                39)
        else:
            lam_midpts, _ = instrument.wavelengths(
                instrument.wavelength_range[0].value,
                instrument.wavelength_range[1].value,
                R)

        try:
            sig = fits.getdata(os.path.join(calibdir, 'PSFwidths.fits'))
        except IOError:
            print("Failed to load PSFwidths.fits. Defaulting to standard PSF width.")
            sig = 0.7

        if method == 'apphot3' or method == 'apphot5':
            sig = 1e10
            if method == 'apphot3':
                delt_x = 3
            else:
                delt_x = 5
        else:
            delt_x = 5

        if flatfield:
            # if refine:
            #     # revert flatfield correction for psflets?
            #     psflets[:, good_pixel_mask] = psflets[:,
            #                                           good_pixel_mask] / pixelflat[good_pixel_mask]
            inImage.data[good_pixel_mask] = inImage.data[good_pixel_mask] / \
                pixelflat[good_pixel_mask]
            if instrument.instrument_name == 'SPHERE':
                inImage.data = sph_ifs_fix_badpix(img=inImage.data, bpm=bpm)
                inImage.ivar[good_pixel_mask] *= pixelflat[good_pixel_mask]**2
                inImage.ivar = sph_ifs_fix_badpix(img=inImage.ivar, bpm=bpm)
                inImage.ivar[bpm.astype('bool')] = 0  # inImage.ivar[bpm.astype('bool')] / 1.2

        # If we did the crosstalk correction, we need to add the model
        # spectra back in and do a modified optimal extraction.
        if refine:
            _coefs, _psflets, _lam_psflets = coefs, psflets, lam_psflets
        else:
            _coefs, _psflets, _lam_psflets = None, None, None

        datacube = primitives.optext_spectra(inImage, loc, lam_midpts,
                                             instrument=instrument, delt_x=delt_x,
                                             coefs_in=_coefs, psflets=_psflets,
                                             lampsflets=_lam_psflets,
                                             sig=sig, header=inImage.header,
                                             lensletflat=lensletflat,
                                             smoothandmask=smoothandmask,
                                             maxcpus=maxcpus)

    if datacube is None:
        raise ValueError("Datacube extraction method " + method + " not implemented.")

    ################################################################
    # Add the original header for reference as the last HDU
    ################################################################
    extrahdr = fits.getheader(filename)
    datacube.extraheader = extrahdr
    ################################################################
    # Add WCS for the cube
    # for now assume the image is centered on the cube
    # in practice, we will have to register things with
    # the satellite spots
    ################################################################

    if instrument.instrument_name == 'CHARIS':
        ydim, xdim = datacube.data[0].shape
        rot_angle = 113  # empirically determined
        utr.addWCS(datacube.header, xpix=ydim // 2, ypix=xdim // 2,
                   xpixscale=-0.0164 / 3600., ypixscale=0.0164 / 3600.,
                   extrarot=rot_angle)

    if instrument.instrument_name == 'SPHERE' and resample:
        clip_info_file = os.path.join(
            calibration_path_instrument, 'hexagon_mapping_calibration.json')
        with open(clip_info_file) as json_data:
            clip_infos = json.load(json_data)
        datacube_resampled = copy.copy(datacube)
        datacube_resampled.data = resample_image_cube(
            datacube.data, clip_infos, hexagon_size=1 / np.sqrt(3))
        datacube_resampled.ivar = resample_image_cube(
            datacube.ivar, clip_infos, hexagon_size=1 / np.sqrt(3))

        datacube.write(
            re.sub('.fits', '_cube' + file_ending + '.fits',
                   os.path.join(outdir, os.path.basename(filename))))
        datacube_resampled.write(
            re.sub('.fits', '_cube_resampled' + file_ending + '.fits',
                   os.path.join(outdir, os.path.basename(filename))))
        return datacube, datacube_resampled

    else:
        datacube.write(
            re.sub('.fits', '_cube.fits',
                   os.path.join(outdir, os.path.basename(filename))))
        return datacube
