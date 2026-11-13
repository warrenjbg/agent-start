"""Release signatures for the in-app updater: who published an installer, not only whether it arrived intact.

Stdlib only; no file, network or process access in this module. Policy:
  * The owner signs every release offline with `ssh-keygen -Y sign` (OpenSSH SSHSIG format, Ed25519 key,
    namespace NAMESPACE) using tools/sign_release.py. The private keys never touch GitHub or this repository.
  * What is signed is manifest(): one fixed ASCII text block binding repo, version, asset name, size and sha256.
    The app never reads a manifest from the network; it rebuilds these bytes itself from the values it already
    validated, so a signature for one version, file or size can not be replayed for another (no downgrades).
  * The app trusts only PUBLIC_KEYS (a primary key plus an offline backup). The public key embedded in a
    signature is compared byte for byte against that list; it is never trusted on its own.
  * The `.sig` text is bounded (MAX_SIG_BYTES) and parsed strictly: ssh-ed25519 key and signature types only,
    every length field bounds-checked, no trailing bytes, reserved field empty, hash sha512 (or sha256).
  * Ed25519 verification is RFC 8032 section 5.1.7 (cofactorless, the stricter check), rejecting non-canonical
    S (S >= L) and encodings that do not decode to a curve point. Only public inputs are processed, so the
    variable-time arithmetic leaks nothing secret.
  * A release without a valid signature is refused by the updater (bad and missing signatures are the same).

verify_ed25519() / verify_release() / enabled() never raise; manifest() / parse_sshsig() / signed_blob() /
public_key_from_openssh() raise SigError on any refusal.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import re
from dataclasses import dataclass

log = logging.getLogger("agent_start")

# Trusted Ed25519 release keys (32 raw bytes each): the primary key first, then the offline backup.
# These are PUBLIC keys. To change the list, run `python tools/sign_release.py --show-key KEY.pub` for each
# public key and paste the printed lines between the parentheses. Never put a private key here.
# An empty tuple means "no keys": the updater then checks an update by SHA-256 only (see updater._check).
PUBLIC_KEYS: tuple[bytes, ...] = (
    # primary - SHA256:arDzY/jLSjal3Rrmn6wnZqTJQKPMpDKxpJxkN3uNvaY
    b"\x78\xee\x51\x90\xa9\x8a\x30\x3b\x26\xa5\xad\x7e\x98\x45\x3e\xa8"
    b"\x3c\x0d\x2a\xc1\x77\x88\x65\xea\xe9\xcd\x37\x7d\x77\xf7\xc6\xd2",
    # backup - SHA256:WwJ+SKbTBfL2mqtfK56E5rg2nICBmXBHaJyxQ45+11g
    b"\x9b\x9a\x55\x27\xe4\x1f\xfc\x32\x42\x56\xb8\xef\x02\x33\x90\xc3"
    b"\x47\x73\x96\x62\x2b\xa6\xd4\xee\x13\x31\xb9\x02\x3f\xe3\x77\xe9",
)

NAMESPACE = "agent-start-release"
MAX_SIG_BYTES = 4096
SIG_SUFFIX = ".sig"
MANIFEST_TAG = "agent-start-release v1"
HASH_ALGS = ("sha512", "sha256")       # ssh-keygen signs with sha512 by default; sha256 on request

_ARMOR_BEGIN = "-----BEGIN SSH SIGNATURE-----"
_ARMOR_END = "-----END SSH SIGNATURE-----"
_SSHSIG_MAGIC = b"SSHSIG"
_KEY_TYPE = b"ssh-ed25519"

_REPO_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
_VER_RE = re.compile(r"(?:0|[1-9][0-9]{0,5})(?:\.(?:0|[1-9][0-9]{0,5})){0,3}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_B64_LINE_RE = re.compile(r"[A-Za-z0-9+/=]+")


class SigError(Exception):
    pass


@dataclass(frozen=True)
class SshSig:
    public_key: bytes      # 32 raw bytes, as embedded in the signature (NOT trusted by itself)
    namespace: str
    hash_alg: str
    signature: bytes       # 64 raw Ed25519 signature bytes


# ------------------------------------------------------------------------------------------- manifest

def manifest(repo: str, version: str, asset_name: str, size: int, sha256: str) -> bytes:
    """The exact bytes that are signed for one release asset (ASCII, LF line ends, fixed order, final LF):

        agent-start-release v1
        repo: owner/name
        version: 1.4.0
        asset: AgentStart-Setup-1.4.0.exe
        size: 12345678
        sha256: <64 lower-case hex>

    version must be canonical (1-4 dot-separated integers, at most 6 digits, no leading zeros) and the asset
    name must be AgentStart-Setup-<version>.exe. Raises SigError for any field that does not fit."""
    for label, v in (("repo", repo), ("version", version), ("asset", asset_name), ("sha256", sha256)):
        if not isinstance(v, str):
            raise SigError(f"{label} is not a string")
        if any(c in v for c in "\r\n\x00"):
            raise SigError(f"{label} contains CR, LF or NUL")
    if not _REPO_RE.fullmatch(repo) or repo.split("/", 1)[1] in (".", ".."):
        raise SigError("bad repo")
    if not _VER_RE.fullmatch(version):
        raise SigError("bad version")
    if asset_name != f"AgentStart-Setup-{version}.exe":
        raise SigError("asset name does not match the version")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise SigError("bad size")
    if not _SHA_RE.fullmatch(sha256):
        raise SigError("bad sha256")
    text = (f"{MANIFEST_TAG}\nrepo: {repo}\nversion: {version}\nasset: {asset_name}\nsize: {size}\n"
            f"sha256: {sha256}\n")
    return text.encode("ascii")


# ------------------------------------------------------------------------------------------- Ed25519 (verify only)

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, -1, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)

_Point = tuple[int, int, int, int]       # extended twisted-Edwards coordinates (X, Y, Z, T), RFC 8032 5.1.4


def _add(p: _Point, q: _Point) -> _Point:
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a, b = (y1 - x1) * (y2 - x2) % _P, (y1 + x1) * (y2 + x2) % _P
    c, d = 2 * t1 * t2 * _D % _P, 2 * z1 * z2 % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return e * f % _P, g * h % _P, f * g % _P, e * h % _P


def _mul(s: int, p: _Point) -> _Point:
    q: _Point = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _equal(p: _Point, q: _Point) -> bool:
    return (p[0] * q[2] - q[0] * p[2]) % _P == 0 and (p[1] * q[2] - q[1] * p[2]) % _P == 0


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:                                   # non-canonical y encoding
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, -1, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P:
        return None
    return _P - x if (x & 1) != sign else x


def _decompress(b: bytes) -> _Point | None:
    if len(b) != 32:
        return None
    y = int.from_bytes(b, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


_GY = 4 * pow(5, -1, _P) % _P
_GX = _recover_x(_GY, 0)
if _GX is None:                                   # cannot happen; keeps the base point well-typed
    raise RuntimeError("Ed25519 base point does not decode")
_G: _Point = (_GX, _GY, 1, _GX * _GY % _P)


def verify_ed25519(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """RFC 8032 Ed25519 verification. False for wrong lengths or types, a public key or R that does not decode,
    S >= L, or a signature that does not match. Never raises."""
    try:
        if not (isinstance(public_key, bytes) and isinstance(message, bytes) and isinstance(signature, bytes)):
            return False
        if len(public_key) != 32 or len(signature) != 64:
            return False
        a, r = _decompress(public_key), _decompress(signature[:32])
        if a is None or r is None:
            return False
        s = int.from_bytes(signature[32:], "little")
        if s >= _L:                                # non-canonical S (malleability)
            return False
        k = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little") % _L
        return _equal(_mul(s, _G), _add(r, _mul(k, a)))
    except Exception:  # noqa: BLE001  never raises
        log.debug("verify_ed25519 failed unexpectedly", exc_info=True)
        return False


# ------------------------------------------------------------------------------------------- SSHSIG

def _string(b: bytes) -> bytes:
    return len(b).to_bytes(4, "big") + b


def _take(buf: bytes, i: int, what: str) -> tuple[bytes, int]:
    if i + 4 > len(buf):
        raise SigError(f"truncated {what} length")
    n = int.from_bytes(buf[i:i + 4], "big")
    if n > len(buf) - i - 4:
        raise SigError(f"{what} length out of bounds")
    return buf[i + 4:i + 4 + n], i + 4 + n


def _ascii_text(data: str | bytes) -> str:
    if isinstance(data, (bytes, bytearray)):
        if len(data) > MAX_SIG_BYTES:
            raise SigError("signature too large")
        try:
            return bytes(data).decode("ascii")
        except UnicodeDecodeError:
            raise SigError("signature is not ASCII") from None
    if isinstance(data, str):
        if len(data) > MAX_SIG_BYTES:
            raise SigError("signature too large")
        if not data.isascii():
            raise SigError("signature is not ASCII")
        return data
    raise SigError("signature is not text")


def parse_sshsig(armored: str | bytes) -> SshSig:
    """Strict parser for an `ssh-keygen -Y sign` signature (ssh-ed25519 only). Raises SigError."""
    text = _ascii_text(armored).strip()
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")]
    if len(lines) < 3 or lines[0] != _ARMOR_BEGIN or lines[-1] != _ARMOR_END:
        raise SigError("bad armor")
    body = lines[1:-1]
    if not all(_B64_LINE_RE.fullmatch(ln) for ln in body):
        raise SigError("bad base64 line")
    try:
        blob = base64.b64decode("".join(body), validate=True)
    except (binascii.Error, ValueError):
        raise SigError("bad base64") from None
    if blob[:6] != _SSHSIG_MAGIC:
        raise SigError("bad magic")
    if len(blob) < 10 or int.from_bytes(blob[6:10], "big") != 1:
        raise SigError("unsupported version")
    pk_blob, i = _take(blob, 10, "public key")
    ns, i = _take(blob, i, "namespace")
    reserved, i = _take(blob, i, "reserved")
    hash_alg, i = _take(blob, i, "hash algorithm")
    sig_blob, i = _take(blob, i, "signature")
    if i != len(blob):
        raise SigError("trailing bytes")
    if reserved:
        raise SigError("reserved field not empty")
    key_type, j = _take(pk_blob, 0, "key type")
    key, j = _take(pk_blob, j, "key")
    if key_type != _KEY_TYPE:
        raise SigError("key type is not ssh-ed25519")
    if j != len(pk_blob) or len(key) != 32:
        raise SigError("bad ssh-ed25519 key")
    sig_type, j = _take(sig_blob, 0, "signature type")
    raw, j = _take(sig_blob, j, "signature bytes")
    if sig_type != _KEY_TYPE:
        raise SigError("signature type is not ssh-ed25519")
    if j != len(sig_blob) or len(raw) != 64:
        raise SigError("bad ssh-ed25519 signature")
    try:
        ns_s, alg_s = ns.decode("ascii"), hash_alg.decode("ascii")
    except UnicodeDecodeError:
        raise SigError("namespace or hash algorithm is not ASCII") from None
    return SshSig(public_key=key, namespace=ns_s, hash_alg=alg_s, signature=raw)


def signed_blob(namespace: str, hash_alg: str, message: bytes) -> bytes:
    """The SSHSIG to-be-signed structure (OpenSSH PROTOCOL.sshsig):
    "SSHSIG" | string(namespace) | string(reserved = "") | string(hash_alg) | string(H(message)).
    Raises SigError for a hash algorithm other than sha512 / sha256."""
    if hash_alg not in HASH_ALGS:
        raise SigError("unsupported hash algorithm")
    if not isinstance(namespace, str) or not namespace or not namespace.isascii():
        raise SigError("bad namespace")
    if not isinstance(message, bytes):
        raise SigError("message is not bytes")
    digest = hashlib.new(hash_alg, message).digest()
    return (_SSHSIG_MAGIC + _string(namespace.encode("ascii")) + _string(b"") + _string(hash_alg.encode("ascii"))
            + _string(digest))


# ------------------------------------------------------------------------------------------- keys

def _valid_key(k: object) -> bool:
    return isinstance(k, bytes) and len(k) == 32 and _decompress(k) is not None


def _trusted(keys: object) -> tuple[bytes, ...]:
    if keys is None:
        keys = PUBLIC_KEYS
    if not isinstance(keys, (tuple, list)):
        return ()
    return tuple(k for k in keys if _valid_key(k))


def enabled(keys: tuple[bytes, ...] | None = None) -> bool:
    """True when at least one valid 32-byte Ed25519 public key is configured (default: PUBLIC_KEYS).
    Never raises."""
    try:
        return bool(_trusted(keys))
    except Exception:  # noqa: BLE001  never raises
        return False


def public_key_from_openssh(line: str) -> bytes:
    """'ssh-ed25519 AAAA... [comment]' -> the 32 raw key bytes. Strict; raises SigError."""
    if isinstance(line, bytes):
        try:
            line = line.decode("ascii")
        except UnicodeDecodeError:
            raise SigError("public key line is not ASCII") from None
    if not isinstance(line, str):
        raise SigError("public key line is not text")
    line = line.strip()
    if not line or len(line) > 4096 or any(c in line for c in "\r\n\x00"):
        raise SigError("expected exactly one public key line")
    parts = line.split(None, 2)
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise SigError("not an ssh-ed25519 public key line")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        raise SigError("bad base64 in public key") from None
    key_type, j = _take(blob, 0, "key type")
    key, j = _take(blob, j, "key")
    if key_type != _KEY_TYPE or len(key) != 32 or j != len(blob):
        raise SigError("not an ssh-ed25519 public key")
    if _decompress(key) is None:
        raise SigError("public key is not a valid Ed25519 point")
    return key


# ------------------------------------------------------------------------------------------- release check

def verify_release(sig_text: str | bytes, *, repo: str, version: str, asset_name: str, size: int, sha256: str,
                   keys: tuple[bytes, ...] | None = None) -> bool:
    """True only when sig_text is a valid SSHSIG signature, namespace NAMESPACE, hash sha512/sha256, made by
    one of `keys` (default PUBLIC_KEYS) over manifest(repo, version, asset_name, size, sha256). Logs the
    reason for a refusal at INFO (never the signature itself). Never raises."""
    what = version if isinstance(version, str) and _VER_RE.fullmatch(version) else "?"
    try:
        trusted = _trusted(keys)
        if not trusted:
            return _refuse(what, "no trusted release keys configured")
        try:
            msg = manifest(repo, version, asset_name, size, sha256)
        except SigError as e:
            return _refuse(what, f"release data not signable ({e})")
        try:
            sig = parse_sshsig(sig_text)
        except SigError as e:
            return _refuse(what, f"malformed signature ({e})")
        if sig.namespace != NAMESPACE:
            return _refuse(what, "wrong signature namespace")
        if sig.hash_alg not in HASH_ALGS:
            return _refuse(what, "unsupported signature hash algorithm")
        match = None
        for k in trusted:                       # no early exit: compare against every trusted key
            if hmac.compare_digest(sig.public_key, k) and match is None:
                match = k
        if match is None:
            return _refuse(what, "signed by a key that is not trusted")
        if not verify_ed25519(match, signed_blob(sig.namespace, sig.hash_alg, msg), sig.signature):
            return _refuse(what, "signature does not match this release (version, file, size or sha256)")
        log.info("update signature verified for %s", what)
        return True
    except Exception as e:  # noqa: BLE001  never raises
        return _refuse(what, f"unexpected error {type(e).__name__}")


def _refuse(what: str, reason: str) -> bool:
    log.info("update signature refused for %s: %s", what, reason)
    return False
