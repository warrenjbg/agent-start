r"""Sign an Agent Start installer for the in-app updater (run by the release owner on his own PC; the app never
imports this file).

    python tools/sign_release.py dist\AgentStart-Setup-X.Y.Z.exe --key PATH\TO\release_primary
        Hash the installer, build update_sig.manifest(updater.REPO, version, asset name, size, sha256), sign it
        with `ssh-keygen -Y sign -n agent-start-release` (ssh-keygen asks for the key's passphrase itself),
        write <installer>.sig next to the installer and verify it with update_sig.verify_release.
        Upload <installer>.sig to the same GitHub release as the installer.
    python tools/sign_release.py --show-key PATH\TO\release_primary.pub
        Print the lines to paste into update_sig.PUBLIC_KEYS, plus the key's SHA256 fingerprint.
    python tools/sign_release.py --new-key-help
        Print the ssh-keygen commands to create the primary and the backup key yourself.

Policy: this script never creates, reads or copies a private key; it only passes the key's path to ssh-keygen
(list arguments, no shell) and reads <key>.pub. A key file inside the repository folder is refused.
Options: --ssh-keygen PATH (default: Windows OpenSSH in System32\OpenSSH, else ssh-keygen on PATH),
--force (replace an existing <installer>.sig).

Exit codes: 0 signed, verified, and the key is in update_sig.PUBLIC_KEYS; 3 signed and verified, but the key is
NOT in update_sig.PUBLIC_KEYS (the app would refuse this release); 1 signing or verification failed;
2 refused (bad arguments, key inside the repository, unexpected installer name, existing .sig without --force).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import update_sig  # noqa: E402
from updater import REPO  # noqa: E402

_NAME_RE = re.compile(r"AgentStart-Setup-([0-9]{1,6}(?:\.[0-9]{1,6}){0,3})\.exe")
MAX_PUB_BYTES = 16 * 1024


class Refused(Exception):
    """A refusal with a message for the owner (exit code 2)."""


# ------------------------------------------------------------------------------------------- helpers

def version_from_name(installer: str) -> str:
    """'...\\AgentStart-Setup-1.4.0.exe' -> '1.4.0'. Raises Refused for any other file name."""
    name = os.path.basename(installer)
    m = _NAME_RE.fullmatch(name)
    if not m:
        raise Refused(f"installer name must be AgentStart-Setup-<version>.exe, got {name!r}")
    try:
        update_sig.manifest(REPO, m.group(1), name, 1, "0" * 64)      # same version rules as the app
    except update_sig.SigError as e:
        raise Refused(f"installer name {name!r}: {e}") from None
    return m.group(1)


def inside_folder(path: str | os.PathLike[str], folder: str | os.PathLike[str] = ROOT) -> bool:
    """True when `path` (after resolving links) is `folder` or lies anywhere below it."""
    p = os.path.normcase(os.path.realpath(path))
    r = os.path.normcase(os.path.realpath(folder))
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:                       # different drives
        return False


def find_ssh_keygen(override: str | None = None) -> str | None:
    """--ssh-keygen PATH when given (must be a file), else Windows OpenSSH in System32\\OpenSSH, else PATH."""
    if override:
        return os.path.abspath(override) if os.path.isfile(override) else None
    sysroot = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or "C:\\Windows"
    found = shutil.which("ssh-keygen", path=os.path.join(sysroot, "System32", "OpenSSH"))
    return found or shutil.which("ssh-keygen")


def openssh_key_blob(pub32: bytes) -> bytes:
    return b"".join(len(x).to_bytes(4, "big") + x for x in (b"ssh-ed25519", pub32))


def fingerprint(pub32: bytes) -> str:
    """The same SHA256 fingerprint `ssh-keygen -l -f KEY.pub` shows."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(openssh_key_blob(pub32)).digest()).decode().rstrip("=")


def key_literal(pub32: bytes) -> str:
    """Three lines to paste inside PUBLIC_KEYS = ( ... ): a fingerprint comment and the 32 bytes as one bytes
    literal split over two lines (adjacent literals concatenate), ending with a comma."""
    if len(pub32) != 32:
        raise ValueError("an Ed25519 public key is 32 bytes")
    a, b = ("".join(f"\\x{c:02x}" for c in half) for half in (pub32[:16], pub32[16:]))
    return f'    # {fingerprint(pub32)}\n    b"{a}"\n    b"{b}",'


