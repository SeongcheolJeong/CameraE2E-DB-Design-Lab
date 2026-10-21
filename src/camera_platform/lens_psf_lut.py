"""Sensor-independent, source-bound scalar optical PSFs and adaptive field cells.

Intensity samples are point evaluations in 1/um². Their quadrature spacing is
not a sensor aperture. Only ``integrate_pixel`` applies a receiving pixel area.
The field coordinates are object-space gnomonic slopes, not sensor millimetres.
"""
from __future__ import annotations

import copy
import math
from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve

from .registry import canonical, digest

SCHEMA = "camera_lens_optical_psf_lut_v1"


def defaults():
    return dict(schema=SCHEMA, name="Patent lens · optical PSF LUT", lens_key="canon_p0248",
                wavelength_nm=[450., 550., 650.], half_field_deg_xy=[6., 4.],
                initial_grid_xy=[3, 3], optical_sample_um=.5, optical_size=769,
                pupil_samples=48, max_refinement_level=1, max_field_nodes=81,
                interpolation_l1_limit=.08, outer_radius_fraction=.7,
                outer_tolerance_scale=.5)


def specification(payload):
    base = defaults()
    if not isinstance(payload, dict) or set(payload) - set(base):
        raise ValueError("Use the declared Lens PSF LUT specification fields")
    s = {**base, **copy.deepcopy(payload)}
    if s['schema'] != SCHEMA or not isinstance(s['name'], str) or not 1 <= len(s['name']) <= 160:
        raise ValueError("Invalid LUT schema or name")
    from .db_design_optics import _source
    _source(s['lens_key'])
    def number(v, lo, hi):
        return type(v) in (float, int) and math.isfinite(v) and lo <= v <= hi
    w = s['wavelength_nm']
    if (not isinstance(w, list) or not 1 <= len(w) <= 31
            or any(not number(v, 450, 650) for v in w)
            or any(a >= b for a, b in zip(w, w[1:]))):
        raise ValueError("Use 1..31 increasing explicit wavelengths in450..650nm; no spectral extrapolation")
    for key, lo, hi in [('half_field_deg_xy', .01, 60), ('initial_grid_xy', 2, 10)]:
        a = s[key]
        if (not isinstance(a, list) or len(a) != 2 or any(not number(v, lo, hi) for v in a)
                or (key == 'initial_grid_xy' and any(type(v) is not int for v in a))):
            raise ValueError(key + " requires two bounded values")
    for key, lo, hi in [('optical_size', 65, 1025), ('pupil_samples', 24, 96),
                        ('max_refinement_level', 0, 5), ('max_field_nodes', 9, 2048)]:
        if type(s[key]) is not int or not lo <= s[key] <= hi:
            raise ValueError(key + " is outside the explicit resource policy")
    if s['optical_size'] % 2 != 1 or s['pupil_samples'] % 2:
        raise ValueError("Optical size must be odd; pupil samples must be even")
    for key, lo, hi in [('optical_sample_um', .05, 2), ('interpolation_l1_limit', .001, .5),
                        ('outer_radius_fraction', .1, 1), ('outer_tolerance_scale', .1, 1)]:
        if not number(s[key], lo, hi): raise ValueError(key + " is outside its range")
    initial_nodes=int(np.prod(s['initial_grid_xy'])) + int(any(n%2==0 for n in s['initial_grid_xy']))
    if initial_nodes > s['max_field_nodes']:
        raise ValueError("Initial grid exceeds the field-node budget")
    # Large libraries are disk-backed and sampled a few fields at a time.
    # This is a storage/work budget, not a claimed physical hardware limit.
    if s['max_field_nodes'] * len(w) * s['optical_size'] ** 2 * 4 > 16_000_000_000:
        raise ValueError("Decoded LUT exceeds the explicit16GB disk-backed library policy")
    return s


