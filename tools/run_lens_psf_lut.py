"""Generate/reopen a source-pinned optical Lens LUT with the common SDK lifecycle."""
import argparse
import json
from pathlib import Path

from camera_platform.lens_psf_lut import specification
from camera_platform.sdk import PlatformClient


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url',default='http://127.0.0.1:8780')
    p.add_argument('--lens',default='canon_p0248')
    p.add_argument('--name',default='Canon p0248 · sensor-independent optical LUT')
    p.add_argument('--half-field-deg',nargs=2,type=float,default=[6.,4.])
    p.add_argument('--grid',nargs=2,type=int,default=[3,3])
    p.add_argument('--wavelength-nm',nargs='+',type=float,default=[450.,550.,650.])
    p.add_argument('--max-nodes',type=int,default=81)
    p.add_argument('--depth',type=int,default=1)
    p.add_argument('--l1-limit',type=float,default=.08)
    p.add_argument('--run',help='Reopen a prior run without tracing')
    p.add_argument('--output',default='outputs/lens-psf-lut-20261017/delivery.json')
    a=p.parse_args();output=Path(a.output);output.parent.mkdir(parents=True,exist_ok=True)
    with PlatformClient(a.url,timeout=600) as c:
        if a.run:job=c.lens_psf_lut(a.run)
        else:
            s=specification(dict(name=a.name,lens_key=a.lens,half_field_deg_xy=a.half_field_deg,
                  initial_grid_xy=a.grid,wavelength_nm=a.wavelength_nm,max_field_nodes=a.max_nodes,
                  max_refinement_level=a.depth,interpolation_l1_limit=a.l1_limit))
            job=c.submit_lens_psf_lut(s)
            output.with_suffix('.submitted.json').write_text(json.dumps(job,ensure_ascii=False,indent=2))
            print(job['id'],flush=True)
            job=c.wait(job['id'],timeout=21600,poll_interval=10,expected_kind='lens_optical_psf_lut')
        output.write_text(json.dumps(job,ensure_ascii=False,indent=2))
        if job['status']!='succeeded':raise RuntimeError(job.get('error',job['status']))
        print(a.url+'/lens-psf-lut.html?run='+job['id'])
        print(json.dumps(job['result']['summary']['statistics'],indent=2))


if __name__=='__main__':main()