def read_public_key(pub_path: str) -> bytes:
    """32 raw bytes from an `ssh-ed25519 AAAA... comment` file. Raises Refused."""
    try:
        with open(pub_path, "rb") as f:
            data = f.read(MAX_PUB_BYTES + 1)
    except OSError as e:
        raise Refused(f"cannot read public key {pub_path}: {e}") from None
    if len(data) > MAX_PUB_BYTES:
        raise Refused(f"{pub_path} is too large for a public key file")
    lines = [ln for ln in data.splitlines() if ln.strip()]
    if len(lines) != 1:
        raise Refused(f"{pub_path} must contain exactly one public key line")
    try:
        return update_sig.public_key_from_openssh(lines[0].decode("ascii"))
    except (update_sig.SigError, UnicodeDecodeError) as e:
        raise Refused(f"{pub_path} is not an ssh-ed25519 public key ({e})") from None


def size_and_sha256(path: str) -> tuple[int, str]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            n += len(chunk)
            h.update(chunk)
    return n, h.hexdigest()


def new_key_help_text() -> str:
    return r"""Create the two release keys yourself; this script never creates or reads a private key.
Each command asks for a passphrase: use a long one, and a different one for each key.
Commands are for cmd.exe; create the target folder first (ssh-keygen does not create folders).

1) Primary key: on your PC, in a folder OUTSIDE the repository and outside any synced/cloud folder:
   ssh-keygen -t ed25519 -a 100 -C "agent-start release primary" -f "%USERPROFILE%\agent-start-keys\release_primary"

2) Backup key: ideally on an offline machine, written straight to a removable drive (E: is only an example):
   ssh-keygen -t ed25519 -a 100 -C "agent-start release backup" -f "E:\agent-start-backup\release_backup"

Then:
 - Run  python tools/sign_release.py --show-key <key>.pub  for BOTH keys and paste the printed lines into
   PUBLIC_KEYS in update_sig.py (primary first, then backup). Only the .pub files are needed for that.
 - The backup private key lives offline only: e.g. two USB drives kept in different places, and the
   passphrase on paper stored apart from them. Never on your daily PC, never in the repository, never on
   GitHub or a cloud drive. Use it only to sign a release that replaces a lost or compromised primary key;
   that release ships the new key list.
 - Sign each release with the primary key:
   python tools/sign_release.py dist\AgentStart-Setup-X.Y.Z.exe --key "%USERPROFILE%\agent-start-keys\release_primary"
   and upload the .sig file next to the installer in the same GitHub release."""


# ------------------------------------------------------------------------------------------- signing

def sign_installer(installer: str, key: str, *, ssh_keygen: str, trusted_keys: tuple[bytes, ...] | None = None,
                   force: bool = False) -> int:
    """Sign `installer` with the private key at `key` (path only; ssh-keygen reads it). Returns the exit code
    described in the module docstring. trusted_keys defaults to update_sig.PUBLIC_KEYS."""
    try:
        return _sign(installer, key, ssh_keygen, trusted_keys, force)
    except Refused as e:
        print(f"REFUSED: {e}")
        return 2


