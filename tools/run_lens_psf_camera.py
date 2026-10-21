"""Capture native IMX462 probes using a completed optical LUT and common SDK jobs."""
import argparse,json
from pathlib import Path
from camera_platform.sdk import PlatformClient


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url',default='http://127.0.0.1:8780')
    p.add_argument('--lut-run',required=True)
    p.add_argument('--exposure-ms',type=float,default=4.)
    p.add_argument('--run',help='Reopen a completed camera job')
    p.add_argument('--output',default='outputs/lens-psf-camera/delivery.json')
    a=p.parse_args();out=Path(a.output);out.parent.mkdir(parents=True,exist_ok=True)
    with PlatformClient(a.url,timeout=600) as c:
        if a.run: j=c.lens_psf_camera(a.run)
        else:
            j=c.submit_lens_psf_camera(a.lut_run,exposure_ms=a.exposure_ms)
            out.with_suffix('.submitted.json').write_text(json.dumps(j,indent=2,ensure_ascii=False))
            print(j['id'],flush=True)
            j=c.wait(j['id'],timeout=7260,poll_interval=5,expected_kind='lens_psf_lut_camera')
        out.write_text(json.dumps(j,indent=2,ensure_ascii=False))
        if j['status']!='succeeded':raise RuntimeError(j.get('error',j['status']))
        print(a.url+'/lens-psf-lut.html?run='+a.lut_run+'&camera='+j['id'])


if __name__=='__main__':main()
