"""Read model/CSC/serial/IMEI from a USB-connected Samsung phone.

Replacement for Frija.ModemInterface.ModemController. The phone exposes a
modem interface (USB vendor 04e8) that answers the AT+DEVCONINFO command.

Backends, picked at call time so importing this module never fails:
  * Linux   : /dev/ttyACM* found via sysfs, talked to with termios (stdlib only)
  * Windows : "SAMSUNG Mobile USB Modem" COM port found via the registry (stdlib
              winreg); talked to with pyserial
  * other   : pyserial for both detection and I/O, if it is installed
"""
import os
import re
import sys
import time
from dataclasses import dataclass

SAMSUNG_VID = "04e8"
AT_COMMAND = b"AT+DEVCONINFO\r\n"


@dataclass
class DeviceInfo:
    port: str
    model: str
    csc: str
    serial: str
    imei: str
    firmware: str


def _platform():
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "win32":
        return "windows"
    return "other"


def _need_pyserial():
    try:
        import serial  # noqa: F401
        return serial
    except ImportError:
        raise RuntimeError("talking to the phone on this system needs pyserial: pip install pyserial") from None


# ---------------------------------------------------------------- detection

def _find_ports_linux():
    import glob

    def usb_attr(tty, name):
        # /sys/class/tty/ttyACM0/device -> USB interface; its parent is the USB device
        dev = os.path.realpath(f"/sys/class/tty/{tty}/device")
        for d in (dev, os.path.dirname(dev)):
            p = os.path.join(d, name)
            if os.path.exists(p):
                with open(p) as f:
                    return f.read().strip()
        return ""

    ports = []
    for path in sorted(glob.glob("/sys/class/tty/ttyACM*")):
        tty = os.path.basename(path)
        if usb_attr(tty, "idVendor").lower() == SAMSUNG_VID:
            ports.append(f"/dev/{tty}")
    return ports


def _find_ports_windows():
    """Samsung modem COM ports that are currently present.

    Same idea as the original WMI query for "SAMSUNG Mobile USB Modem", done with
    the registry: USB\\VID_04E8* devices give PortName + FriendlyName, and
    HARDWARE\\DEVICEMAP\\SERIALCOMM lists only ports that exist right now.
    """
    import winreg

    def values(key):
        i = 0
        while True:
            try:
                yield winreg.EnumValue(key, i)
            except OSError:
                return
            i += 1

    def subkeys(key):
        i = 0
        while True:
            try:
                yield winreg.EnumKey(key, i)
            except OSError:
                return
            i += 1

    def read(key, path, name):
        try:
            with winreg.OpenKey(key, path) as k:
                return str(winreg.QueryValueEx(k, name)[0])
        except OSError:
            return ""

    HKLM = winreg.HKEY_LOCAL_MACHINE
    try:
        with winreg.OpenKey(HKLM, r"HARDWARE\DEVICEMAP\SERIALCOMM") as k:
            active = {str(v[1]).upper() for v in values(k)}
    except OSError:
        return []

    found = []  # (is_modem, port)
    try:
        usb = winreg.OpenKey(HKLM, r"SYSTEM\CurrentControlSet\Enum\USB")
    except OSError:
        return []
    with usb:
        for dev in subkeys(usb):
            if not dev.upper().startswith("VID_" + SAMSUNG_VID.upper()):
                continue
            with winreg.OpenKey(usb, dev) as dk:
                for inst in subkeys(dk):
                    port = read(dk, inst + r"\Device Parameters", "PortName").upper()
                    if port and port in active:
                        name = read(dk, inst, "FriendlyName")
                        found.append(("modem" in name.lower(), port))
    modems = [p for is_modem, p in found if is_modem]
    ports = modems or [p for _, p in found]
    return sorted(set(ports), key=lambda p: int(re.sub(r"\D", "", p) or 0))


