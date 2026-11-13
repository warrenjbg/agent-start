"""Self-check for update_sig.py and tools/sign_release.py. Run: .venv\\Scripts\\python.exe tests\\test_update_sig.py

Safe by construction:
  * No network, no engines, no app processes. The only programs started are ssh-keygen (list argv, no shell)
    for throwaway keys this suite creates itself; nothing else is spawned.
  * Every key used here is a THROWAWAY key: either generated at run time (unencrypted, `-N ""`) inside this
    suite's temp folder, or the embedded fixture below whose private half was deleted right after it signed.
    The suite never reads ~/.ssh or any real key.
  * Everything is written under one temp folder (outside the repository, no game-library folder names in its
    path), removed at the end. AGENTSTART_TEST_NO_SSHKEYGEN=1 runs the fixture-only path (as on a CI runner
    without OpenSSH); the ssh-keygen checks then print SKIP lines and the suite still passes.
"""
from __future__ import annotations

import ast
import base64
import contextlib
import hashlib
import io
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import update_sig  # noqa: E402
import updater  # noqa: E402
from update_sig import SigError  # noqa: E402

import sign_release  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
TMP = Path(tempfile.mkdtemp(prefix="agentstart-sig-test-"))
GAME_DIR_NAMES = {"steamapps", "steamlibrary", "epic games", "gog galaxy"}


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    return bool(cond)


