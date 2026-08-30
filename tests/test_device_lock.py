from pathlib import Path

import pytest

from xangi_stackchan.device_lock import DeviceBusyError, DeviceLock


def test_device_lock_rejects_second_owner(tmp_path: Path):
    first = DeviceLock("/dev/stackchan", tmp_path).acquire()
    try:
        with pytest.raises(DeviceBusyError):
            DeviceLock("/dev/stackchan", tmp_path).acquire()
    finally:
        first.release()

    second = DeviceLock("/dev/stackchan", tmp_path).acquire()
    second.release()
