"""Direct boundary/validation PSFs; compact, hash-bound, resumable optical evidence.

All 21 wavelength nodes are independently ray traced at each new position. Fine
q16/r96 optical point masses exist only during one field's qualification. Retain
rays, physical gates, finite-reference curves/captures and q8/r64 applied kernels,
not redundant 3073-square RGB source arrays in every report.
"""
from __future__ import annotations

import json
import math
import shutil
import time
from copy import deepcopy
from pathlib import Path

import numpy as np
from scipy.optimize import brentq

from . import calibration_direct_optics as source
from . import calibration_direct_convergence as direct
from . import prescription_psf, reference_exit_pupil as pupil
from .registry import digest

POLICY = "direct_sensor_boundary_anchor_source_v1"
SETTING = direct.setting(8, 64)


def bind(g):
    from rayoptics.raytr import trace
    from rayoptics.raytr.opticalspec import Field

    source.validate(g)
    h, w = g["full_sensor_shape"]
    request = g["requests"][3]
    model = prescription_psf._model(request, 1)

    def chief(angle):
        f = Field(y=angle, fov=model.optical_spec["fov"])
        pupil.aim_at_stop(model, f, 550.0)
        ray, _, _ = trace.trace_base(model, [0., 0.], f, 550., apply_vignetting=False,
                                     check_apertures=True, pt_inside_fuzz=0.)
        prescription_psf._validate_physical_ray(
            ray, model, request["prescription"]["surfaces"], "sensor boundary chief")
        return np.asarray(ray[-1][0][:2], float)

    fields = []
    # 3x3 is nested in 5x5; sixteen cell centers are NEVER candidate anchors.
    positions = [(f"a-{r}-{c}", (h-1)*r/4, (w-1)*c/4, "anchor")
                 for r in range(5) for c in range(5)]
    positions += [(f"v-{r}-{c}", (h-1)*(2*r+1)/8, (w-1)*(2*c+1)/8, "validation")
                  for r in range(4) for c in range(4)]
    for fid, row, col, role in positions:
        axis = g["optical_axis_active_row_col"]
        xy = [(col-axis[1])*g["pixel_pitch_um"]/1000,
              -(row-axis[0])*g["pixel_pitch_um"]/1000]
        radius = float(np.linalg.norm(xy))
        angle = 0. if radius == 0 else brentq(
            lambda a: np.linalg.norm(chief(a))-radius, 0., 12., xtol=1e-10)
        point = chief(angle)
        error = float(abs(np.linalg.norm(point)-radius))
        if error > 1e-7:
            raise ValueError("Direct sensor boundary chief misses requested position")
        fields.append(dict(id=fid, role=role, optical_anchor_active_row_col=[row,col],
                           image_xy_mm=xy, object_angle_deg=float(angle),
                           meridional_anchor_xy_mm=point.tolist(),
                           azimuth_deg=math.degrees(math.atan2(xy[1],xy[0])) if radius else 0.,
                           chief_anchor_residual_mm=error))
    result = dict(policy=POLICY, generator_sha256=source.sha(__file__),
                  source_geometry_sha256=g["geometry_sha256"], backend=g["backend"],
                  fields=fields, grids=[3,5], independent_validation_fields=16,
                  setting=SETTING, full_sensor_shape=[h,w],
                  physical_validation="not_established")
    result["sha256"] = digest(result)
    return result


def validate(a, g):
    if a.get("sha256") != digest({k:v for k,v in a.items() if k != "sha256"}):
        raise ValueError("Direct anchor geometry hash differs")
    source.validate(g)
    if (a["source_geometry_sha256"] != g["geometry_sha256"]
        or a["generator_sha256"] != source.sha(__file__)
        or a["backend"] != source.backend() or a["policy"] != POLICY):
        raise ValueError("Direct anchor source/backend differs from frozen geometry")


def cache_root(workspace, a):
    return Path(workspace)/".cache"/"calibration-boundary-psf"/a["sha256"]


def _verify(folder, row):
    for name, hashed in row["files"].items():
        p=folder/name
        if not p.is_file() or source.sha(p) != hashed:
            raise ValueError("Direct anchor cache changed: "+str(p))
    if row["status"] != "passed":
        raise ValueError("Direct anchor physical/numerical qualification failed")