def skip(name: str, why: str) -> None:
    print(f"  [SKIP] {name} -- {why}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def raises(fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
    except SigError:
        return True
    return False


# ----------------------------------------------------------------------------------------- fixtures

# RFC 8032 section 7.1 (public key, message, signature), copied from the RFC text.
RFC8032_VECTORS = [
    ("TEST 1",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "",
     ("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0"
      "595bbe24655141438e7a100b")),
    ("TEST 2",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     ("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eae"
      "b4302aeeb00d291612bb0c00")),
    ("TEST 3",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
     "af82",
     ("6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9"
      "716ed28dc027beceea1ec40a")),
    ("TEST 1024",
     "278117fc144c72340f67d0f2316e8386ceffbf2b2428c9c51fef7c597f1d426e",
     ("08b8b2b733424243760fe426a4b54908632110a66c2f6591eabd3345e3e4eb98fa6e264bf09efe12ee50f8f54e9f77b1e355f6c5"
      "0544e23fb1433ddf73be84d879de7c0046dc4996d9e773f4bc9efe5738829adb26c81b37c93a1b270b20329d658675fc6ea534e0"
      "810a4432826bf58c941efb65d57a338bbd2e26640f89ffbc1a858efcb8550ee3a5e1998bd177e93a7363c344fe6b199ee5d02e82"
      "d522c4feba15452f80288a821a579116ec6dad2b3b310da903401aa62100ab5d1a36553e06203b33890cc9b832f79ef80560ccb9"
      "a39ce767967ed628c6ad573cb116dbefefd75499da96bd68a8a97b928a8bbc103b6621fcde2beca1231d206be6cd9ec7aff6f6c9"
      "4fcd7204ed3455c68c83f4a41da4af2b74ef5c53f1d8ac70bdcb7ed185ce81bd84359d44254d95629e9855a94a7c1958d1f8ada5"
      "d0532ed8a5aa3fb2d17ba70eb6248e594e1a2297acbbb39d502f1a8c6eb6f1ce22b3de1a1f40cc24554119a831a9aad6079cad88"
      "425de6bde1a9187ebb6092cf67bf2b13fd65f27088d78b7e883c8759d2c4f5c65adb7553878ad575f9fad878e80a0c9ba63bcbcc"
      "2732e69485bbc9c90bfbd62481d9089beccf80cfe2df16a2cf65bd92dd597b0707e0917af48bbb75fed413d238f5555a7a569d80"
      "c3414a8d0859dc65a46128bab27af87a71314f318c782b23ebfe808b82b0ce26401d2e22f04d83d1255dc51addd3b75a2b1ae078"
      "4504df543af8969be3ea7082ff7fc9888c144da2af58429ec96031dbcad3dad9af0dcbaaaf268cb8fcffead94f3c7ca495e056a9"
      "b47acdb751fb73e666c6c655ade8297297d07ad1ba5e43f1bca32301651339e22904cc8c42f58c30c04aafdb038dda0847dd988d"
      "cda6f3bfd15c4b4c4525004aa06eeff8ca61783aacec57fb3d1f92b0fe2fd1a85f6724517b65e614ad6808d6f6ee34dff7310fdc"
      "82aebfd904b01e1dc54b2927094b2db68d6f903b68401adebf5a7e08d78ff4ef5d63653a65040cf9bfd4aca7984a74d371459867"
      "80fc0b16ac451649de6188a7dbdf191f64b5fc5e2ab47b57f7f7276cd419c17a3ca8e1b939ae49e488acba6b965610b5480109c8"
      "b17b80e1b7b750dfc7598d5d5011fd2dcc5600a32ef5b52a1ecc820e308aa342721aac0943bf6686b64b2579376504ccc493d97e"
      "6aed3fb0f9cd71a43dd497f01f17c0e2cb3797aa2a2f256656168e6c496afc5fb93246f6b1116398a346f1a641f3b041e989f791"
      "4f90cc2c7fff357876e506b50d334ba77c225bc307ba537152f3f1610e4eafe595f6d9d90d11faa933a15ef1369546868a7f3a45"
      "a96768d40fd9d03412c091c6315cf4fde7cb68606937380db2eaaa707b4c4185c32eddcdd306705e4dc1ffc872eeee475a64dfac"
      "86aba41c0618983f8741c5ef68d3a101e8a3b8cac60c905c15fc910840b94c00a0b9d0"),
     ("0aab4c900501b3e24d7cdf4663326a3a87df5e4843b2cbdb67cbf6e460fec350aa5371b1508f9f4528ecea23c436d94b5e8fcd4f"
      "681e30a6ac00a9704a188a03")),
    ("TEST SHA(abc)",
     "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
     ("ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a2192992a274fc1a836ba3c23a3feebbd454d4423"
      "643ce80e2a9ac94fa54ca49f"),
     ("dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b58909351fc9ac90b3ecfdfbc7c66431e0303dca179c"
      "138ac17ad9bef1177331a704")),
]
VECS = [(n, bytes.fromhex(p), bytes.fromhex(m), bytes.fromhex(s)) for n, p, m, s in RFC8032_VECTORS]
L = 2 ** 252 + 27742317777372353535851937790883648493

# Throwaway fixture made once with Windows OpenSSH `ssh-keygen -Y sign` (key created with -N ""; the private key
# was deleted right after signing). It signs manifest(**FX_INPUTS).
FX_INPUTS = dict(repo="example-owner/example-repo", version="2.5.1", asset_name="AgentStart-Setup-2.5.1.exe",
                 size=123456, sha256="3f" * 16 + "a5" * 16)
FX_PUB_LINE = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJjUbA9TPCfk8ZiU/vbsd5Lr8zzfwAUoiI10wj1YKQHm throwaway-fixture"
FX_SIG512 = """-----BEGIN SSH SIGNATURE-----
U1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgmNRsD1M8J+TxmJT+9ux3kuvzPN
/ABSiIjXTCPVgpAeYAAAATYWdlbnQtc3RhcnQtcmVsZWFzZQAAAAAAAAAGc2hhNTEyAAAA
UwAAAAtzc2gtZWQyNTUxOQAAAEAcGVOGzcsI1LM/IJ9D7Pu+48SoEpqza7fnF0kFPJ6u1S
SktbGFWEDP4Nl2YHBkhRxeIfGAvaIJTyz3GwOe700C
-----END SSH SIGNATURE-----
"""
FX_SIG256 = """-----BEGIN SSH SIGNATURE-----
U1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgmNRsD1M8J+TxmJT+9ux3kuvzPN
/ABSiIjXTCPVgpAeYAAAATYWdlbnQtc3RhcnQtcmVsZWFzZQAAAAAAAAAGc2hhMjU2AAAA
UwAAAAtzc2gtZWQyNTUxOQAAAEADPBSgd54Pq9Ua58gqju76qex5FBt6ccOhNarXp33ps+
D6lBFBLTCoxAIC3SmFD/hlmSjzKlMDex/dLMpaDhEG
-----END SSH SIGNATURE-----
"""
FX_SIG_NS_GIT = """-----BEGIN SSH SIGNATURE-----
U1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgmNRsD1M8J+TxmJT+9ux3kuvzPN
/ABSiIjXTCPVgpAeYAAAADZ2l0AAAAAAAAAAZzaGE1MTIAAABTAAAAC3NzaC1lZDI1NTE5
AAAAQMwNBOFeBZvC2OKYbUPCxwKPX5t9SzdZ/bXWCeFl0hTr2Y7hkS9p8F/ROWUP8KmhIS
qpJhN1l2oEa/HU9IsH2QQ=
-----END SSH SIGNATURE-----
"""
FX_SIG_ECDSA = """-----BEGIN SSH SIGNATURE-----
U1NIU0lHAAAAAQAAAGgAAAATZWNkc2Etc2hhMi1uaXN0cDI1NgAAAAhuaXN0cDI1NgAAAE
EEX5c5c6EFP2SpuwD2rRbozlSKGXQthzcrpRgsLPQyOzLSTGxPMcQ8dUbijBjcrm6i1X2l
5W36bCzvSlmvzXEK1QAAABNhZ2VudC1zdGFydC1yZWxlYXNlAAAAAAAAAAZzaGE1MTIAAA
BkAAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAABJAAAAIQCxfzLe0N1zMjWsfmXoKQblMryh
YD/s563CsofOXSC+owAAACAlzHgoXEG6ts20JfNX4fpeBteEzYg3NE9SeqLwsFXPhA==
-----END SSH SIGNATURE-----
"""
FX_PUB = update_sig.public_key_from_openssh(FX_PUB_LINE)
OTHER_PUB = VECS[0][1]                       # a valid Ed25519 key that did not sign the fixture


def s(b: bytes) -> bytes:
    return len(b).to_bytes(4, "big") + b


def sshsig(*, magic=b"SSHSIG", version=1, key_type=b"ssh-ed25519", key=None, key_extra=b"",
           ns=b"agent-start-release", reserved=b"", hash_alg=b"sha512", sig_type=b"ssh-ed25519", sig=None,
           sig_extra=b"", trailing=b"") -> bytes:
    """Build an SSHSIG blob field by field (defaults: the FX_SIG512 fixture)."""
    key = FX_PUB if key is None else key
    sig = update_sig.parse_sshsig(FX_SIG512).signature if sig is None else sig
    pk = s(key_type) + s(key) + key_extra
    sg = s(sig_type) + s(sig) + sig_extra
    return magic + version.to_bytes(4, "big") + s(pk) + s(ns) + s(reserved) + s(hash_alg) + s(sg) + trailing


def armor(blob: bytes) -> str:
    t = base64.b64encode(blob).decode()
    return ("-----BEGIN SSH SIGNATURE-----\n" + "\n".join(t[i:i + 70] for i in range(0, len(t), 70))
            + "\n-----END SSH SIGNATURE-----\n")


def vr(sig, keys=(FX_PUB,), **over) -> bool:
    return update_sig.verify_release(sig, keys=keys, **{**FX_INPUTS, **over})


# ----------------------------------------------------------------------------------------- tests

def test_tmp_safety() -> None:
    section("temp folder safety")
    parts = {p.lower() for p in TMP.resolve().parts}
    check("temp folder is outside the repository", not sign_release.inside_folder(TMP, ROOT), str(TMP))
    check("temp folder path has no game-library component", not (parts & GAME_DIR_NAMES), str(TMP))


def test_ed25519() -> None:
    section("Ed25519 (RFC 8032 section 7.1)")
    check("five RFC 8032 7.1 vectors embedded", len(VECS) == 5)
    for name, pk, msg, sig in VECS:
        check(f"{name}: verifies", update_sig.verify_ed25519(pk, msg, sig))
        check(f"{name}: tampered message", not update_sig.verify_ed25519(pk, msg + b"x", sig))
        bad = bytearray(sig)
        bad[5] ^= 1
        check(f"{name}: tampered R", not update_sig.verify_ed25519(pk, msg, bytes(bad)))
        bad = bytearray(sig)
        bad[40] ^= 1
        check(f"{name}: tampered S", not update_sig.verify_ed25519(pk, msg, bytes(bad)))
        other = VECS[0][1] if pk != VECS[0][1] else VECS[1][1]
        check(f"{name}: wrong public key", not update_sig.verify_ed25519(other, msg, sig))
    _, pk, msg, sig = VECS[1]
    s_int = int.from_bytes(sig[32:], "little")
    check("non-canonical S (S + L) rejected",
          not update_sig.verify_ed25519(pk, msg, sig[:32] + (s_int + L).to_bytes(32, "little")))
    check("S == L rejected", not update_sig.verify_ed25519(pk, msg, sig[:32] + L.to_bytes(32, "little")))
    check("S = 2^256 - 1 rejected", not update_sig.verify_ed25519(pk, msg, sig[:32] + b"\xff" * 32))
    check("key 31 bytes rejected", not update_sig.verify_ed25519(pk[:31], msg, sig))
    check("key 33 bytes rejected", not update_sig.verify_ed25519(pk + b"\x00", msg, sig))
    check("signature 63 bytes rejected", not update_sig.verify_ed25519(pk, msg, sig[:63]))
    check("signature 65 bytes rejected", not update_sig.verify_ed25519(pk, msg, sig + b"\x00"))
    check("empty everything rejected", not update_sig.verify_ed25519(b"", b"", b""))
    check("all-zero signature rejected", not update_sig.verify_ed25519(pk, msg, b"\x00" * 64))
    check("junk signature rejected", not update_sig.verify_ed25519(pk, msg, hashlib.sha512(b"junk").digest()))
    check("non-decoding key (y >= p) rejected", not update_sig.verify_ed25519(b"\xff" * 32, msg, sig))
    check("non-decoding R rejected", not update_sig.verify_ed25519(pk, msg, b"\xff" * 32 + sig[32:]))
    no_x = bytes.fromhex("02" + "00" * 31)              # y = 2 is not on the curve
    check("key that is not a curve point rejected", not update_sig.verify_ed25519(no_x, msg, sig))
    check("str / None / bytearray inputs rejected without raising",
          not update_sig.verify_ed25519(pk.hex(), msg, sig)                       # type: ignore[arg-type]
          and not update_sig.verify_ed25519(pk, None, sig)                        # type: ignore[arg-type]
          and not update_sig.verify_ed25519(bytearray(pk), msg, sig))             # type: ignore[arg-type]


def test_manifest() -> None:
    section("manifest()")
    m = update_sig.manifest("owner-1/agent-start", "1.4.0", "AgentStart-Setup-1.4.0.exe", 12345678, "ab" * 32)
    want = (b"agent-start-release v1\n"
            b"repo: owner-1/agent-start\n"
            b"version: 1.4.0\n"
            b"asset: AgentStart-Setup-1.4.0.exe\n"
            b"size: 12345678\n"
            b"sha256: " + b"ab" * 32 + b"\n")
    check("manifest pinned byte for byte", m == want, repr(m))
    check("manifest is ASCII with LF only and a trailing newline",
          m.isascii() and b"\r" not in m and m.endswith(b"\n") and m.count(b"\n") == 6)
    check("four-part version accepted",
          raises(update_sig.manifest, "a/b", "1.2.3.4", "AgentStart-Setup-1.2.3.4.exe", 1, "0" * 64) is False)
    check("version 0 accepted", not raises(update_sig.manifest, "a/b", "0", "AgentStart-Setup-0.exe", 1, "0" * 64))
    good = dict(repo="a/b", version="1.2", asset_name="AgentStart-Setup-1.2.exe", size=5, sha256="0" * 64)
    bad: list[tuple[str, dict]] = []
    for r in ("", "ab", "a/b/c", "/b", "a/", "-a/b", "a b/c", "a/b c", "a/..", "a/.", "ä/b", "a\\b", "a/b\n",
              "a/b\r", "a/b\x00", "x" * 40 + "/b", "a/" + "b" * 101, None, 5):
        bad.append((f"repo {r!r}", {"repo": r}))
    for v in ("", "1.2.3.4.5", "01.2", "1.02", "1234567", "v1.2", "1..2", ".1", "1.", "1.2 ", " 1.2", "1.2\n",
              "\u0661.2", "1,2", "-1", None, 1.2):
        bad.append((f"version {v!r}", {"version": v, "asset_name": f"AgentStart-Setup-{v}.exe"}))
    for a in ("AgentStart-Setup-1.3.exe", "agentstart-setup-1.2.exe", "AgentStart-Setup-1.2.exe.sig",
              "AgentStart-Setup-1.2.EXE", "dist/AgentStart-Setup-1.2.exe", "AgentStart-Setup-1.2.0.exe",
              "AgentStart-Setup-1.2.exe\n", "AgentStart-Setup-1.2.exe\x00", "", None):
        bad.append((f"asset {a!r}", {"asset_name": a}))
    for z in (0, -1, True, False, 1.0, "5", None, 2 ** 64 * -1):
        bad.append((f"size {z!r}", {"size": z}))
    for h in ("AB" * 32, "0" * 63, "0" * 65, "g" * 64, "0" * 63 + "\n", " " + "0" * 64, "", None, b"0" * 64):
        bad.append((f"sha256 {h!r}", {"sha256": h}))
    check("the good base case is accepted", not raises(update_sig.manifest, **good))
    for label, over in bad:
        check(f"refused: {label}", raises(update_sig.manifest, **{**good, **over}))


def test_parse_sshsig() -> None:
    section("parse_sshsig()")
    p = update_sig.parse_sshsig(FX_SIG512)
    check("fixture parses: key, namespace, hash, 64-byte signature",
          p.public_key == FX_PUB and p.namespace == "agent-start-release" and p.hash_alg == "sha512"
          and len(p.signature) == 64)
    check("sha256 fixture parses", update_sig.parse_sshsig(FX_SIG256).hash_alg == "sha256")
    check("bytes input parses", update_sig.parse_sshsig(FX_SIG512.encode()) == p)
    check("CRLF line ends parse", update_sig.parse_sshsig(FX_SIG512.replace("\n", "\r\n")) == p)
    check("test builder reproduces the fixture byte for byte", armor(sshsig()) == FX_SIG512)
    try:
        p.namespace = "x"                                      # type: ignore[misc]
        frozen = False
    except Exception:  # noqa: BLE001
        frozen = True
    check("SshSig is frozen", frozen)
    sig64 = p.signature
    bad = {
        "bad magic": armor(sshsig(magic=b"SSHSIH")),
        "version 2": armor(sshsig(version=2)),
        "version 0": armor(sshsig(version=0)),
        "reserved field not empty": armor(sshsig(reserved=b"x")),
        "trailing bytes after the signature": armor(sshsig(trailing=b"\x00")),
        "key type ssh-rsa": armor(sshsig(key_type=b"ssh-rsa")),
        "key 31 bytes": armor(sshsig(key=FX_PUB[:31])),
        "key 33 bytes": armor(sshsig(key=FX_PUB + b"\x00")),
        "extra bytes in the key blob": armor(sshsig(key_extra=b"\x00")),
        "signature type rsa-sha2-512": armor(sshsig(sig_type=b"rsa-sha2-512")),
        "signature 63 bytes": armor(sshsig(sig=sig64[:63])),
        "extra bytes in the signature blob": armor(sshsig(sig_extra=b"\x00\x00\x00\x00")),
        "truncated blob": armor(sshsig()[:-5]),
        "huge length field": armor(b"SSHSIG" + (1).to_bytes(4, "big") + b"\xff\xff\xff\xf0" + b"\x00" * 40),
        "only the magic": armor(b"SSHSIG"),
        "empty body": "-----BEGIN SSH SIGNATURE-----\n-----END SSH SIGNATURE-----\n",
        "missing BEGIN": FX_SIG512.replace("-----BEGIN SSH SIGNATURE-----\n", ""),
        "missing END": FX_SIG512.replace("-----END SSH SIGNATURE-----\n", ""),
        "wrong armor label": FX_SIG512.replace("SSH SIGNATURE", "PGP SIGNATURE"),
        "text before the armor": "hello\n" + FX_SIG512,
        "trailing garbage after END": FX_SIG512 + "garbage\n",
        "non-base64 character": FX_SIG512.replace("U1NIU0lH", "U1NIU0l*"),
        "space inside a base64 line": FX_SIG512.replace("U1NIU0lH", "U1NI U0lH"),
        "broken base64 padding": FX_SIG512.replace("700C\n", "700C=\n"),
        "non-ASCII": FX_SIG512.replace("U1NIU0lH", "U1NIU0l\u00e9"),
        "oversized": FX_SIG512 + " " * update_sig.MAX_SIG_BYTES,
        "empty": "",
    }
    for label, text in bad.items():
        check(f"refused: {label}", raises(update_sig.parse_sshsig, text))
    check("refused: non-UTF8 bytes", raises(update_sig.parse_sshsig, b"\xff\xfe" + FX_SIG512.encode()))
    check("refused: oversized bytes", raises(update_sig.parse_sshsig, FX_SIG512.encode() + b" " * 5000))
    check("refused: None", raises(update_sig.parse_sshsig, None))
    check("refused: real ecdsa signature (key type)", raises(update_sig.parse_sshsig, FX_SIG_ECDSA))
    check("a non-ASCII namespace is refused",
          raises(update_sig.parse_sshsig, armor(sshsig(ns="n\u00e4".encode()))))


def test_signed_blob() -> None:
    section("signed_blob()")
    msg = update_sig.manifest(**FX_INPUTS)
    b = update_sig.signed_blob("agent-start-release", "sha512", msg)
    want = (b"SSHSIG" + s(b"agent-start-release") + s(b"") + s(b"sha512") + s(hashlib.sha512(msg).digest()))
    check("structure pinned", b == want)
    p = update_sig.parse_sshsig(FX_SIG512)
    check("fixture signature verifies over signed_blob(manifest)", update_sig.verify_ed25519(FX_PUB, b, p.signature))
    p2 = update_sig.parse_sshsig(FX_SIG256)
    check("sha256 fixture verifies over its signed_blob",
          update_sig.verify_ed25519(FX_PUB, update_sig.signed_blob("agent-start-release", "sha256", msg),
                                    p2.signature))
    check("md5 refused", raises(update_sig.signed_blob, "agent-start-release", "md5", msg))
    check("empty namespace refused", raises(update_sig.signed_blob, "", "sha512", msg))
    check("str message refused", raises(update_sig.signed_blob, "agent-start-release", "sha512", "x"))


def test_verify_release() -> None:
    section("verify_release()")
    recs: list[logging.LogRecord] = []

    class Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            recs.append(record)

    lg = logging.getLogger("agent_start")
    h = Grab(level=logging.DEBUG)
    old_level = lg.level
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    try:
        check("good signature (sha512) accepted", vr(FX_SIG512))
        check("good signature (sha256) accepted", vr(FX_SIG256))
        check("good signature as bytes accepted", vr(FX_SIG512.encode()))
        check("CRLF copy of a good signature accepted", vr(FX_SIG512.replace("\n", "\r\n")))
        check("trusted set (other key, signing key) accepted", vr(FX_SIG512, keys=(OTHER_PUB, FX_PUB)))
        check("invalid entries in the trusted set are ignored",
              vr(FX_SIG512, keys=(b"\x00" * 31, "x" * 32, FX_PUB)))            # type: ignore[arg-type]
        recs.clear()
        check("empty trusted set refused", not vr(FX_SIG512, keys=()))
        check("refusal logged at INFO", any(r.levelno == logging.INFO and "refused" in r.getMessage() for r in recs))
        check("default keys (shipped PUBLIC_KEYS, fixture key not in it) refused",
              not update_sig.verify_release(FX_SIG512, **FX_INPUTS))
        check("key not in the trusted set refused", not vr(FX_SIG512, keys=(OTHER_PUB,)))
        check("trusted set of only invalid entries refused", not vr(FX_SIG512, keys=(b"\x00" * 31,)))
        check("wrong namespace (same key, namespace 'git') refused", not vr(FX_SIG_NS_GIT))
        # Replay / downgrade: the signature is bound to version, asset, size and sha256 of ONE installer.
        check("replay: genuine 2.5.1 signature + installer re-published as tag 9.9.9 refused",
              not vr(FX_SIG512, version="9.9.9", asset_name="AgentStart-Setup-9.9.9.exe"))
        check("downgrade: 2.5.1 signature presented for release 2.5.0 refused",
              not vr(FX_SIG512, version="2.5.0", asset_name="AgentStart-Setup-2.5.0.exe"))
        check("same version written differently (2.5.1.0) refused",
              not vr(FX_SIG512, version="2.5.1.0", asset_name="AgentStart-Setup-2.5.1.0.exe"))
        check("different asset name refused", not vr(FX_SIG512, asset_name="AgentStart-Setup-2.5.2.exe"))
        check("different size (one byte longer) refused", not vr(FX_SIG512, size=FX_INPUTS["size"] + 1))
        check("different sha256 (swapped installer) refused", not vr(FX_SIG512, sha256="3f" * 32))
        check("different repo (signature for another project) refused", not vr(FX_SIG512, repo="example-owner/other"))
        lines = FX_SIG512.splitlines()
        last = lines[-2]
        lines[-2] = last[:10] + ("A" if last[10] != "A" else "B") + last[11:]
        check("tampered armor (signature bytes) refused", not vr("\n".join(lines) + "\n"))
        lines = FX_SIG512.splitlines()
        lines[1] = lines[1][:40] + ("A" if lines[1][40] != "A" else "B") + lines[1][41:]
        check("tampered armor (embedded key) refused", not vr("\n".join(lines) + "\n"))
        check("embedded key swapped for another trusted key refused",
              not vr(armor(sshsig(key=OTHER_PUB)), keys=(OTHER_PUB, FX_PUB)))
        check("oversized input refused", not vr(FX_SIG512 + "\n" * update_sig.MAX_SIG_BYTES))
        check("trailing garbage after the armor refused", not vr(FX_SIG512 + "x"))
        check("trailing bytes inside the blob refused", not vr(armor(sshsig(trailing=b"\x00"))))
        check("non-ed25519 key type (real ecdsa signature) refused", not vr(FX_SIG_ECDSA))
        check("hash algorithm sha1 refused", not vr(armor(sshsig(hash_alg=b"sha1"))))
        check("empty signature text refused", not vr(""))
        recs.clear()
        check("None / int / list inputs refused without raising",
              not vr(None) and not vr(42) and not vr([FX_SIG512]) and not vr(FX_SIG512, keys="nope")
              and not vr(FX_SIG512, size="123456") and not vr(FX_SIG512, version=None))
        body = FX_SIG512.splitlines()[1:-1]
        check("refusal logs never contain the signature body",
              not any(any(b_ in r.getMessage() for b_ in body) for r in recs) and len(recs) > 0)
        check("refusal logs carry no newline", not any("\n" in r.getMessage() for r in recs))
    finally:
        lg.removeHandler(h)
        lg.setLevel(old_level)


def test_keys() -> None:
    section("keys: enabled(), PUBLIC_KEYS, public_key_from_openssh()")
    check("enabled(()) is False", update_sig.enabled(()) is False)
    check("enabled((fixture key,)) is True", update_sig.enabled((FX_PUB,)) is True)
    check("enabled with a 31-byte key is False", update_sig.enabled((FX_PUB[:31],)) is False)
    check("enabled with a non-point 32-byte key is False", update_sig.enabled((b"\xff" * 32,)) is False)
    check("enabled with a str key is False", update_sig.enabled(("x" * 32,)) is False)   # type: ignore[arg-type]
    check("enabled with a non-tuple is False", update_sig.enabled("garbage") is False)    # type: ignore[arg-type]
    pk = update_sig.PUBLIC_KEYS
    check("shipped PUBLIC_KEYS is a tuple of 32-byte bytes (empty allowed)",
          isinstance(pk, tuple) and all(isinstance(k, bytes) and len(k) == 32 for k in pk), repr(pk))
    check("every shipped key decodes as an Ed25519 point", update_sig.enabled(tuple(pk)) == bool(pk))
    check("enabled() follows the shipped PUBLIC_KEYS", update_sig.enabled() == bool(pk))
    check("shipped keys are distinct", len(set(pk)) == len(pk))
    check("constants", update_sig.NAMESPACE == "agent-start-release" and update_sig.MAX_SIG_BYTES == 4096
          and update_sig.SIG_SUFFIX == ".sig")
    check("fixture public key line -> 32 bytes", len(FX_PUB) == 32)
    b64 = FX_PUB_LINE.split()[1]
    check("public key line without a comment", update_sig.public_key_from_openssh(f"ssh-ed25519 {b64}") == FX_PUB)
    check("public key line with a multi-word comment and spaces",
          update_sig.public_key_from_openssh(f"  ssh-ed25519 {b64} a b c \n") == FX_PUB)
    blob = base64.b64decode(b64)
    bad = {
        "ssh-rsa type": f"ssh-rsa {b64}",
        "bad base64": "ssh-ed25519 AAAA*AAA",
        "truncated blob": "ssh-ed25519 " + base64.b64encode(blob[:-1]).decode(),
        "trailing bytes in blob": "ssh-ed25519 " + base64.b64encode(blob + b"\x00").decode(),
        "inner type differs": "ssh-ed25519 " + base64.b64encode(s(b"ssh-rsa") + s(FX_PUB)).decode(),
        "non-point key": "ssh-ed25519 " + base64.b64encode(s(b"ssh-ed25519") + s(b"\xff" * 32)).decode(),
        "two lines": f"{FX_PUB_LINE}\n{FX_PUB_LINE}",
        "options prefix": f"restrict {FX_PUB_LINE}",
        "empty": "",
        "only the type": "ssh-ed25519",
    }
    for label, line in bad.items():
        check(f"public key refused: {label}", raises(update_sig.public_key_from_openssh, line))
    check("public key refused: None", raises(update_sig.public_key_from_openssh, None))


# ----------------------------------------------------------------------------------------- ssh-keygen (runtime)

def keygens() -> list[str]:
    """Distinct ssh-keygen binaries to cross-check (Windows OpenSSH first). Empty when disabled or missing."""
    if os.environ.get("AGENTSTART_TEST_NO_SSHKEYGEN") == "1":
        return []
    out: list[str] = []
    for c in (sign_release.find_ssh_keygen(), shutil.which("ssh-keygen")):
        if c and os.path.normcase(os.path.realpath(c)) not in {os.path.normcase(os.path.realpath(x)) for x in out}:
            out.append(c)
    return out


def run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60, **kw)


