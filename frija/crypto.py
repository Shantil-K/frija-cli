"""FUS nonce/signature crypto, logic check and firmware decryption.

Ported from Frija.Backend.Crypto.AesDeEncode, Frija.Backend.Helper.Utility
and Frija.Utils.FirmwareDecryptor.
"""
import base64
import hashlib

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY1 = "jqw2zntssasudk3zuwwx4tnqz5h3b6i4"
KEY2 = "ifzg4ovyl3cyl3a5"
IV = "jqw2zntssasudk3z"


def _aes_cbc_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    raw = d.update(data) + d.finalize()
    u = padding.PKCS7(128).unpadder()
    return u.update(raw) + u.finalize()


def _aes_cbc_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    p = padding.PKCS7(128).padder()
    data = p.update(data) + p.finalize()
    e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return e.update(data) + e.finalize()


def decrypt_nonce(enc_nonce: str) -> str:
    """Decrypt the NONCE header returned by the FUS server."""
    raw = _aes_cbc_decrypt(base64.b64decode(enc_nonce), KEY1.encode(), IV.encode())
    return raw.decode("utf-8")


def auth_signature(nonce: str) -> str:
    """Signature sent in the Authorization header (newauth="1" scheme)."""
    fkey = "".join(KEY1[ord(c) % 16] for c in nonce[:16]) + KEY2
    key = fkey.encode()
    return base64.b64encode(_aes_cbc_encrypt(nonce.encode("utf-8"), key, key[:16])).decode()


def logic_check(inp: str, nonce: str) -> str:
    """Pick characters of `inp` indexed by the low nibble of each nonce char."""
    if not inp:
        return ""
    off = len(inp) - 25 if inp.endswith((".zip.enc2", ".zip.enc4")) else 0
    out = []
    for c in nonce:
        i = (ord(c) & 0xF) + off
        if len(inp) <= i:
            return ""
        out.append(inp[i])
    return "".join(out)


def key_enc2(region: str, model: str, version: str) -> bytes:
    return hashlib.md5(f"{region}:{model}:{version}".encode("ascii")).digest()


def key_enc4(version: str, logic_value: str) -> bytes:
    return hashlib.md5(logic_check(version, logic_value).encode("ascii")).digest()


def decrypt_file(src: str, dst: str, key: bytes, progress=None, cancel=None, chunk=4 * 1024 * 1024):
    """AES-128-ECB + PKCS7 decrypt `src` into `dst`. Raises ValueError on a wrong key."""
    import os

    total = os.path.getsize(src)
    if total % 16:
        raise ValueError("encrypted file size is not a multiple of 16 (incomplete download?)")
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    unpad = padding.PKCS7(128).unpadder()
    done = 0
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            buf = fi.read(chunk)
            if not buf:
                break
            fo.write(unpad.update(dec.update(buf)))
            done += len(buf)
            if progress:
                progress(done, total)
            if cancel and cancel():
                from .core import Cancelled
                raise Cancelled()
        fo.write(unpad.update(dec.finalize()))
        fo.write(unpad.finalize())  # raises ValueError if padding is bad -> wrong key