def intensity(pupil, wave_nm, size, spacing_um):
    """Point-sampled scalar intensity; no receiving-pixel box integration."""
    from prysm.propagation import focus_fixed_sampling
    support = np.argwhere(pupil['power'] > 0)
    extent = np.max(np.ptp(support, axis=0)) * pupil['spacing']
    if spacing_um > wave_nm * 1e-3 / (2 * extent):
        raise ValueError("Optical intensity spacing exceeds its angular-spectrum Nyquist bound")
    if size * spacing_um >= .8 * wave_nm * 1e-3 / pupil['spacing']:
        raise ValueError("Optical support approaches periodic image replicas")
    field = np.sqrt(pupil['power']) * np.exp(-2j * np.pi * pupil['opd_nm'] / wave_nm)
    focused = focus_fixed_sampling(field, pupil['spacing'], 1., wave_nm * 1e-3,
                                   spacing_um, size)
    density = (np.abs(focused) ** 2 / pupil['power'].sum() / spacing_um**2)[::-1].copy()
    if not np.isfinite(density).all() or density.sum() <= 0:
        raise ValueError("Nonfinite or empty optical intensity")
    return density


def optical_node(coarse, fine, wave, size, spacing):
    """Independent ray, direction-grid, and optical-sampling checks."""
    from . import reference_exit_pupil as ep
    span = max(np.max(np.ptp(t['q'], axis=0)) * 1.04 for t in (coarse, fine))
    minimum = math.ceil(size * spacing * span / (.8 * wave * 1e-3))
    n = max(256, 2 ** math.ceil(math.log2(minimum + 1)))
    pupils = [ep.regular_pupil(t, n) for t in (coarse, fine)]
    refined = ep.regular_pupil(fine, 2 * n)
    images = [intensity(p, wave, size, spacing) for p in [*pupils, refined]]
    dense = intensity(refined, wave, 2 * size - 1, spacing / 2)
    energies = [float(a.sum() * spacing**2) for a in images]
    energies.append(float(dense.sum() * (spacing / 2)**2))
    normalized = [a / e for a, e in zip(images, energies)]
    reference = dense[::2, ::2] / energies[-1]
    changes = [float(np.abs(a - reference).sum() * spacing**2) for a in normalized]
    opd = ep.interpolation_error(coarse, fine, wave)
    step = ep.phase_grid_step(refined, wave)
    accepted = (max(changes) <= .03 and min(energies) >= .95 and max(energies) <= 1.001
                and step <= .25 and opd['maximum_abs_waves'] <= .02)
    # Keep the declared optical grid; the finer grid qualifies its quadrature.
    density = images[-1] / energies[2]
    diag = dict(status='qualified' if accepted else 'refused', pupil_samples=[coarse['samples'],fine['samples']],
                direction_grid_samples=[n, 2*n], optical_spacing_um=[spacing, spacing/2],
                retained_energy=energies, l1_changes=changes, unwrapped_opd=opd,
                maximum_neighbor_opd_waves=step, sensor_pixel_aperture_applied=False,
                chief_aiming=[t.get('chief_aiming') for t in (coarse,fine)])
    return density.astype(np.float32), diag