def new_key(kg: str, folder: Path, kind: str = "ed25519") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    k = folder / f"throwaway_{kind}"
    extra = ["-b", "256"] if kind == "ecdsa" else []
    r = run([kg, "-q", "-t", kind, *extra, "-N", "", "-C", "throwaway-test", "-f", str(k)])
    if r.returncode != 0:
        raise RuntimeError(f"ssh-keygen keygen failed: {r.stderr.strip()}")
    return k


def test_real_ssh_keygen() -> None:
    section("real ssh-keygen -Y sign (throwaway keys made now)")
    kgs = keygens()
    if not kgs:
        skip("ssh-keygen signing checks", "no ssh-keygen found or AGENTSTART_TEST_NO_SSHKEYGEN=1; "
             "the embedded fixtures above cover the full verify path")
        return
    msg = update_sig.manifest(**FX_INPUTS)
    for n, kg in enumerate(kgs):
        tag = f"[{n}:{Path(kg).parent.name}]"
        d = TMP / f"kg{n}"
        k = new_key(kg, d)
        pub = update_sig.public_key_from_openssh((d / (k.name + ".pub")).read_text())
        mf = d / "manifest.txt"
        mf.write_bytes(msg)
        for label, extra in (("sha512", []), ("sha256", ["-O", "hashalg=sha256"])):
            r = run([kg, "-Y", "sign", "-f", str(k), "-n", update_sig.NAMESPACE, *extra, str(mf)])
            sig_path = Path(str(mf) + ".sig")
            if not check(f"{tag} ssh-keygen -Y sign ({label}) succeeded", r.returncode == 0 and sig_path.is_file(),
                         r.stderr.strip()):
                continue
            text = sig_path.read_text()
            sig_path.unlink()
            check(f"{tag} {label}: verify_release accepts", vr(text, keys=(pub,)))
            check(f"{tag} {label}: other version refused",
                  not vr(text, keys=(pub,), version="2.5.2", asset_name="AgentStart-Setup-2.5.2.exe"))
            check(f"{tag} {label}: fixture key does not accept it", not vr(text, keys=(FX_PUB,)))
        r = run([kg, "-Y", "sign", "-f", str(k), "-n", "file", str(mf)])
        if check(f"{tag} sign with namespace 'file' succeeded", r.returncode == 0, r.stderr.strip()):
            p = Path(str(mf) + ".sig")
            check(f"{tag} namespace 'file' refused", not vr(p.read_text(), keys=(pub,)))
            p.unlink()
        ek = new_key(kg, d, "ecdsa")
        r = run([kg, "-Y", "sign", "-f", str(ek), "-n", update_sig.NAMESPACE, str(mf)])
        if check(f"{tag} ecdsa sign succeeded", r.returncode == 0, r.stderr.strip()):
            p = Path(str(mf) + ".sig")
            check(f"{tag} ecdsa signature refused (key type)", not vr(p.read_text(), keys=(pub,)))
            p.unlink()
        r = run([kg, "-l", "-E", "sha256", "-f", str(d / (k.name + ".pub"))])
        check(f"{tag} fingerprint matches ssh-keygen -l", r.returncode == 0
              and sign_release.fingerprint(pub) in r.stdout, r.stdout.strip())
        # the fixture also verifies with ssh-keygen itself (cross-implementation agreement)
        allowed = d / "allowed_signers"
        allowed.write_text(f'release@example namespaces="{update_sig.NAMESPACE}" {FX_PUB_LINE}\n')
        sigf = d / "fixture.sig"
        sigf.write_text(FX_SIG512)
        with open(mf, "rb") as fin:
            r = subprocess.run([kg, "-Y", "verify", "-f", str(allowed), "-I", "release@example", "-n",
                                update_sig.NAMESPACE, "-s", str(sigf)], stdin=fin, capture_output=True, text=True,
                               timeout=60)
        check(f"{tag} ssh-keygen -Y verify accepts the embedded fixture", r.returncode == 0, r.stderr.strip())