def _sign(installer: str, key: str, ssh_keygen: str, trusted_keys: tuple[bytes, ...] | None, force: bool) -> int:
    if inside_folder(key):
        raise Refused(f"the key {key} is inside the repository folder {ROOT}. The private key must never be in "
                      "the repository: move it to a folder outside it (see --new-key-help).")
    if not os.path.isfile(key):
        raise Refused(f"key file not found: {key}")
    if not os.path.isfile(installer):
        raise Refused(f"installer not found: {installer}")
    name = os.path.basename(installer)
    version = version_from_name(installer)
    pub_path = key if key.lower().endswith(".pub") else key + ".pub"
    pub = read_public_key(pub_path)
    target = os.path.abspath(installer) + update_sig.SIG_SUFFIX
    if os.path.exists(target) and not force:
        raise Refused(f"{target} already exists (use --force to replace it)")
    size, sha = size_and_sha256(installer)
    try:
        msg = update_sig.manifest(REPO, version, name, size, sha)
    except update_sig.SigError as e:
        raise Refused(f"cannot build the manifest: {e}") from None

    tmp = tempfile.mkdtemp(prefix="agentstart-sign-")
    try:
        mf = os.path.join(tmp, name + ".manifest")
        with open(mf, "wb") as f:
            f.write(msg)
        argv = [ssh_keygen, "-Y", "sign", "-f", key, "-n", update_sig.NAMESPACE, mf]
        print(f"Signing with {ssh_keygen} (it asks for the key's passphrase) ...", flush=True)
        r = subprocess.run(argv, check=False)            # no shell; stdin/stdout/stderr stay on the console
        if r.returncode != 0:
            print(f"FAILED: ssh-keygen exited with code {r.returncode}")
            return 1
        try:
            with open(mf + ".sig", "rb") as fr:
                sig = fr.read(update_sig.MAX_SIG_BYTES + 1)
        except OSError as e:
            print(f"FAILED: ssh-keygen wrote no signature: {e}")
            return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    def verifies(sig_bytes: bytes) -> bool:
        return update_sig.verify_release(sig_bytes, repo=REPO, version=version, asset_name=name, size=size,
                                         sha256=sha, keys=(pub,))

    if not verifies(sig):
        print(f"FAILED: the new signature does not verify with {pub_path}; nothing was written")
        return 1
    part = target + ".part"
    with open(part, "wb") as fw:
        fw.write(sig)
    os.replace(part, target)
    with open(target, "rb") as fr:
        written = fr.read(update_sig.MAX_SIG_BYTES + 1)
    if not verifies(written):
        os.remove(target)
        print(f"FAILED: {target} does not verify after writing it; removed it")
        return 1

    trusted = trusted_keys if trusted_keys is not None else update_sig.PUBLIC_KEYS
    known = False
    for k in trusted:
        if isinstance(k, bytes) and hmac.compare_digest(k, pub):
            known = True
    print("Signed manifest:")
    print(msg.decode("ascii"), end="")
    print(f"Signature file: {target}")
    print(f"Signature verifies with {pub_path} ({fingerprint(pub)}).")
    if known:
        print("This key is one of update_sig.PUBLIC_KEYS: the app will accept this release.")
        return 0
    bar = "!" * 100
    print(f"{bar}\nWARNING: this key is NOT in update_sig.PUBLIC_KEYS. The in-app updater would REFUSE this release.\n"
          f"Sign with a key the app trusts, or add this key first (--show-key {pub_path}).\n{bar}")
    return 3


# ------------------------------------------------------------------------------------------- command line

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sign an Agent Start installer for the in-app updater.")
    ap.add_argument("installer", nargs="?", help="AgentStart-Setup-<version>.exe to sign")
    ap.add_argument("--key", help="private key file (outside the repository); KEY.pub must sit next to it")
    ap.add_argument("--show-key", metavar="KEY.pub", help="print the PUBLIC_KEYS lines for this public key")
    ap.add_argument("--new-key-help", action="store_true", help="print how to create the two release keys")
    ap.add_argument("--ssh-keygen", metavar="PATH", help="ssh-keygen to use")
    ap.add_argument("--force", action="store_true", help="replace an existing <installer>.sig")
    a = ap.parse_args(argv)
    if a.new_key_help:
        print(new_key_help_text())
        return 0
    if a.show_key:
        try:
            pub = read_public_key(a.show_key)
        except Refused as e:
            print(f"REFUSED: {e}")
            return 2
        print("Paste these lines inside PUBLIC_KEYS = ( ... ) in update_sig.py:")
        print(key_literal(pub))
        print(f"Fingerprint: {fingerprint(pub)}  (compare with: ssh-keygen -l -f {a.show_key})")
        return 0
    if not a.installer or not a.key:
        ap.print_usage()
        print("REFUSED: give INSTALLER and --key KEYFILE, or --show-key / --new-key-help")
        return 2
    kg = find_ssh_keygen(a.ssh_keygen)
    if kg is None:
        print("REFUSED: ssh-keygen not found" + (f" at {a.ssh_keygen}" if a.ssh_keygen else ""))
        return 2
    return sign_installer(a.installer, a.key, ssh_keygen=kg, force=a.force)


if __name__ == "__main__":
    sys.exit(main())
