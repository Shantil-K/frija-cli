"""Command-line front end. The workflow itself lives in core.py (also used by frija-gtk).

Modes, matching the original app:
  * Manual  : --model + --region + --imei   (version looked up on the FOTA server)
  * Legacy  : --model + --region + --fw PDA/CSC/PHONE   (IMEI optional)
  * Auto    : --auto  (model/CSC/serial read from a phone connected over USB)
"""
import argparse
import logging
import os
import sys
import time

import requests

from . import __version__, core, crypto
from .fus import FUSClient, Firmware

log = logging.getLogger("frija")


def die(msg, code=1):
    log.error(msg)
    sys.exit(code)


class Progress:
    def __init__(self, label):
        self.label = label
        self.t0 = time.time()
        self.last = 0.0
        self.start_bytes = None

    def __call__(self, done, total):
        now = time.time()
        if self.start_bytes is None:
            self.start_bytes = done
        if now - self.last < 0.5 and done < total:
            return
        self.last = now
        pct = done * 100 // total if total else 0
        speed = (done - self.start_bytes) / max(now - self.t0, 1e-6) / 1024 / 1024
        sys.stderr.write(f"\r{self.label}: {pct:3d}%  {done/2**30:.2f}/{total/2**30:.2f} GB  {speed:.2f} MB/s ")
        sys.stderr.flush()
        if done >= total:
            sys.stderr.write("\n")


def make_query(args) -> core.Query:
    if args.auto:
        from . import modem  # only needed for USB mode

        port = args.port or (modem.find_ports() or [None])[0]
        if not port:
            log.info("Waiting for a Samsung device on USB (Ctrl+C to cancel)...")
            port = modem.wait_for_device()
        log.info("Reading device info from %s", port)
        dev = modem.read_device_info(port)
        log.info("Device: model=%s csc=%s serial=%s firmware=%s", dev.model, dev.csc, dev.serial, dev.firmware)
        # The original Auto mode sends the serial number.
        return core.build_query(dev.model, dev.csc, args.imei or dev.serial, args.fw)
    if not args.model or not args.region:
        die("--model and --region are required (or use --auto)")
    return core.build_query(args.model, args.region, args.imei, args.fw)


def print_fw(fw: Firmware):
    print(f"Model:        {fw.model} ({fw.display_name})")
    print(f"Region:       {fw.region}")
    print(f"Version:      {fw.version}")
    print(f"OS:           {fw.os}")
    print(f"Last updated: {fw.last_modified}")
    print(f"Filename:     {fw.filename}")
    print(f"Size:         {fw.size_mb} MB")
    print(f"CRC32:        {fw.crc}")
    print(f"Type:         {'Factory' if fw.binary_nature == 1 else 'Home'}")


def do_check(args, client) -> Firmware:
    fw = core.check(client, make_query(args), factory=args.factory)
    print_fw(fw)
    return fw


def cmd_check(args):
    do_check(args, FUSClient())


def cmd_download(args):
    client = FUSClient()
    fw = do_check(args, client)
    os.makedirs(args.output, exist_ok=True)
    dest = os.path.join(args.output, fw.filename)
    if args.overwrite and os.path.exists(dest):
        os.remove(dest)
    core.download(client, fw, dest, Progress("Downloading"))
    if not args.no_crc:
        core.verify_crc(fw, dest, Progress("CRC32"))
    if args.no_decrypt:
        print(dest)
        return
    print(core.decrypt(dest, core.decrypt_key_for(fw, dest), args.keep_encrypted, Progress("Decrypting")))


def cmd_decrypt(args):
    src = args.input
    if not os.path.exists(src):
        die(f"{src} not found")
    if args.key:
        key = bytes.fromhex(args.key)
    elif src.endswith(".enc2") and args.model and args.region and args.version:
        key = crypto.key_enc2(args.region.upper(), args.model.upper(), args.version)
    elif src.endswith(".enc4") and args.version and args.logic_value:
        key = crypto.key_enc4(args.version, args.logic_value)
    else:
        # Ask FUS again to obtain version + logic value.
        fw = do_check(args, FUSClient())
        if os.path.basename(src) != fw.filename:
            log.warning("File name differs from the latest firmware (%s); key may be wrong", fw.filename)
        key = core.decrypt_key_for(fw, src)
    print(core.decrypt(src, key, keep_encrypted=True, progress=Progress("Decrypting")))


def cmd_detect(args):
    from . import modem  # only needed for USB mode

    ports = modem.find_ports()
    if not ports and args.wait:
        log.info("Waiting for a Samsung device on USB (Ctrl+C to cancel)...")
        ports = [modem.wait_for_device()]
    if not ports:
        die(modem.no_device_hint())
    for p in ports:
        try:
            d = modem.read_device_info(p)
            print(f"{p}: model={d.model} csc={d.csc} serial={d.serial} imei={d.imei} firmware={d.firmware}")
        except RuntimeError as e:
            print(f"{p}: {e}")


def add_device_args(p):
    g = p.add_argument_group("device")
    g.add_argument("-m", "--model", help="model, e.g. SM-S928B")
    g.add_argument("-r", "--region", help="CSC / region, e.g. EUX, XAA, BTU")
    g.add_argument("-i", "--imei", help="IMEI or serial number of the device")
    g.add_argument("--fw", metavar="PDA/CSC/PHONE", help="legacy mode: firmware version to query from")
    g.add_argument("--auto", action="store_true", help="read model/CSC/serial from a phone connected over USB")
    g.add_argument("--port", help="serial port for --auto, e.g. /dev/ttyACM0 or COM5 (default: auto-detect)")
    g.add_argument("--factory", action="store_true", help="request the factory (BINARY_NATURE=1) firmware")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="frija", description="Samsung firmware downloader (Linux port of Frija)")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    ap.add_argument("--version", action="version", version=f"frija {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="show the latest firmware for a device")
    add_device_args(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("download", help="download (resumable), verify and decrypt firmware")
    add_device_args(p)
    p.add_argument("-o", "--output", default=".", help="output directory (default: current)")
    p.add_argument("--overwrite", action="store_true", help="discard a partial file instead of resuming")
    p.add_argument("--no-crc", action="store_true", help="skip CRC32 verification")
    p.add_argument("--no-decrypt", action="store_true", help="keep the .enc2/.enc4 file, don't decrypt")
    p.add_argument("--keep-encrypted", action="store_true", help="don't delete the encrypted file after decrypting")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("decrypt", help="decrypt an already downloaded .enc2/.enc4 file")
    p.add_argument("input", help="encrypted firmware file")
    add_device_args(p)
    p.add_argument("--version", dest="version", help="full firmware version (PDA/CSC/PHONE/PDA)")
    p.add_argument("--logic-value", help=".enc4 logic value (LOGIC_VALUE_HOME/FACTORY)")
    p.add_argument("--key", help="raw AES key as hex")
    p.set_defaults(func=cmd_decrypt)

    p = sub.add_parser("detect", help="read info from a phone connected over USB")
    p.add_argument("--wait", action="store_true", help="wait until a phone is plugged in")
    p.set_defaults(func=cmd_detect)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")
    if not args.verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
    try:
        args.func(args)
    except (core.FrijaError, RuntimeError) as e:
        die(str(e))
    except requests.RequestException as e:
        die(f"Network error: {e}")
    except (KeyboardInterrupt, core.Cancelled):
        sys.stderr.write("\nInterrupted. Run the same download command again to resume.\n")
        sys.exit(130)


if __name__ == "__main__":
    main()