class PatentSolver:
    """Reuse physical ray bundles under the declared centered rotational symmetry."""
    def __init__(self, workspace, spec):
        from . import db_design_optics as db, reference_workshop_optics as optics
        from . import prescription_psf
        self.spec = spec
        self.context = db._source(spec['lens_key'])
        self.metadata = db.source_metadata(workspace, spec['lens_key'])
        waves = sorted(set([550., *spec['wavelength_nm']]))
        # The reviewed bridge bounds requests to16 wavelengths. Validate the
        # complete material interval, then evaluate each exact configured wave
        # from that same dispersion model; no PSF spectral interpolation.
        self.request = optics._preflight(workspace, [450.,550.,650.], source_context=self.context)['psf_request']
        self.request['prescription']['wavelength_nm']=waves
        self.model = prescription_psf._model(self.request, waves.index(550.))
        self.bundles = {}
        self.trace_count = 0
        # Reject unsupported authored fields before expensive PSF generation.
        limits=bounds(spec)
        for x in (-limits[0],limits[0]):
            for y in (-limits[1],limits[1]): self.chief((x,y))

    def chief(self, uv):
        from rayoptics.raytr import trace
        from rayoptics.raytr.opticalspec import Field
        from .reference_exit_pupil import aim_at_stop
        from .prescription_psf import _validate_physical_ray
        angle = math.degrees(math.atan(math.hypot(*uv)))
        field = Field(y=angle, fov=self.model.optical_spec['fov'])
        aim_at_stop(self.model, field, 550.)
        ray, _, _ = trace.trace_base(self.model, [0., 0.], field, 550.,
                                     check_apertures=True, apply_vignetting=False)
        _validate_physical_ray(ray, self.model, self.request['prescription']['surfaces'], 'LUT chief')
        anchor = np.asarray(ray[-1][0][:2])
        if np.linalg.norm(anchor) > self.context['image_height_mm']:
            raise ValueError("Requested field exceeds the authored image height; no120-degree extrapolation")
        azimuth = math.degrees(math.atan2(uv[1], uv[0])) if math.hypot(*uv) else 90.
        a = math.radians(azimuth - 90)
        rotation = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
        return angle, anchor, azimuth, rotation

    def solve(self, uv, progress=lambda s: None):
        from . import reference_exit_pupil as ep
        angle, anchor, azimuth, rotation = self.chief(uv)
        output, diagnostics = [], []
        for wave in self.spec['wavelength_nm']:
            wi = self.request['prescription']['wavelength_nm'].index(wave)
            attempts = []
            for n in sorted({self.spec['pupil_samples'], max(64,self.spec['pupil_samples']),96}):
                traces = []
                for level in (n, 2*n):
                    key = (round(angle, 13), wave, level)
                    if key not in self.bundles:
                        progress(f'{angle:.4f}deg {wave:g}nm · physical pupil{level}')
                        self.bundles[key] = ep.trace_pupil(self.request, wi, angle, anchor, level, 90.)
                        self.trace_count += 1
                        if len(self.bundles)>48:
                            self.bundles.pop(next(iter(self.bundles)))
                    t = dict(self.bundles[key])
                    t['q'] = t['q'] @ rotation.T
                    traces.append(t)
                density, diag = optical_node(*traces, wave, self.spec['optical_size'],
                                              self.spec['optical_sample_um'])
                attempts.append(diag)
                if diag['status'] == 'qualified': break
            if diag['status'] != 'qualified':
                raise ValueError('Optical point PSF failed its unchanged numerical gates: '+canonical(attempts))
            output.append(density); diagnostics.append(dict(wavelength_nm=wave, attempts=attempts))
        return np.stack(output), dict(object_angle_deg=angle, azimuth_deg=azimuth,
            chief_sensor_xy_mm=(rotation @ anchor).tolist(), diagnostics=diagnostics)


def bounds(spec):
    return np.tan(np.radians(spec['half_field_deg_xy']))


def _weights(cell, uv):
    x0,x1,y0,y1 = cell['bounds_uv']
    x,y = uv; a=(x-x0)/(x1-x0); b=(y-y0)/(y1-y0)
    return np.array([(1-a)*(1-b),a*(1-b),(1-a)*b,a*b])


