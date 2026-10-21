#!/usr/bin/env python3
"""Standalone source-frozen Meep optical structure worker (2D / periodic 3D).

Incident power, active-Si absorption, DTI loss and conditional redistribution
have distinct denominators. No charge collection, product QE or sensor MTF.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import time
from pathlib import Path

import numpy as np


def solve(task, output):
    import meep as mp
    mp.verbosity(0)
    start=time.monotonic()
    g=task['structure']['geometry']; tables=task['structure']['materials']
    wave=task['wavelength_nm']; freq=1000/wave; dim=task['dimension']
    res=task['resolution']; pml=task['pml_um']; pitch=g['pitch_um']
    ncell=task['neighborhood']+2*task['guard_cells'] if dim==2 else 2
    width=ncell*pitch
    low=-g['si_um']; high=g['passivation_um']+g['cfa_um']+g['ocl_height_um']+.5
    bottom=low-.4-pml; top=high+.8+pml; cy=(top+bottom)/2
    def vec(x=0,y=0,z=0): return mp.Vector3(x,y-cy,z)
    nk={}; media={}
    for name,t in tables.items():
        if not t['wavelength_nm'][0]<=wave<=t['wavelength_nm'][-1]: raise ValueError('Material extrapolation forbidden')
        n=float(np.interp(wave,t['wavelength_nm'],t['n'])); k=float(np.interp(wave,t['wavelength_nm'],t['k']))
        nk[name]=complex(n,k); eps=n*n-k*k
        if eps<=0: raise ValueError('Metal dispersion fitting required; cannot use positive-epsilon conductivity adapter')
        media[name]=mp.Medium(epsilon=eps+2*n*k, E_susceptibilities=[mp.DrudeSusceptibility(frequency=freq,gamma=freq,sigma=4*n*k)] if k>0 else [])
    names=['air','silicon','passivation','lens','red','green','blue']
    eps_loss={name:0. if name=='air' else float((nk[name]**2).imag) for name in names}
    centers=(np.arange(ncell)-(ncell-1)/2)*pitch
    shift=.1*pitch if task['variant']=='ocl_shift' else 0.
    dti_on=task['variant']!='without_dti'
    def classify(x,y,z=0):
        # Repeated array coordinates. Half-pitch centered Bayer sites in 3D.
        ix=int(np.floor((x+width/2)/pitch)); dx=(x+width/2)%pitch-pitch/2
        iz=int(np.floor((z+width/2)/pitch)) if dim==3 else 0
        dz=(z+width/2)%pitch-pitch/2 if dim==3 else 0.
        if y<0:
            boundary=min(pitch/2-abs(dx),pitch/2-abs(dz)) if dim==3 else pitch/2-abs(dx)
            if dti_on and -g['dti_depth_um']<=y<0 and boundary<g['dti_width_um']/2:
                core=g['dti_width_um']/2-g['dti_oxide_liner_um']
                return ('silicon' if boundary<core else 'passivation'), 'dti'
            return 'silicon','active_si' if y>=low else 'substrate'
        if y<g['passivation_um']: return 'passivation','passivation'
        cfa_top=g['passivation_um']+g['cfa_um']
        if y<cfa_top:
            channel='green' if dim==2 else [['red','green'],['green','blue']][iz%2][ix%2]
            # R/B nominal .475 um: the remaining .025 um is passivation.
            if dim==3 and channel!='green' and y<cfa_top-.475: return 'passivation','passivation'
            return channel,'cfa'
        if y<cfa_top+g['ocl_height_um']:
            # Periodic nearest shifted lens, preserving fractional-position shift.
            dxx=(x-shift+width/2)%pitch-pitch/2
            radial=dxx*dxx+(dz*dz if dim==3 else 0)
            if radial/g['ocl_semiaxis_um']**2+((y-cfa_top)/g['ocl_height_um'])**2<=1:
                return 'lens','lens'
        return 'air','air'
    def material(point):
        name,_=classify(point.x,point.y+cy,point.z)
        return mp.air if name=='air' else media[name]
    angle=np.deg2rad(task['angle_deg']); kx=freq*np.sin(angle)
    sigma=task['source_sigma_pitch']*pitch
    def amplitude(point):
        envelope=np.exp(-.5*(point.x/sigma)**2) if dim==2 else 1.
        return envelope*np.exp(2j*np.pi*kx*point.x)
    component=mp.Ez if task['polarization']=='s' else mp.Ex
    source=mp.Source(mp.GaussianSource(freq,fwidth=.1*freq), component=component,
        center=vec(y=high+.35),size=mp.Vector3(width,0,width if dim==3 else 0),amp_func=amplitude)
    cell=mp.Vector3(width+2*(pml+.3) if dim==2 else width,top-bottom,width if dim==3 else 0)
    boundary=[mp.PML(pml)] if dim==2 else [mp.PML(pml,direction=mp.Y)]
    kpoint=mp.Vector3() if dim==2 else mp.Vector3(kx,0,0)
    size=mp.Vector3(width,high-low,width if dim==3 else 0)
    def build(with_stack):
        sim=mp.Simulation(cell_size=cell,dimensions=dim,boundary_layers=boundary,
            sources=[source],resolution=res,k_point=kpoint,force_complex_fields=True,
            material_function=material if with_stack else None,
            extra_materials=list(media.values()))
        monitors={}
        for name,y in [('top',high),('bottom',low)]:
            monitors[name]=sim.add_flux(freq,0,1,mp.FluxRegion(center=vec(y=y),
                size=mp.Vector3(width,0,width if dim==3 else 0),direction=mp.Y,weight=-1))
        if dim==2:
            for name,x,weight in [('left',-width/2,-1),('right',width/2,1)]:
                monitors[name]=sim.add_flux(freq,0,1,mp.FluxRegion(center=vec(x=x,y=(high+low)/2),
                    size=mp.Vector3(0,high-low),direction=mp.X,weight=weight))
        fields=sim.add_dft_fields([mp.Ex,mp.Ey,mp.Ez,mp.Dx,mp.Dy,mp.Dz],freq,0,1,
             center=vec(y=(high+low)/2),size=size,yee_grid=True) if with_stack else None
        return sim,monitors,fields
    ref,rm,_=build(False)
    ref.run(until_after_sources=task['after_source_time'])
    incident=float(mp.get_fluxes(rm['top'])[0]); subtraction=ref.get_flux_data(rm['top'])
    if incident<=0: raise ValueError('No positive incident reference flux')
    ref.reset_meep()
    sim,mon,fields=build(True)
    sim.load_minus_flux_data(mon['top'],subtraction)
    sim.run(until_after_sources=task['after_source_time'])
    flux={k:float(mp.get_fluxes(v)[0])/incident for k,v in mon.items()}
    reflect=-flux['top']; transmit=flux['bottom']; side=flux.get('left',0)+flux.get('right',0)
    x,y,z,weights=sim.get_array_metadata(dft_cell=fields)
    y=np.asarray(y)+cy; x=np.asarray(x); z=np.asarray(z)
    role_names=['air','active_si','dti','substrate','passivation','cfa','lens']
    def role_grid(x,y,z):
        # Vectorized masks avoid a Python loop over the dense field cube.
        shape=(len(x),len(y)) if dim==2 else (len(x),len(y),len(z))
        roles=np.zeros(shape,dtype=np.uint8)
        X=x.reshape((-1,1) if dim==2 else (-1,1,1)); Y=y.reshape((1,-1) if dim==2 else (1,-1,1))
        Z=0 if dim==2 else z.reshape((1,1,-1))
        dx=(X+width/2)%pitch-pitch/2; dz=0 if dim==2 else (Z+width/2)%pitch-pitch/2
        bound=pitch/2-np.abs(dx) if dim==2 else np.minimum(pitch/2-np.abs(dx),pitch/2-np.abs(dz))
        def assign(mask,name,role):
            mask=np.broadcast_to(mask,shape)
            roles[mask]=role_names.index(role)
        assign(Y<0,'silicon','active_si'); assign(Y<low,'silicon','substrate')
        if dti_on:
            trench=(Y<0)&(Y>=-g['dti_depth_um'])&(bound<g['dti_width_um']/2)
            assign(trench,'passivation','dti')
            assign(trench&(bound<g['dti_width_um']/2-g['dti_oxide_liner_um']),'silicon','dti')
        assign((Y>=0)&(Y<g['passivation_um']),'passivation','passivation')
        ctop=g['passivation_um']+g['cfa_um']; cfa=(Y>=g['passivation_um'])&(Y<ctop)
        if dim==2: assign(cfa,'green','cfa')
        else:
            ix=np.floor((X+width/2)/pitch).astype(int)%2; iz=np.floor((Z+width/2)/pitch).astype(int)%2
            for name,mask in [('red',(ix==0)&(iz==0)),('green',ix!=iz),('blue',(ix==1)&(iz==1))]:
                assign(cfa&mask,name,'cfa')
                if name!='green': assign(cfa&mask&(Y<ctop-.475),'passivation','passivation')
        dxx=(X-shift+width/2)%pitch-pitch/2
        lens=((dxx*dxx+dz*dz)/g['ocl_semiaxis_um']**2+((Y-ctop)/g['ocl_height_um'])**2<=1)&(Y>=ctop)
        assign(lens,'lens','lens')
        return roles
    # E and D must be paired at their own Yee locations. Interpolating complex
    # E and D separately creates spurious signed absorption at interfaces.
    # Physical integrals below use component-specific coordinates and cell
    # overlap quadrature. Only the visualization is interpolated to one grid.
    from scipy.ndimage import map_coordinates
    display_axes=[x,y] if dim==2 else [x,y,z]
    display_shape=tuple(len(a) for a in display_axes)
    signed_density=np.zeros(display_shape)
    layers={r:0. for r in role_names};pixel=np.zeros(ncell if dim==2 else 4)
    negative_power=0.;native_arrays={}
    bounds=[(-width/2,width/2),(low,high)]+([] if dim==2 else [(-width/2,width/2)])
    for e,d in ((mp.Ex,mp.Dx),(mp.Ey,mp.Dy),(mp.Ez,mp.Dz)):
        E=sim.get_dft_array(fields,e,0);D=sim.get_dft_array(fields,d,0)
        if not np.any(E):continue
        dims,lo,hi=sim.get_array_slice_dimensions(e,center=vec(y=(high+low)/2),size=size)
        axes=[np.linspace(getattr(lo,k),getattr(hi,k),int(dims[i])) for i,k in enumerate(('x','y','z'))]
        axes[1]+=cy
        active_axes=axes[:2] if dim==2 else axes
        if E.shape!=tuple(len(a) for a in active_axes):raise ValueError('Yee field coordinate mismatch')
        density=2*np.pi*freq*np.imag(np.conj(E)*D)/incident
        w=np.ones(E.shape)
        for axis,(coords,(lower,upper)) in enumerate(zip(active_axes,bounds)):
            overlap=np.maximum(0,np.minimum(coords+.5/res,upper)-np.maximum(coords-.5/res,lower))
            sh=[1]*dim;sh[axis]=len(coords);w*=overlap.reshape(sh)
        negative_power+=float(np.sum(np.minimum(density,0)*w))
        power=np.maximum(density,0)*w
        role=role_grid(*axes)
        for i,r in enumerate(role_names):layers[r]+=float(power[role==i].sum())
        # Boundary Yee cells are attributed to the nearest edge pixel.
        ix=np.clip(np.floor((axes[0]+width/2)/pitch).astype(int),0,ncell-1)
        active=role==role_names.index('active_si')
        if dim==2:
            for i in range(ncell):pixel[i]+=float(power[active & (ix[:,None]==i)].sum())
        else:
            iz=np.clip(np.floor((axes[2]+width/2)/pitch).astype(int),0,1)
            for row in range(2):
                for col in range(2):pixel[row*2+col]+=float(power[active & (ix[:,None,None]==col) & (iz[None,None,:]==row)].sum())
        coordinates=np.meshgrid(*[(a-b[0])*res for a,b in zip(display_axes,active_axes)],indexing='ij')
        signed_density+=map_coordinates(density,coordinates,order=1,mode='nearest',prefilter=False)
        native_arrays[f'component_{e}_density']=density.astype(np.float32)
        for i,axis in enumerate(active_axes):native_arrays[f'component_{e}_axis_{i}']=axis
    pixel=pixel.tolist()
    density=np.maximum(signed_density,0);roles=role_grid(x,y,z)
    absorption=sum(layers.values()); residual=1-reflect-transmit-side-absorption
    total=sum(pixel)
    guard=task['guard_cells']
    tail=(sum(pixel[:guard])+sum(pixel[-guard:]))/max(total,1e-15) if dim==2 else 0.
    # Store a bounded visualization plus full numerical absorption arrays.
    output=Path(output); output.parent.mkdir(parents=True,exist_ok=True)
    np.savez_compressed(output.with_suffix('.npz'),x_um=x,y_um=y,z_um=z,
        absorption_density=density.astype(np.float32),signed_absorption_density=signed_density.astype(np.float32),role=roles,**native_arrays)
    array_hash=hashlib.sha256(output.with_suffix('.npz').read_bytes()).hexdigest()
    result=dict(schema='camera_sensor_structure_sample_v1',task_id=task['id'],
        variant=task['variant'],wavelength_nm=wave,angle_deg=task['angle_deg'],
        polarization=task['polarization'],dimension=dim,resolution=res,
        control=task.get('control','mesh'), structure_revision=task['structure']['revision'],
        R=reflect,T=transmit,side_outflow=side,absorption=absorption,
        active_si_absorption=layers['active_si'],layer_absorption=layers,
        energy_residual=residual,incident_power=incident,pixel_absorption=pixel,
        conditional_redistribution=[v/total for v in pixel] if total>0 else [0.]*len(pixel),
        support_tail_fraction=tail,negative_dissipation_integral=negative_power,passive=min(reflect,transmit,absorption,*pixel,negative_power)>=-1e-5,
        array_sha256=array_hash,elapsed_s=time.monotonic()-start,meep_version=mp.__version__,
        source_model='finite_gaussian_xsection' if dim==2 else 'periodic_bayer_plane_wave',
        source_sigma_um=sigma if dim==2 else None,measured_accuracy=False)
    output.write_text(json.dumps(result,sort_keys=True,allow_nan=False))
    sim.reset_meep()
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('task');p.add_argument('output');a=p.parse_args()
    solve(json.loads(Path(a.task).read_text()),a.output)