def field(g, a, fid, workspace, progress=None):
    validate(a,g)
    c = next(c for c in a["fields"] if c["id"] == fid)
    folder=cache_root(workspace,a)/fid
    folder.mkdir(parents=True,exist_ok=True)
    if (folder/"result.json").exists():
        row=json.loads((folder/"result.json").read_text());_verify(folder,row);return row
    started=time.perf_counter()
    size=2*source.REFERENCE_RADIUS*source.REFERENCE_Q+1
    summed=np.zeros((size,size,3),float)
    diagnostics=[];required_q=0.
    for wi,wave in enumerate(g["wavelength_nm"]):
        if progress:progress(f"직접 경계 PSF · {fid} · {wi+1}/21 · {wave:g}nm")
        wave_folder=folder/f"wave-{int(wave)}";wave_folder.mkdir(exist_ok=True)
        qp=wave_folder/"qualification.json"
        if qp.exists():
            saved=json.loads(qp.read_text())
            if saved["status"] != "passed" or source.sha(wave_folder/"qualified-rays.npz") != saved["ray_arrays_sha256"]:
                raise ValueError("Retained boundary pupil rays changed")
            with np.load(wave_folder/"qualified-rays.npz",allow_pickle=False) as z:
                t={k.removeprefix("fine_"):z[k] for k in z if k.startswith("fine_")}
            t.update(saved["ray_metadata"][-1]);diag=saved["attempts"][-1]
        else:
            t,_,diag=source._qualified_wave(g["requests"][wi//3],wi%3,c,wave_folder)
        mass,ref=source._reference_from_trace(t,wave)
        required_q=max(required_q,2*float(np.max(np.ptp(t["q"],axis=0))*1.04)*g["pixel_pitch_um"]/(wave*.001))
        for ch,weight in enumerate(g["spectral_weights_rgb"][wi]):summed[...,ch]+=mass*weight
        diagnostics.append(dict(wavelength_nm=wave,physical_qualification=diag,reference=ref))
        from prysm.fttools import mdft
        mdft.clear()
    del mass,t
    fine=summed.astype(np.float32);del summed
    settings=[direct.setting(8,64),direct.setting(16,64),direct.setting(16,96)]
    evidence=[]
    # Stationary numerical checks translate the PSF to an interior test chart.
    # They do not claim that a Crop centered on a boundary fits inside the sensor.
    test=deepcopy(g["crops"][4]);test["id"]=fid
    for s in settings:
        k=direct.sampled_kernel(fine,s)
        row=dict(status="measured",energy_rgb=k.sum(axis=(0,1),dtype=float).tolist(),
                 kernel_curves=direct.optical_curves(k,s["samples_per_pixel"]),captures={})
        evidence.append(row)
    # Numerical RAW/ISP execution needs the electronics/CCM frozen alongside g.
    profile=json.loads((cache_root(workspace,a)/"numerical-profile.json").read_text())
    for s,row in zip(settings,evidence,strict=True):
        k=direct.sampled_kernel(fine,s)
        for angle in (0,90):
            cdf,offset=direct.projection_cdf(k,angle)
            for phase in direct.PHASES:
                cid=f"edge-{angle}-phase-{int(phase*100):02d}"
                raw,linear,_,meta,_=direct.capture(profile,g,test,s,angle,phase,cdf,offset)
                curves,_=direct.curves(raw,linear,meta,angle)
                name=f"{direct.key(s)}-{cid}.npz"
                np.savez_compressed(folder/name,raw=raw,linear=linear)
                row["captures"][cid]=dict(curves=curves,file=name,sha256=source.sha(folder/name))
    reference={"fields":{fid:evidence[-1]}}
    checks=[direct.compare({"fields":{fid:r}},reference) for r in evidence[:-1]]
    numerical_pass=required_q<=8 and all(v["passed"] for v in checks)
    k=direct.sampled_kernel(fine,SETTING)
    np.savez_compressed(folder/"kernel.npz",mass_rgb=k)
    result=dict(field=c,setting=SETTING,status="passed" if numerical_pass else "not_converged",
                minimum_intensity_nyquist_q=required_q,wavelengths=diagnostics,
                references=[dict(setting=s,**v) for s,v in zip(settings,evidence,strict=True)],
                numerical_checks=checks,reference_scope="finite q16/r96; unknown tails remain unknown",
                elapsed_s=time.perf_counter()-started,
                kernel_file="kernel.npz",kernel_sha256=source.sha(folder/"kernel.npz"))
    result["files"]={str(p.relative_to(folder)):source.sha(p) for p in folder.rglob("*") if p.is_file() and not p.name.endswith(".tmp") and p.name!="result.json"}
    source.save(folder/"result.json",result)
    return result


def prepare_cache(profile,a,workspace):
    validate(a,profile["sensor_crops"])
    base=cache_root(workspace,a);base.mkdir(parents=True,exist_ok=True)
    # This input changes numerical RAW/ISP gates; include its digest in evidence.
    p={k:deepcopy(profile[k]) for k in ("fields","electronics","conditions","isp","isp_tuning")}
    source.save(base/"geometry.json",a)
    path=base/"numerical-profile.json"
    if path.exists() and json.loads(path.read_text()) != p:
        raise ValueError("Boundary PSF numerical electronics/ISP input changed")
    source.save(path,p)


def retain(profile,a,workspace,root,progress=None):
    prepare_cache(profile,a,workspace)
    records=[];base=cache_root(workspace,a);dest=Path(root)/"anchor-optics"
    for c in a["fields"]:
        row=field(profile["sensor_crops"],a,c["id"],workspace,progress)
        _verify(base/c["id"],row)
        shutil.copytree(base/c["id"],dest/c["id"],dirs_exist_ok=True)
        records.append(row)
    result=dict(schema=POLICY,geometry=a,fields=records,status="passed",
                numerical_profile_sha256=digest(json.loads((base/"numerical-profile.json").read_text())))
    source.save(dest/"result.json",result)
    return result