# ----------------------------------------------------------------------------------------- sign_release.py

def test_sign_release_helpers() -> None:
    section("tools/sign_release.py helpers")
    check("version from file name", sign_release.version_from_name("x/y/AgentStart-Setup-1.13.0.exe") == "1.13.0")
    check("version from a Windows path", sign_release.version_from_name("C:\\d\\AgentStart-Setup-2.exe") == "2")
    for bad in ("AgentStart-Setup-1.2.exe.sig", "agentstart-setup-1.2.exe", "AgentStart-Setup-01.2.exe",
                "AgentStart-Setup-1.2.3.4.5.exe", "AgentStart-Setup-v1.2.exe", "Setup.exe", "AgentStart-Setup-.exe"):
        try:
            sign_release.version_from_name(bad)
            ok = False
        except sign_release.Refused:
            ok = True
        check(f"version from name refused: {bad}", ok)
    check("repo folder itself is inside", sign_release.inside_folder(ROOT, ROOT))
    check("file in the repo is inside", sign_release.inside_folder(ROOT / "keys" / "release_primary", ROOT))
    check("upper-cased repo path is inside (normcase)",
          sign_release.inside_folder(str(ROOT / "k").upper(), ROOT) == (os.path.normcase("A") == "a"))
    check("'..' out of the repo is outside", not sign_release.inside_folder(ROOT / ".." / "k", ROOT))
    check("sibling folder with the repo name as prefix is outside",
          not sign_release.inside_folder(str(ROOT) + "-keys/k", ROOT))
    check("temp folder is outside", not sign_release.inside_folder(TMP / "k", ROOT))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = sign_release.sign_installer(str(TMP / "AgentStart-Setup-1.0.exe"), str(ROOT / "release_key_never_here"),
                                         ssh_keygen="ssh-keygen", trusted_keys=())
    check("signing refuses a key inside the repository (exit 2)", rc == 2, out.getvalue())
    check("... and says why", "inside the repository" in out.getvalue() and "never" in out.getvalue(), out.getvalue())
    check("... and creates nothing in the repo", not (ROOT / "release_key_never_here").exists()
          and not (ROOT / "release_key_never_here.pub").exists())
    lit = sign_release.key_literal(FX_PUB)
    lines = lit.splitlines()
    check("key literal: comment + two literal lines, each <= 120 chars",
          len(lines) == 3 and lines[0].strip().startswith("# SHA256:") and all(len(x) <= 120 for x in lines), lit)
    check("key literal evaluates to the 32 key bytes",
          ast.literal_eval("(" + "\n".join(lines[1:]).rstrip(",") + ")") == FX_PUB)
    tup = ast.literal_eval("(\n" + lit + "\n)")
    check("pasted into a tuple it gives (key,)", tup == (FX_PUB,))
    pubf = TMP / "throwaway_fixture.pub"
    pubf.write_text(FX_PUB_LINE + "\n")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = sign_release.main(["--show-key", str(pubf)])
    check("--show-key exit 0 and prints the literal and fingerprint",
          rc == 0 and lit in out.getvalue() and sign_release.fingerprint(FX_PUB) in out.getvalue(), out.getvalue())
    check("fingerprint format", sign_release.fingerprint(FX_PUB).startswith("SHA256:")
          and len(sign_release.fingerprint(FX_PUB)) == 7 + 43)
    bad_pub = TMP / "bad.pub"
    bad_pub.write_text("ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ== x\n")
    with contextlib.redirect_stdout(io.StringIO()):
        rc = sign_release.main(["--show-key", str(bad_pub)])
    check("--show-key refuses a non-ed25519 key (exit 2)", rc == 2)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = sign_release.main(["--new-key-help"])
    txt = out.getvalue()
    check("--new-key-help: two ed25519 -a 100 commands, primary and backup",
          rc == 0 and txt.count("ssh-keygen -t ed25519 -a 100") == 2 and "backup" in txt and "primary" in txt, txt)
    check("--new-key-help names no user folder", "Users\\" not in txt and "/Users/" not in txt)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        rc = sign_release.main([])
    check("no arguments -> exit 2", rc == 2)
    check("--ssh-keygen with a missing path -> None", sign_release.find_ssh_keygen(str(TMP / "nope.exe")) is None)
    check("sign_release REPO is updater.REPO", sign_release.REPO == updater.REPO)


