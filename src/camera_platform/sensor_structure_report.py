"""Bounded map previews with one shared scale across structure candidates."""
from pathlib import Path
import numpy as np


def render_maps(folder, samples, data_dir, spec, *, sample_paths=None):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    maps=[]
    for variant in spec['variants']:
        for wave in spec['wavelength_nm']:
            for angle in spec['angles_deg']:
                group=[v for v in samples if v['variant']==variant and v['wavelength_nm']==wave and v['angle_deg']==angle and v['resolution']==max(spec['resolutions'])]
                if len(group)!=2 or spec['dimension']!=2:continue
                arrays=[]
                for v in group:
                    path=sample_paths[v['task_id']].with_suffix('.npz') if sample_paths is not None else Path(data_dir)/(v['task_id']+'.npz')
                    with np.load(path,allow_pickle=False) as a:
                        arrays.append(a['absorption_density']); x=a['x_um'];y=a['y_um'];role=a['role']
                maps.append((variant,wave,angle,(arrays[0]+arrays[1])/2,x,y,role))
    maximum=max((float(m[3].max()) for m in maps),default=1)
    paths=[]
    for variant,wave,angle,d,x,y,role in maps:
        fig,ax=plt.subplots(figsize=(8,4.2),layout='constrained')
        image=ax.imshow(d.T,origin='lower',extent=[x[0],x[-1],y[0],y[-1]],aspect='equal',cmap='magma',vmin=0,vmax=maximum)
        ax.set(xlabel='x [um]',ylabel='depth coordinate [um]',title=f'{variant} | {wave:g} nm | {angle:g} deg | TE/TM mean')
        fig.colorbar(image,ax=ax,label='Absorbed power / incident power / um²')
        p=Path(folder)/f'map-{variant}-{wave:g}-{angle:g}.png';fig.savefig(p,dpi=130);plt.close(fig);paths.append(p)
    return paths
