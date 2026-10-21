"""Exact-node 3D absorption adapter; no 2D promotion or spectral extrapolation.

Input is optical-image photon irradiance before CFA/QE/pixel collection. This
replaces the spectral response, never multiplies an existing sensor QE. The
periodic plane-wave solution is a local-uniform-illumination approximation,
not a spatial crosstalk kernel. Spatial propagation remains the optical engine's
responsibility; a spatial absorption kernel requires a separate source study.
"""
from __future__ import annotations
import numpy as np
from .sensor_structure_job import qualify


def photoelectron_rate(samples, spec, structure, photon_irradiance, *,
                       wavelength_nm, spectral_bin_width_nm, angle_deg,
                       variant, collection_efficiency, cfa_origin=(0,0)):
    """Return native RGGB e-/s from photons/(m² s nm), at computed nodes only."""
    base,assessment=qualify(samples,spec)
    if spec['dimension']!=3 or not assessment['camera_response_eligible']:
        raise ValueError('Qualified 3D absorption required; 2D cannot produce camera QE')
    data=np.asarray(photon_irradiance,dtype=float)
    waves=np.asarray(wavelength_nm,dtype=float); bins=np.asarray(spectral_bin_width_nm,dtype=float)
    ce=np.asarray(collection_efficiency,dtype=float)
    if ce.ndim==0:ce=np.full(4,float(ce))
    if ce.shape!=(4,) or not np.isfinite(ce).all() or np.any((ce<0)|(ce>1)):
        raise ValueError('Declare four finite collection efficiencies between zero and one')
    if data.ndim!=3 or data.shape[-1]!=len(waves) or bins.shape!=waves.shape or not len(waves) or len(set(waves))!=len(waves):
        raise ValueError('Irradiance must have row/column/wavelength axes and explicit spectral widths')
    if not np.isfinite(data).all() or np.any(data<0) or not np.isfinite(bins).all() or np.any(bins<=0):
        raise ValueError('Finite positive spectral widths and nonnegative irradiance required')
    if len(cfa_origin)!=2 or any(isinstance(x,bool) or int(x)!=x or x<0 for x in cfa_origin):
        raise ValueError('Native nonnegative integer CFA origin required')
    response=[];ids=[]
    for wave in waves:
        group=[v for v in base if v['variant']==variant and v['wavelength_nm']==wave
               and v['angle_deg']==angle_deg and v['resolution']==max(spec['resolutions'])]
        if len(group)!=2 or {v['polarization'] for v in group}!={'s','p'}:
            raise ValueError('Exact wavelength/direction node absent; interpolation is not qualified')
        if any(v.get('structure_revision')!=structure['revision'] for v in group):
            raise ValueError('Response belongs to a different structure revision')
        # Four pixels share the incident unit cell: convert cell fraction to
        # per-pixel optical response before applying collection and pixel area.
        response.append(4*np.mean([v['pixel_absorption'] for v in group],axis=0)*ce)
        ids.extend(v['task_id'] for v in group)
    response=np.asarray(response)
    area=(structure['geometry']['pitch_um']*1e-6)**2
    planes=np.einsum('hwk,kc,k->hwc',data,response,bins)*area
    row,col=np.indices(data.shape[:2]);site=((row+cfa_origin[0])%2)*2+(col+cfa_origin[1])%2
    mosaic=np.take_along_axis(planes,site[...,None],axis=-1)[...,0]
    return dict(mean_electron_rate=mosaic,planes_electron_rate=planes,
        provenance=dict(structure_revision=structure['revision'],task_ids=ids,
            collection_efficiency=ce.tolist(),collection_status='explicit_assumption',
            response_kind='absolute_response_replaces_QE',angle_deg=angle_deg,
            angular_scope='exact phi=0 computed nodes; no pupil interpolation',
            spatial_scope='locally uniform periodic response; no spatial crosstalk kernel',
            measured_accuracy=False))


def capture(samples, spec, structure, photon_irradiance, *, electrical_model,
            exposure_s, seed, gain=1., **response_settings):
    """Connect qualified exact-node response to existing integer RAW + fixed ISP."""
    from . import electrical,isp
    rate=photoelectron_rate(samples,spec,structure,photon_irradiance,**response_settings)
    rows,cols=rate['mean_electron_rate'].shape
    origin=list(response_settings.get('cfa_origin',(0,0)))
    geometry=dict(full_sensor_shape=[rows+origin[0],cols+origin[1]],crop_origin=origin)
    acquired=electrical.capture_electrons(rate['mean_electron_rate']*exposure_s,exposure_s,
        electrical_model,analog_gain=gain,temperature_c=25.,geometry=geometry,noise_enabled=True,seed=seed)
    adc=electrical_model['adc']
    raw_format=dict(cfa='RGGB',bit_depth=adc['bit_depth'],black_level_dn=adc['black_level_dn'],
        response_type='linear',packing='unpacked',layout='bayer',hdr_streams=1)
    processed=isp.process_raw(acquired['arrays']['raw_dn'],raw_format,isp.default_profile(),
        geometry={'kind':'native_sensor',**geometry})
    return dict(response=rate,acquisition=acquired,isp=processed)
