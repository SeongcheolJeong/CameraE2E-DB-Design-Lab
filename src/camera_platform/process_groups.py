"""Signal private runner groups, distinguishing exited Darwin groups from denial."""

import os
import subprocess
import sys


def group_has_live_members(group):
    """Require a complete, readable process-state observation; never assume failure is empty."""
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,pgid=,stat="],
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    )
    own_row_seen, live = False, False
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            raise ValueError("Cannot establish process group state from ps output")
        pid, pgid, state = int(parts[0]), int(parts[1]), parts[2]
        own_row_seen |= pid == os.getpid() and pgid == os.getpgrp()
        if pgid == group and not state.startswith("Z"):
            live = True
    if not own_row_seen:
        raise ValueError("Process observation did not include the current Worker")
    return live


def signal_private_group(group, signum):
    if type(group) is not int or group <= 1 or group == os.getpgrp():
        raise ValueError("Only a separate positive runner process group can be signalled")
    try:
        os.killpg(group, signum)
    except ProcessLookupError:
        return
    except PermissionError:
        # Darwin killpg1 excludes SZOMB; zero eligible recipients can produce
        # EPERM. A fresh successful state observation must prove no live member.
        # Real permission failures or unavailable observations remain errors.
        if sys.platform == "darwin":
            try:
                if not group_has_live_members(group):
                    return
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        raise
