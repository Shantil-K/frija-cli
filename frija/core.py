"""Shared workflow used by both the CLI and the GUI.

Functions raise FrijaError instead of exiting, report progress through
`progress(done, total)` callbacks, and stop early when `cancel()` returns True.
"""
import logging
import os
import zlib
from dataclasses import dataclass

from . import crypto, fota
from .fus import FUSClient, FUSError, Firmware

log = logging.getLogger("frija")


class FrijaError(Exception):
    pass


class Cancelled(Exception):
    pass


@dataclass
class Query:
    model: str
    csc: str
    imei_sn: str
    pda: str
    csc_ver: str
    phone: str


def _never():
    return False


def build_query(model, csc, imei_sn="", fw=None) -> Query:
    """Normalise input and fill in the firmware version (from FOTA unless `fw` is given).

    `fw` is either None, a 'PDA/CSC/PHONE' string, or a (pda, csc, phone) tuple.
    """
    model = (model or "").replace(" ", "").upper()
    csc = (csc or "").replace(" ", "").upper()
    imei_sn = (imei_sn or "").replace(" ", "")
    if not model:
        raise FrijaError("Please specify the model")
    if not csc:
        raise FrijaError("Please specify the CSC / region")
    if csc == "ATT":
        raise FrijaError("ATT CSC code is not supported")
    if model.endswith("FD"):
        model = model[:-1]

    if fw:
        parts = fw.split("/") if isinstance(fw, str) else list(fw)
        parts = [p.strip().upper() for p in parts]
        if len(parts) < 3 or not all(parts[:3]):
            raise FrijaError("Firmware version must have PDA, CSC and Phone parts")
        pda, csc_ver, phone = parts[:3]
        imei_sn = imei_sn or "dummy"
    else:
        if not imei_sn:
            raise FrijaError("Please specify the IMEI or serial number")
        log.info("Looking up latest firmware on FOTA server for %s/%s", model, csc)
        info = fota.info_lookup(model, csc)
        if not info:
            raise FrijaError("Firmware information for this model/CSC not found on FOTA server")
        parts = info.split("/") + ["", "", ""]
        pda, csc_ver, phone = parts[:3]
        phone = phone or pda
    log.info("PDA: %s  CSC: %s  Phone: %s", pda, csc_ver, phone)
    return Query(model, csc, imei_sn, pda, csc_ver, phone)


def check(client: FUSClient, q: Query, factory=False) -> Firmware:
    log.info("Retrieving firmware from FUS server")
    try:
        fw = client.check(q.model, q.csc, q.imei_sn, q.pda, q.csc_ver, q.phone, binary_nature=factory)
    except FUSError as e:
        raise FrijaError(str(e)) from e
    log.info("Latest firmware: %s (%s MB) %s", fw.version, fw.size_mb, fw.filename)
    return fw


def download(client: FUSClient, fw: Firmware, dest: str, progress=None, cancel=_never, fallback=False):
    """Download (or resume) `fw` into `dest`. Raises Cancelled if stopped."""
    have = os.path.getsize(dest) if os.path.exists(dest) else 0
    if have == fw.size_bytes:
        log.info("File already downloaded")
        return
    if have > fw.size_bytes:
        raise FrijaError(f"{dest} is larger than the firmware; delete it and try again")
    if have:
        log.info("File exists, resuming at %.2f GB", have / 2**30)
    try:
        resp = client.open_download(fw, have, fallback=fallback)
    except FUSError as e:
        raise FrijaError(str(e)) from e
    if resp.status_code == 403 and not fallback:
        resp.close()
        log.warning("HTTP 403 from CDN, retrying with fallback server")
        return download(client, fw, dest, progress, cancel, fallback=True)
    if resp.status_code not in (200, 206):
        resp.close()
        raise FrijaError(f"Error downloading firmware, HTTP {resp.status_code}")
    if have and resp.status_code == 200:
        log.warning("Server ignored resume request; restarting download")
        have = 0
    with resp, open(dest, "ab" if have else "wb") as f:
        done = have
        for chunk in resp.iter_content(1024 * 1024):
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, fw.size_bytes)
            if cancel():
                raise Cancelled()
    if os.path.getsize(dest) != fw.size_bytes:
        raise FrijaError("Download incomplete; start it again to resume")
    log.info("Firmware downloaded from FUS server")


def crc32_file(path, progress=None, cancel=_never):
    crc, done, total = 0, 0, os.path.getsize(path)
    with open(path, "rb") as f:
        while buf := f.read(4 * 1024 * 1024):
            crc = zlib.crc32(buf, crc)
            done += len(buf)
            if progress:
                progress(done, total)
            if cancel():
                raise Cancelled()
    return crc & 0xFFFFFFFF


def verify_crc(fw: Firmware, path, progress=None, cancel=_never):
    if fw.crc is None:
        log.info("Server gave no CRC; skipping verification")
        return
    log.info("Verifying CRC32 of downloaded firmware")
    got = crc32_file(path, progress, cancel)
    if got != fw.crc:
        os.remove(path)
        raise FrijaError(f"CRC does not match (expected {fw.crc}, got {got}). File deleted, please re-download.")
    log.info("CRC matched")


def decrypt_key_for(fw: Firmware, filename: str) -> bytes:
    if filename.endswith(".enc2"):
        return crypto.key_enc2(fw.region, fw.model, fw.version)
    if filename.endswith(".enc4"):
        lv = fw.logic_value_factory if fw.binary_nature == 1 else fw.logic_value_home
        if not lv:
            raise FrijaError("Server did not provide a logic value; cannot derive the .enc4 key")
        return crypto.key_enc4(fw.version, lv)
    raise FrijaError(f"Unknown encrypted file type: {filename}")


def decrypt(src, key, keep_encrypted=False, progress=None, cancel=_never) -> str:
    dst = src.rsplit(".", 1)[0]
    log.info("Decrypting to %s", dst)
    try:
        crypto.decrypt_file(src, dst, key, progress, cancel)
    except Cancelled:
        if os.path.exists(dst):
            os.remove(dst)
        raise
    except ValueError as e:
        if os.path.exists(dst):
            os.remove(dst)
        raise FrijaError(f"Error decrypting file: wrong key ({e})") from e
    log.info("Firmware successfully decrypted: %s", dst)
    if not keep_encrypted:
        os.remove(src)
    return dst
