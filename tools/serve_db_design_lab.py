"""Run a separate local DB Lab with portable project storage, never a T7 default."""
import argparse
import os
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8781)
    parser.add_argument('--state-dir', type=Path, default=root / 'outputs/db-design-lab')
    parser.add_argument('--workshop-dir', type=Path, default=root / 'outputs/db-design-workshop')
    args = parser.parse_args()
    os.environ['CAMERA_PLATFORM_WORKSHOP_ROOT'] = str(args.workshop_dir.resolve())
    import uvicorn
    from camera_platform.api import create_app
    uvicorn.run(create_app(args.state_dir.resolve(), root, seed_demo=False),
                host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
