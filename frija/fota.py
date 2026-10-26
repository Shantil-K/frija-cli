"""Latest-version lookup on Samsung's public FOTA server (port of FotaFirmware)."""
import logging
import xml.etree.ElementTree as ET

import requests

log = logging.getLogger("frija.fota")

VZW_SEED = "https://raw.githubusercontent.com/wssyncmldm/vzw-seed/master/vzw_seed.xml"

# The FOTA CDN rejects python-requests/urllib3/curl user agents (HTTP 403).
# The original .NET WebClient sends none; urllib3 would add its own, so send
# the same agent Frija uses for FUS instead.
_NO_UA = {"User-Agent": "Kies2.0_FUS"}


def info_lookup(model: str, csc: str, latest: bool = True) -> str:
    """Return 'PDA/CSC/PHONE[/...]' for model+csc, or '' if not found."""
    try:
        if csc == "VZW" and model[-1:] in ("U", "V"):
            root = ET.fromstring(requests.get(VZW_SEED, headers=_NO_UA, timeout=30).text)
            el = root.find(model)
            if el is None or not (el.text or "").strip():
                log.error("Firmware info for %s/%s not found in VZW seed list", model, csc)
                return ""
            return el.text.strip().upper()

        url = f"http://fota-cloud-dn.ospserver.net/firmware/{csc}/{model}/version.xml"
        r = requests.get(url, headers=_NO_UA, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        path = "./firmware/version/latest" if latest else "./firmware/version/upgrade/value"
        return (root.findtext(path) or "").strip().upper()
    except (requests.RequestException, ET.ParseError) as e:
        log.debug("FOTA lookup failed: %s", e)
        return ""
