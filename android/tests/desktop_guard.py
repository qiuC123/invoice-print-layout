"""Pytest audit guard for this handoff's prohibition on the real PC workspace/service."""
import os
from pathlib import Path
import sys

BLOCKED = os.path.normcase(str(Path("E:/devlop/invoice-print-layout/workspace").absolute()))


def guard(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        name = os.path.normcase(str(Path(os.fsdecode(args[0])).absolute()))
        if name == BLOCKED or name.startswith(BLOCKED + os.sep):
            raise PermissionError("F22-C: real workspace is forbidden")
    if event == "socket.connect" and isinstance(args[1], tuple) and args[1][1] == 8765:
        raise PermissionError("F22-C: real desktop service is forbidden")


for event, args in [("open", (BLOCKED + os.sep + "must-not-read",)),
                    ("socket.connect", (None, ("127.0.0.1", 8765)))]:
    try:
        guard(event, args)
        raise AssertionError("guard failed to reject forbidden access")
    except PermissionError:
        pass
sys.addaudithook(guard)