def test_sign_release_e2e() -> None:
    section("tools/sign_release.py end to end (throwaway key, fake installer)")
    kg = sign_release.find_ssh_keygen() if os.environ.get("AGENTSTART_TEST_NO_SSHKEYGEN") != "1" else None
    if kg is None:
        skip("end-to-end signing", "no ssh-keygen found or AGENTSTART_TEST_NO_SSHKEYGEN=1")
        return
    d = TMP / "e2e"
    k = new_key(kg, d / "keys")
    pub = update_sig.public_key_from_openssh((d / "keys" / (k.name + ".pub")).read_text())
    inst = d / "dist" / "AgentStart-Setup-0.0.7.exe"
    inst.parent.mkdir(parents=True)
    inst.write_bytes(b"MZ not a real installer, just test bytes\n" * 50)
    before = {p.name for p in Path(tempfile.gettempdir()).glob("agentstart-sign-*")}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = sign_release.sign_installer(str(inst), str(k), ssh_keygen=kg, trusted_keys=(pub,))
    text = out.getvalue()
    sig = Path(str(inst) + ".sig")
    check("signs with a trusted throwaway key: exit 0", rc == 0, text)
    check(".sig written next to the installer", sig.is_file())
    data = inst.read_bytes()
    fields = dict(repo=updater.REPO, version="0.0.7", asset_name=inst.name, size=len(data),
                  sha256=hashlib.sha256(data).hexdigest())
    check("the .sig verifies with verify_release", update_sig.verify_release(sig.read_bytes(), keys=(pub,), **fields))
    check("printed the manifest it signed", update_sig.manifest(**fields).decode() in text, text)
    check("printed the .sig path", str(sig) in text, text)
    after = {p.name for p in Path(tempfile.gettempdir()).glob("agentstart-sign-*")}
    check("temp manifest folder removed", after == before, str(after - before))
    check("no stray files next to the installer", sorted(p.name for p in inst.parent.iterdir())
          == sorted([inst.name, sig.name]))
    with contextlib.redirect_stdout(io.StringIO()) as o:
        rc = sign_release.sign_installer(str(inst), str(k), ssh_keygen=kg, trusted_keys=(pub,))
    check("existing .sig without --force refused (exit 2)", rc == 2, o.getvalue())
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = sign_release.sign_installer(str(inst), str(k), ssh_keygen=kg, trusted_keys=(), force=True)
    check("key not in the trusted set: exit 3 and a loud warning",
          rc == 3 and "NOT in update_sig.PUBLIC_KEYS" in out.getvalue(), out.getvalue())
    check("... the .sig still verifies with its own key",
          update_sig.verify_release(sig.read_bytes(), keys=(pub,), **fields))
    inst.write_bytes(data + b"x")
    data2 = inst.read_bytes()
    check("installer changed after signing -> refused",
          not update_sig.verify_release(sig.read_bytes(), keys=(pub,), **{
              **fields, "size": len(data2), "sha256": hashlib.sha256(data2).hexdigest()}))
    renamed = d / "dist" / "AgentStart-Setup-9.9.9.exe"
    shutil.copyfile(sig, str(renamed) + ".sig")
    check("same .sig re-used for a renamed 9.9.9 installer -> refused",
          not update_sig.verify_release(Path(str(renamed) + ".sig").read_bytes(), keys=(pub,), **{
              **fields, "version": "9.9.9", "asset_name": renamed.name}))
    bad_name = d / "dist" / "Setup.exe"
    bad_name.write_bytes(b"x")
    with contextlib.redirect_stdout(io.StringIO()):
        rc = sign_release.sign_installer(str(bad_name), str(k), ssh_keygen=kg, trusted_keys=(pub,))
    check("bad installer name refused (exit 2)", rc == 2)
    with contextlib.redirect_stdout(io.StringIO()):
        rc = sign_release.sign_installer(str(inst), str(d / "keys" / "nope"), ssh_keygen=kg, force=True)
    check("missing key refused (exit 2)", rc == 2)


def main() -> int:
    print(f"temp folder: {TMP}")
    try:
        for fn in (test_tmp_safety, test_ed25519, test_manifest, test_parse_sshsig, test_signed_blob,
                   test_verify_release, test_keys, test_real_ssh_keygen, test_sign_release_helpers,
                   test_sign_release_e2e):
            if fn is not test_tmp_safety and any(not ok for _, ok, _ in RESULTS[:2]):
                check("aborted: unsafe temp folder", False)
                break
            try:
                fn()
            except Exception:
                traceback.print_exc()
                check(f"{fn.__name__} raised", False, "exception")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
        check("temp dir removed", not TMP.exists(), str(TMP))
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\nRESULT: {passed} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAILED: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