def adaptive_grid(spec, evaluate, progress=lambda s: None, *, array_store=None):
    """Qualify bilinear cells at five independently solved off-corner positions.

    Failing cells are split into four. Unchecked/budget-limited cells remain
    unresolved and runtime interpolation refuses them. Every wavelength counts.
    """
    lim = bounds(spec); nodes=[]; arrays=[] if array_store is None else array_store; by_key={}; leaves=[]
    class RefusedNode(Exception):
        pass
    def node(x,y):
        key=(round(float(x),14),round(float(y),14))
        if key in by_key: return by_key[key]
        if len(nodes) >= spec['max_field_nodes']: raise OverflowError('field-node budget')
        a, meta = evaluate(key)
        a=np.asarray(a,dtype=np.float32)
        if a.shape != (len(spec['wavelength_nm']),spec['optical_size'],spec['optical_size']):
            raise ValueError('Optical evaluator returned a different wavelength/grid contract')
        if not np.isfinite(a).all() or a.min()<0: raise ValueError('Invalid optical density')
        index=len(nodes); by_key[key]=index
        nodes.append(dict(id=index,uv=list(key),**meta))
        if array_store is None: arrays.append(a)
        else: arrays[index]=a
        progress(f'field node {len(nodes)}/{spec["max_field_nodes"]}')
        return index
    xs,ys=[np.linspace(-v,v,n) for v,n in zip(lim,spec['initial_grid_xy'])]
    def prefetch(points):
        if hasattr(evaluate,'prefetch'):
            pending=[]
            for x,y in points:
                key=(round(float(x),14),round(float(y),14))
                if key not in by_key and key not in pending: pending.append(key)
            evaluate.prefetch(pending[:max(0,spec['max_field_nodes']-len(nodes))])
    prefetch([(0,0), *[(x,y) for y in ys for x in xs]])
    # An even initial grid still gets an independently calculated optical axis.
    node(0,0)
    for y in ys:
        for x in xs: node(x,y)
    queue=[dict(bounds_uv=[float(x0),float(x1),float(y0),float(y1)],level=0)
           for y0,y1 in zip(ys,ys[1:]) for x0,x1 in zip(xs,xs[1:])]
    while queue:
        cell=queue.pop(0); x0,x1,y0,y1=cell['bounds_uv']; xm=(x0+x1)/2; ym=(y0+y1)/2
        corner_xy=[(x0,y0),(x1,y0),(x0,y1),(x1,y1)]
        radius=max(math.hypot(x/lim[0],y/lim[1])/math.sqrt(2) for x,y in corner_xy)
        threshold=spec['interpolation_l1_limit']*(spec['outer_tolerance_scale']
                   if radius >= spec['outer_radius_fraction'] else 1.)
        cell.update(tolerance=threshold,outer=bool(radius>=spec['outer_radius_fraction']),probes=[])
        try:
            prefetch([*corner_xy,(xm,ym),(xm,y0),(xm,y1),(x0,ym),(x1,ym)])
            cell['corners']=[node(*xy) for xy in corner_xy]
            if any(nodes[i].get('status')=='refused' for i in cell['corners']):
                raise RefusedNode('A physical corner PSF was refused')
            for xy in [(xm,ym),(xm,y0),(xm,y1),(x0,ym),(x1,ym)]:
                actual=node(*xy); weights=_weights(cell,xy)
                if nodes[actual].get('status')=='refused':
                    raise RefusedNode('A physical interpolation probe was refused')
                estimated=sum(w*arrays[i] for w,i in zip(weights,cell['corners']))
                errors=np.abs(estimated-arrays[actual]).sum(axis=(1,2))*spec['optical_sample_um']**2
                cell['probes'].append(dict(node=actual,l1_by_wavelength=errors.tolist()))
            error=max(max(p['l1_by_wavelength']) for p in cell['probes'])
            cell['maximum_l1_error']=error
            if error <= threshold:
                cell['status']='qualified'; leaves.append(cell)
            elif cell['level'] >= spec['max_refinement_level']:
                cell.update(status='unresolved',reason='refinement_limit'); leaves.append(cell)
            else:
                progress(f'Adaptive split · level{cell["level"]} error{error:.4g} > {threshold:.4g}')
                queue.extend(dict(bounds_uv=[a,b,c,d],level=cell['level']+1)
                    for c,d in [(y0,ym),(ym,y1)] for a,b in [(x0,xm),(xm,x1)])
        except OverflowError:
            cell.update(status='unresolved',reason='field_node_budget'); leaves.append(cell)
        except RefusedNode as exc:
            if cell['level']>=spec['max_refinement_level']:
                cell.update(status='unresolved',reason='physical_node_refused',detail=str(exc));leaves.append(cell)
            else:
                progress('Adaptive split around refused optical node')
                queue.extend(dict(bounds_uv=[a,b,c,d],level=cell['level']+1)
                    for c,d in [(y0,ym),(ym,y1)] for a,b in [(x0,xm),(xm,x1)])
    return nodes, np.stack(arrays) if array_store is None else arrays[:len(nodes)], leaves


