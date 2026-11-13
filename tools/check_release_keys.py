"""Build guard: an installer must not be built from a source tree without release keys.

With update_sig.PUBLIC_KEYS empty the app still updates, but checks an update by SHA-256 only (the checksum
comes from the same GitHub answer as the download, so it does not prove who published it). That is fine for a
test build and wrong for a release. build.bat runs this first.

Exit 0: at least one valid key is configured, or AGENTSTART_ALLOW_UNSIGNED_BUILD=1 says this is a test build.
Exit 1: no keys and no override.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OVERRIDE = "AGENTSTART_ALLOW_UNSIGNED_BUILD"


def main(env: Mapping[str, str] | None = None, keys: tuple[bytes, ...] | None = None) -> int:
    import update_sig
    env = os.environ if env is None else env
    if update_sig.enabled(keys):
        print("release keys: present (updates must carry a valid signature)")
        return 0
    if env.get(OVERRIDE) == "1":
        print(f"WARNING: no release keys in update_sig.PUBLIC_KEYS; {OVERRIDE}=1, so this TEST build goes on.")
        print("         Apps built from it check updates by SHA-256 only. Do not publish it.")
        return 0
    print("ERROR: no release keys in update_sig.PUBLIC_KEYS.")
    print("       Apps built from this tree would install updates without a signature check.")
    print("       Add the keys (python tools/sign_release.py --show-key KEY.pub), or set")
    print(f"       {OVERRIDE}=1 for a test build that is not published.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
