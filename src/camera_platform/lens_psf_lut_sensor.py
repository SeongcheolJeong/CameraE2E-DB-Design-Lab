"""Lens LUT → fine optical image → receiving area → four-site electron rate.

This is a local field adapter. A caller supplies absolute lens radiometry and
QE explicitly; a unit-normalized PSF never supplies missing photon throughput.
The returned rates can enter existing capture_electrons/readout/ISP operators.
"""
from __future__ import annotations

import numpy as np

from .lens_psf_lut import apply_local_radiance, interpolate


def evaluate_local_sensor_rate(document, density, photons, *, uv, pixel_pitch_um,
        output_shape_row_col, radiometry, absolute_qe, spectral_bin_width_nm,
        cfa_origin_row_col=(0,0)):
    spec=document['specification'];waves=spec['wavelength_nm'];step=spec['optical_sample_um']
    source=document['pinned_source']['lens_source']
    expected_prescription=source.get('applied_prescription_sha256',source.get('prescription_sha256'))
    if (not expected_prescription or radiometry.get('lens_revision_id')!=source['revision_id']
            or radiometry.get('applied_prescription_sha256')!=expected_prescription
            or radiometry.get('wavelength_nm')!=waves):
        raise ValueError('Absolute radiometry must match the LUT lens revision, applied prescription and wavelengths')
    a=np.asarray(photons,float);qe=np.asarray(absolute_qe,float)
    widths=np.asarray(spectral_bin_width_nm,float)
    illumination=np.asarray(radiometry.get('irradiance_per_radiance_sr'),float)
    if (a.ndim!=3 or a.shape[2]!=len(waves) or not np.isfinite(a).all() or a.min()<0
            or qe.shape!=(len(waves),4) or not np.isfinite(qe).all() or qe.min()<0 or qe.max()>1
            or widths.shape!=(len(waves),) or not np.isfinite(widths).all() or widths.min()<=0
            or illumination.shape!=(len(waves),) or not np.isfinite(illumination).all()
            or illumination.min()<=0):
        raise ValueError('Declare finite radiance, four-site absolute QE, nm widths and absolute E/L in sr')
    if (type(pixel_pitch_um) not in (float,int) or not np.isfinite(pixel_pitch_um)
            or not .2<=pixel_pitch_um<=20 or pixel_pitch_um<2*step):
        raise ValueError('Receiving pitch requires at least2 optical quadrature samples')
    if (len(output_shape_row_col)!=2 or any(type(n) is not int or not 1<=n<=2048 for n in output_shape_row_col)
            or len(cfa_origin_row_col)!=2 or any(type(n) is not int for n in cfa_origin_row_col)):
        raise ValueError('Explicit bounded output shape and integer CFA origin required')
    for count,out in zip(a.shape[:2],output_shape_row_col):
        if count*step/2+1e-10 < out*pixel_pitch_um/2 + spec['optical_size']*step/2:
            raise ValueError('Fine-grid radiance lacks the optical and receiving-area halo')
    def overlaps(count,out):
        fine=(np.arange(count+1)-count/2)*step
        receiving=(np.arange(out+1)-out/2)*pixel_pitch_um
        return np.maximum(0,np.minimum(receiving[1:,None],fine[None,1:])-
                            np.maximum(receiving[:-1,None],fine[None,:-1]))
    wy,wx=[overlaps(count,out) for count,out in zip(a.shape[:2],output_shape_row_col)]
    planes=np.zeros((*output_shape_row_col,4));sources=[]
    for wi,wave in enumerate(waves):
        psf,meta=interpolate(document,density,uv,wave)
        fine=apply_local_radiance(a[...,wi],psf,step)
        # overlap matrices integrate um²; convert receiving area to m² once.
        incident=(wy@fine@wx.T)*1e-12*illumination[wi]*widths[wi]
        planes+=incident[...,None]*qe[wi]
        sources.append(meta)
    rr,cc=np.indices(output_shape_row_col)
    sites=np.array([[0,1],[2,3]])[(rr+cfa_origin_row_col[0])%2,(cc+cfa_origin_row_col[1])%2]
    mosaic=np.take_along_axis(planes,sites[...,None],axis=-1)[...,0]
    return dict(mean_electron_rate=mosaic,planes_electron_rate=planes,metadata=dict(
        unit='electrons/s',source_input_unit='photons/s/m2/sr/nm',lut_id=document['lut_id'],
        lens_revision_id=source['revision_id'],applied_prescription_sha256=expected_prescription,
        optical_sample_um=step,pixel_pitch_um=pixel_pitch_um,pixel_aperture_applications=1,
        radiometry=radiometry,spectral_bin_width_nm=widths.tolist(),cfa='RGGB',
        cfa_origin_row_col=list(cfa_origin_row_col),field_interpolation=sources,
        ray_tracing_calls=0,field_approximation='local_stationary_optical_response',
        sensor_angular_response='caller_supplied_effective_absolute_QE'))