def interpolate(document, density, uv, wave):
    """Exact wavelength only; no field extrapolation or unqualified cell use."""
    s=document['specification']; uv=np.asarray(uv,float)
    if uv.shape!=(2,) or not np.isfinite(uv).all() or np.any(np.abs(uv)>bounds(s)+1e-13):
        raise ValueError('Field lies outside the stored LUT domain')
    if wave not in s['wavelength_nm']: raise ValueError('Choose a stored wavelength; spectral interpolation is not qualified')
    wi=s['wavelength_nm'].index(wave)
    for node in document['nodes']:
        if np.max(np.abs(np.asarray(node['uv'])-uv)) <= 1e-13:
            if node.get('status')=='refused':
                raise ValueError('Optical node was refused: '+node.get('reason','see preserved diagnostics'))
            return density[node['id'],wi].astype(float),dict(method='exact_node',node_ids=[node['id']])
    for cell in document['cells']:
        x0,x1,y0,y1=cell['bounds_uv']
        if x0-1e-13<=uv[0]<=x1+1e-13 and y0-1e-13<=uv[1]<=y1+1e-13:
            if cell['status']!='qualified': raise ValueError('Field interpolation is unresolved: '+cell['reason'])
            weights=_weights(cell,uv)
            return sum(w*density[i,wi].astype(float) for w,i in zip(weights,cell['corners'])),dict(
                method='qualified_bilinear',node_ids=cell['corners'],weights=weights.tolist(),
                checked_l1_error=cell['maximum_l1_error'],tolerance=cell['tolerance'])
    raise ValueError('No qualified field cell contains this direction')


def integrate_pixel(density, optical_spacing_um, pixel_pitch_um, kernel_size,
                    source_offset_um_xy=(0.,0.)):
    """Integrate a piecewise-constant optical quadrature into pixel rectangles.

    Conserves mass on the represented support. No padding-induced renormalization
    hides a cropped tail. Fractional source offsets are explicit coordinates.
    """
    if (type(kernel_size) is not int or not 3<=kernel_size<=513 or kernel_size%2!=1
            or type(pixel_pitch_um) not in (float,int) or not math.isfinite(pixel_pitch_um)
            or not .2<=pixel_pitch_um<=20): raise ValueError('Invalid sensor integration grid')
    a=np.asarray(density,float); offsets=np.asarray(source_offset_um_xy,float)
    if (a.ndim!=2 or a.shape[0]!=a.shape[1] or a.shape[0]%2!=1 or not np.isfinite(a).all()
            or a.min()<0 or not math.isfinite(optical_spacing_um) or optical_spacing_um<=0
            or offsets.shape!=(2,) or not np.isfinite(offsets).all()): raise ValueError('Invalid optical intensity or source offset')
    if pixel_pitch_um<2*optical_spacing_um:
        raise ValueError('Sensor pitch needs at least2 optical quadrature samples; regenerate a finer optical LUT')
    def overlap(count,step,offset):
        optical_edges=(np.arange(count+1)-count/2)*step+offset
        receiving=(np.arange(kernel_size+1)-kernel_size/2)*pixel_pitch_um
        return np.maximum(0,np.minimum(receiving[1:,None],optical_edges[None,1:])-
                          np.maximum(receiving[:-1,None],optical_edges[None,:-1]))
    wx=overlap(a.shape[1],optical_spacing_um,offsets[0])
    wy=overlap(a.shape[0],optical_spacing_um,-offsets[1])
    kernel=wy@a@wx.T; mass=float(kernel.sum()); total=float(a.sum()*optical_spacing_um**2)
    if mass<.95*total: raise ValueError('Sensor kernel discards >5% of optical support; enlarge explicit kernel')
    return kernel,dict(pixel_pitch_um=pixel_pitch_um,kernel_size=kernel_size,
         retained_conditional_energy=mass,optical_quadrature_energy=total,
         pixel_aperture_applications=1,normalization='physical_bin_mass_not_renormalized',
         source_offset_um_xy=offsets.tolist(),quadrature_model='piecewise_constant_optical_intensity')


def apply_local_radiance(plane, density, optical_spacing_um):
    """Optical convolution on the declared fine grid; receiving area is separate."""
    a=np.asarray(plane,float)
    if a.ndim!=2 or not np.isfinite(a).all() or a.min()<0: raise ValueError('Invalid monochromatic radiance plane')
    return fftconvolve(a,np.asarray(density,float)*optical_spacing_um**2,mode='same')
