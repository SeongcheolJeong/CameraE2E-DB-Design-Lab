"""Paired structural sensitivity, not an absolute-error or confidence bound."""
from __future__ import annotations
import copy
import numpy as np
from .registry import digest

METRICS=('total_absorption','central_absorption','neighbor_fraction','asymmetry','centroid_pitch')

def defaults():
    return dict(schema='trend_screening_v1',min_effect=dict(total_absorption=.01,central_absorption=.01,
        neighbor_fraction=.001,asymmetry=.001,centroid_pitch=.01),weak_baseline=.01,
        energy_limit=.01,tail_limit=.015,body_drift_limit=.10,magnitude_fraction=.20,
        direction_multiplier=2.,budget_seconds=28800,extra_resolution=100)

def policy(value=None):
    out=defaults();value=value or {}
    if not isinstance(value,dict) or set(value)-set(out):raise ValueError('Unknown trend policy')
    out.update(copy.deepcopy(value))
    if out['schema']!='trend_screening_v1' or set(out['min_effect'])!=set(METRICS):raise ValueError('Invalid trend policy schema')
    nums=[*out['min_effect'].values(),*(out[k] for k in ('weak_baseline','energy_limit','tail_limit','body_drift_limit','magnitude_fraction','direction_multiplier','budget_seconds','extra_resolution'))]
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not np.isfinite(v) or v<=0 for v in nums):raise ValueError('Positive finite policy values required')
    if out['energy_limit']>.01 or out['tail_limit']>.015 or out['budget_seconds']>28800 or out['extra_resolution']!=100:
        raise ValueError('Energy/support/budget limits cannot be weakened; extra grid is 100')
    if out['magnitude_fraction']>.2 or out['direction_multiplier']<2:raise ValueError('Trend reliability limits cannot be weakened')
    return out

def metrics(sample):
    p=np.asarray(sample['pixel_absorption'],float)
    if p.ndim!=1 or len(p)%2!=1 or not np.isfinite(p).all() or np.any(p<0):raise ValueError('2D odd native pixel response required')
    total=float(p.sum());mid=len(p)//2
    if total<=0:raise ValueError('Zero active response')
    return dict(total_absorption=total,central_absorption=float(p[mid]),neighbor_fraction=float((total-p[mid])/total),
        asymmetry=float((p[:mid].sum()-p[mid+1:].sum())/total),centroid_pitch=float(p@((np.arange(len(p))-mid))/total))

def setting(s):return (int(s['resolution']),s.get('control','mesh'))
def valid(s,p):
    reasons=[]
    if not np.isfinite(s.get('energy_residual',np.nan)) or abs(s['energy_residual'])>p['energy_limit']:reasons.append('energy_balance')
    if s.get('passive') is not True:reasons.append('passivity')
    if not np.isfinite(s.get('support_tail_fraction',np.nan)) or s['support_tail_fraction']>p['tail_limit']:reasons.append('support_tail')
    try:metrics(s)
    except (ValueError,KeyError):reasons.append('invalid_response')
    return reasons

def delta(a,b,name,weak):
    ma,mb=metrics(a)[name],metrics(b)[name]
    relative=name in METRICS[:2] and not weak
    return (mb-ma)/ma if relative else mb-ma

