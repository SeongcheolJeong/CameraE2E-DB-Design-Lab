"""Full rear-disk quadrature with denser radial sampling near the optical axis.

The enclosing disk is unchanged. Exact annular-area weights compensate the
nonuniform radii. Reuse the Factory ray/intersection/pass operator with a local
sampler binding; never mutate the Factory's process-global functions.
"""
from __future__ import annotations
import copy,time,types
import numpy as np
from . import lens_factory as factory,db_design_radiometry as dense

PROFILE='camera_lut_full_disk_companded_radiometry_v1'


def sampling(data,refinement):
    nr=data['sampling']['polar_rings']*refinement
    na=data['sampling']['azimuth_samples']*refinement
    rear=data['surfaces'][-1]
    # The Factory computes the existing plane placement and its support proof.
    _,_,_,distance=factory._sampling(data,1)
    positions=[];directions=[];weights=[]
    for height in data['image_height_mm']:
        bound=max(height,rear['semi_diameter_mm'])
        rings=nr*na if height==0 else nr
        sectors=1 if height==0 else na
        edges=(np.arange(rings+1)/rings)**2
        radii=np.sqrt((edges[:-1]**2+edges[1:]**2)/2)*bound
        angle=2*np.pi*(np.arange(sectors)+.5)/sectors if sectors>1 else np.zeros(1)
        xy=np.stack(np.broadcast_arrays(radii[:,None]*np.cos(angle),radii[:,None]*np.sin(angle)),axis=-1).reshape(-1,2)
        delta=np.column_stack((xy[:,0]-height,xy[:,1],np.full(len(xy),-distance)))
        direction=delta/np.linalg.norm(delta,axis=1)[:,None]
        area=np.repeat(np.diff(edges**2)/sectors,sectors)*np.pi*bound**2
        positions.append(xy);directions.append(direction)
        weights.append(area*direction[:,2]**4/distance**2)
    return np.asarray(positions),np.asarray(directions),np.asarray(weights),distance


def characterize(request):
    data=factory.validate_request(request)
    if data['schema']!='lens_prescription_v4' or len(data['wavelength_nm'])>2 or len(data['image_height_mm'])!=2:
        raise ValueError('Companded radiometry requires the two-field two-band v4 contract')
    backend=factory._backend();paths=factory._paths(data,backend)
    _,_,_,distance=factory._sampling(data,1)
    support=factory._angular_support(data,distance)
    if support['certificate'] is not None:raise ValueError('This adapter uses the original full rear disk')
    theta=np.linspace(0,support['maximum_sample_angle_deg'],data['sampling']['theta_bins']+1)
    # Identical tracing, surface checks, transmission and angle accumulation;
    # only the supplied numerical quadrature changes.
    namespace={**factory._pass.__globals__,'_sampling':sampling}
    run=types.FunctionType(factory._pass.__code__,namespace,name='companded_factory_pass')
    started=time.monotonic();deadline=started+600.;attempts=[]
    for rings,sectors in [(128,64),(256,64),(512,64)]:
        data['sampling'].update(polar_rings=rings,azimuth_samples=sectors)
        try:
            coarse=run(data,backend,paths,theta,1,deadline)
            fine=run(data,backend,paths,theta,2,deadline)
        except factory.LensFactoryError as exc:
            # The legacy exception constructor is not process-pickle-safe.
            # Preserve the failure reason in an ordinary exception across jobs.
            raise ValueError('Full-disk radiometry failed within its 600s budget: '+str(exc)) from exc
        agreement=dense._agreement(data,coarse,fine)
        attempts.append(dict(coarse_grid=[rings,sectors],**agreement))
        if agreement['observed_two_level_agreement']:break
    summary=dict(schema=PROFILE,status='PROPOSED' if agreement['observed_two_level_agreement'] else 'REFUSED',
        factory_id=PROFILE,normalized_request=copy.deepcopy(request),actual_prescription=data,
        elapsed_seconds=time.monotonic()-started,convergence=agreement,refinement_attempts=attempts,
        physical_validation='not_established',sampling=dict(measure='complete_rear_disk_exact_annular_area',
            radial_edges='(i/N)^2',support_truncation=False,plane_distance_mm=distance,
            maximum_seconds=600.,maximum_grid=[1024,128]),
        irradiance_per_radiance_sr=fine['irradiance_per_radiance_sr'].tolist())
    arrays={k:fine[k] for k in ('irradiance_per_radiance_sr','image_direction_cosines')}
    arrays['directional_conditional_weights']=dense._conditional(fine)
    return dict(summary=summary,arrays=arrays)