def _find_ports_pyserial():
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return sorted(p.device for p in list_ports.comports() if p.vid == int(SAMSUNG_VID, 16))


def find_ports() -> list[str]:
    """Return the Samsung modem ports that are connected now (may be empty)."""
    plat = _platform()
    if plat == "linux":
        return _find_ports_linux()
    if plat == "windows":
        try:
            return _find_ports_windows()
        except Exception:  # noqa: BLE001 - registry layout surprises; fall back
            return _find_ports_pyserial()
    return _find_ports_pyserial()


# ---------------------------------------------------------------- talking

def _query_termios(port: str, timeout: float) -> str:
    import select
    import termios

    try:
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except PermissionError:
        raise RuntimeError(f"permission denied on {port}: add yourself to the 'dialout' group and log in again")
    try:
        cc = termios.tcgetattr(fd)[6]
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        cflag = termios.CS8 | termios.CREAD | termios.CLOCAL  # 9600 8N1, DTR/RTS raised on open
        termios.tcsetattr(fd, termios.TCSANOW, [0, 0, cflag, 0, termios.B9600, termios.B9600, cc])
        termios.tcflush(fd, termios.TCIOFLUSH)
        os.write(fd, AT_COMMAND)
        buf = b""
        end = time.time() + timeout
        while time.time() < end and not _complete(buf):
            if select.select([fd], [], [], 0.2)[0]:
                buf += os.read(fd, 4096)
        return buf.decode("ascii", "replace")
    finally:
        os.close(fd)


def _query_pyserial(port: str, timeout: float) -> str:
    serial = _need_pyserial()
    try:
        s = serial.Serial(port, 9600, bytesize=8, parity="N", stopbits=1, timeout=0.2, write_timeout=2)
    except serial.SerialException as e:
        raise RuntimeError(f"could not open {port}: {e} (is another program, e.g. Smart Switch, using it?)")
    with s:
        s.dtr = True
        s.rts = True
        s.reset_input_buffer()
        s.write(AT_COMMAND)
        buf = b""
        end = time.time() + timeout
        while time.time() < end and not _complete(buf):
            buf += s.read(4096)
        return buf.decode("ascii", "replace")


def _complete(buf: bytes) -> bool:
    return b"\r\nOK\r\n" in buf or b"ERROR" in buf


def read_device_info(port: str, timeout: float = 3.0) -> DeviceInfo:
    if _platform() == "linux":
        text = _query_termios(port, timeout)
        hint = ("If ModemManager is running it may be holding the port; "
                "try: sudo systemctl stop ModemManager")
    else:
        text = _query_pyserial(port, timeout)
        hint = "Close Samsung Smart Switch or other tools that may be using the port."

    if "\r\nERROR\r\n" in text:
        raise RuntimeError("device returned ERROR instead of AT+DEVCONINFO info")
    if not all(k in text for k in ("MN(", "PRD(", "SN(")):
        raise RuntimeError("device did not return model/CSC/serial. " + hint)

    def grab(key):
        m = re.search(key + r"\((.+?)\)", text)
        return m.group(1) if m else ""

    return DeviceInfo(port, grab("MN"), grab("PRD"), grab("SN"), grab("IMEI"), grab("VER"))


def wait_for_device(poll: float = 1.0) -> str:
    """Block until a Samsung modem port appears; return it."""
    while True:
        ports = find_ports()
        if ports:
            time.sleep(1.5)  # let the phone finish enumerating
            return ports[0]
        time.sleep(poll)


def no_device_hint() -> str:
    plat = _platform()
    if plat == "linux":
        return "No Samsung modem port found (/dev/ttyACM*, USB vendor 04e8). Is the phone connected and unlocked?"
    if plat == "windows":
        return ("No 'SAMSUNG Mobile USB Modem' COM port found. Is the phone connected and unlocked, "
                "and are the Samsung USB drivers installed?")
    return "No Samsung modem port found (needs pyserial on this system). Is the phone connected and unlocked?"