def assess(samples,spec,configuration=None):
    p=policy(configuration)
    if spec['dimension']!=2:raise ValueError('Trend screening v1 is a 2D structure diagnostic')
    records={};duplicates=[]
    for s in samples:
        key=(s['variant'],s['wavelength_nm'],s['angle_deg'],s['polarization'],*setting(s))
        if key in records:duplicates.append(key)
        records[key]=s
    if duplicates:raise ValueError('Duplicate sample coordinates')
    invalid_samples=[]
    for key,sample in list(records.items()):
        try:metrics(sample)
        except (ValueError,KeyError,TypeError):
            invalid_samples.append(dict(coordinates=list(key),reason="invalid_response"));del records[key]
    rows=[];requests={};baseline='reviewed';base_res=sorted(spec['resolutions'])[-2:];high=max(base_res)
    def request(variant,wave,angle,pol,control,res,reason):
        key=(variant,wave,angle,pol,control,res)
        requests.setdefault(key,dict(variants=[baseline,variant],wavelength_nm=wave,angle_deg=angle,
            polarization=pol,control=control,resolution=res,reasons=[]))['reasons'].append(reason)
    for wave in spec['wavelength_nm']:
      for angle in spec['angles_deg']:
       for pol in ('s','p','unpolarized'):
        for variant in spec['variants']:
         if variant==baseline:continue
         pairs={}
         keys=set((k[-2],k[-1]) for k in records if k[:3] in ((baseline,wave,angle),(variant,wave,angle)))
         for k in keys:
          if pol!='unpolarized':
            a=records.get((baseline,wave,angle,pol,*k));b=records.get((variant,wave,angle,pol,*k))
          else:
            def average(v):
              a=records.get((v,wave,angle,'s',*k));b=records.get((v,wave,angle,'p',*k))
              if a is None or b is None:return None
              if len(a['pixel_absorption'])!=len(b['pixel_absorption']):return None
              return {**a,'polarization':'unpolarized','pixel_absorption':((np.array(a['pixel_absorption'])+b['pixel_absorption'])/2).tolist(),
                'energy_residual':max(abs(a['energy_residual']),abs(b['energy_residual'])),
                'passive':a['passive'] and b['passive'], 'support_tail_fraction':max(a['support_tail_fraction'],b['support_tail_fraction'])}
            a,b=average(baseline),average(variant)
          if a is not None and b is not None:pairs[k]=(a,b)
         if not pairs:continue
         reference=max((k for k in pairs if k[1]=='mesh'),default=None)
         if reference is None:continue
         a,b=pairs[reference];reference_res=reference[0]
         # Original high-grid controls remain required after selective refinement.
         required={(int(r),'mesh') for r in base_res}|{(high,'time'),(high,'pml'),(high,'phase')}
         body_drift=0.
         if all((int(r),'mesh') in pairs for r in base_res):
            low_pair=pairs[(int(base_res[0]),'mesh')];high_pair=pairs[(high,'mesh')]
            body_drift=max(abs(metrics(x)[n]-metrics(y)[n])/max(abs(metrics(y)[n]),.01) for x,y in zip(low_pair,high_pair) for n in METRICS[:2])
         latest_drift=0.
         if (p['extra_resolution'],'mesh') in pairs and (high,'mesh') in pairs:
            latest_drift=max(abs(metrics(x)[n]-metrics(y)[n])/max(abs(metrics(y)[n]),.01)
                for x,y in zip(pairs[(high,'mesh')],pairs[(p['extra_resolution'],'mesh')]) for n in METRICS[:2])
         for name in METRICS:
            weak=name in METRICS[:2] and any(metrics(x)[name]<p['weak_baseline'] for x,_ in pairs.values())
            changes={k:delta(x,y,name,weak) for k,(x,y) in pairs.items()}
            D=changes[reference];U=max(abs(d-D) for d in changes.values())
            floor=p['min_effect'][name]*(p['weak_baseline'] if weak else 1)
            sign_flip=any(d*D<0 for d in changes.values())
            near=abs(D)>=floor and (sign_flip or abs(D)<=p['direction_multiplier']*U)
            required_here=set(required)
            if body_drift>p['body_drift_limit'] or sign_flip or near:
                required_here.add((p['extra_resolution'],'mesh'))
            if name=='neighbor_fraction' and abs(D)<a['support_tail_fraction']+b['support_tail_fraction']:
                required_here.add((high,'support'))
            missing=sorted(required_here-set(pairs));invalid=[]
            for k,(x,y) in pairs.items():
                failures=sorted(set(valid(x,p)+valid(y,p)))
                if failures:invalid.append(dict(setting=list(k),reasons=failures))
            # Sign reversal remains unresolved even after a third grid; no cherry-picking.
            status='unresolved' if missing or invalid or sign_flip or latest_drift>p['body_drift_limit'] else 'indistinguishable' if abs(D)<floor or abs(D)<=p['direction_multiplier']*U else 'magnitude_stable' if U<=p['magnitude_fraction']*abs(D) else 'direction_stable'
            row=dict(variant=variant,wavelength_nm=wave,angle_deg=angle,polarization=pol,metric=name,
                baseline=metrics(a)[name],candidate=metrics(b)[name],delta=D,sensitivity=U,
                delta_unit='relative_fraction' if name in METRICS[:2] and not weak else 'pitch' if name=='centroid_pitch' else 'fraction',
                weak_baseline=weak,min_effect=floor,status=status,reference_setting=list(reference),
                completed_checks=[list(k) for k in sorted(pairs)],missing_checks=[list(k) for k in missing],
                invalid_checks=invalid,sign_reversal=sign_flip,body_drift=body_drift,latest_body_drift=latest_drift,
                changes=[dict(setting=list(k),delta=v) for k,v in sorted(changes.items())])
            rows.append(row)
            if pol!='unpolarized':
                for res,control in missing:request(variant,wave,angle,pol,control,res,name)
    for row in rows:
        if row['polarization']!='unpolarized':continue
        polarized=[r for r in rows if all(r[k]==row[k] for k in ('variant','wavelength_nm','angle_deg','metric')) and r['polarization'] in ('s','p')]
        row['polarization_opposition']=len(polarized)==2 and polarized[0]['delta']*polarized[1]['delta']<0
        if len(polarized)!=2 or any(r['status']=='unresolved' for r in polarized):row['status']='unresolved'
    return dict(schema='camera_sensor_structure_trend_assessment_v1',policy=p,policy_sha256=digest(p),rows=rows,
        additional_requests=list(requests.values()),invalid_samples=invalid_samples,camera_response_eligible=False,
        physical_scope=dict(dimension=2,model_assumptions_verified=False,measured_validated=False),
        sensitivity_meaning='Observed numerical sensitivity; not a confidence interval or error bound',
        status='unresolved' if invalid_samples or not rows or any(r['status']=='unresolved' for r in rows) else 'assessed')
