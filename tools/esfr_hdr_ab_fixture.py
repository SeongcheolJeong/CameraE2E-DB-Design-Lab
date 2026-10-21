"""Single-exposure synthetic normal/DCG camera inputs, separate from IQ fitting.

Both DCG readouts share ONE photoelectron draw. This is a controlled numerical
camera, never a vendor HDR reconstruction or a real sensor characterization.
"""
from copy import deepcopy
import numpy as np
from scipy.ndimage import gaussian_filter
from camera_platform.esfr_engine import bilinear_isp

READOUT = {'full_well_e':20000., 'low_gain_read_noise_e':12.,
           'high_gain_read_noise_e':2., 'high_gain_saturation_e':5000.,
           'high_gain_transition_e':[3800.,4500.], 'adc_bits':12,
           'black_dn':64., 'white_dn':4095.}


def declaration(mode):
    if mode not in ('normal','hdr'):
        raise ValueError('Unknown synthetic acquisition mode')
    return {'method':'single_exposure_shared_photoelectron_dcg_v1', 'mode':mode,
        'scope':'generic synthetic readout assumptions, not measured camera parameters',
        'private_color_blur_truth_uploaded':False, 'readout_assumptions':deepcopy(READOUT),
        'shared_shot_sample_between_gain_reads':True, 'multiple_exposures':False,
        'actual_isp_tone':'identity' if mode=='normal' else 'fixed_luma_divide_0.65_plus_0.35_luma',
        'automatic_per_frame_normalization':False, 'physical_validation':'not_established'}


def readout(photoelectrons, rng, mode):
    q=np.asarray(photoelectrons,dtype=float)
    if mode not in ('normal','hdr') or not np.isfinite(q).all() or np.any(q<0):
        raise ValueError('Readout requires finite nonnegative photoelectrons and explicit mode')
    span=READOUT['white_dn']-READOUT['black_dn']
    low_step=READOUT['full_well_e']/span
    low=np.rint(np.clip(q+rng.normal(0,READOUT['low_gain_read_noise_e'],q.shape),0,READOUT['full_well_e'])/low_step)*low_step
    if mode=='normal':
        return low
    high_step=READOUT['high_gain_saturation_e']/span
    high=np.rint(np.clip(q+rng.normal(0,READOUT['high_gain_read_noise_e'],q.shape),0,READOUT['high_gain_saturation_e'])/high_step)*high_step
    lower,upper=READOUT['high_gain_transition_e']
    weight=np.clip((upper-low)/(upper-lower),0,1)
    return weight*high+(1-weight)*low


def capture(scene, seed, mode, sigma=1.4):
    rgb=np.asarray(scene,dtype=np.float32)
    if rgb.ndim!=3 or rgb.shape[-1]!=3 or not np.isfinite(rgb).all():
        raise ValueError('A finite native linear RGB scene is required')
    blur=gaussian_filter(rgb,sigma=(sigma,sigma,0),mode='reflect',truncate=5)
    mosaic=np.empty(rgb.shape[:2],dtype=np.float32)
    for r,c,ch in ((0,0,0),(0,1,1),(1,0,1),(1,1,2)):
        mosaic[r::2,c::2]=blur[r::2,c::2,ch]
    rng=np.random.default_rng(seed)
    # The same accumulated charge feeds high/low conversions in this exposure.
    photons=rng.poisson(np.clip(mosaic,0,1)*READOUT['full_well_e'])
    electrons=readout(photons,rng,mode)
    raw=np.rint(READOUT['black_dn']+electrons/READOUT['full_well_e']*(READOUT['white_dn']-READOUT['black_dn'])).astype('<u2')
    levels={ch:{'black_dn':64.,'white_dn':4095.} for ch in ('R','Gr','Gb','B')}
    linear=bilinear_isp(raw,'RGGB',(0,0),levels)
    if mode=='hdr':
        luma=np.clip(linear@np.array([.2126,.7152,.0722]),0,1)
        linear=linear/(.65+.35*luma[...,None])
    isp=np.rint(np.clip(linear,0,1)*65535).astype(np.uint16)
    return raw,isp
