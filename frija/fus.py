"""Samsung FUS (Firmware Update Server) client.

Port of Frija.Backend.Firmware.FUS / FUSRequest / FUSHelper.
"""
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests

from . import crypto

log = logging.getLogger("frija.fus")

FUS_HOST = "https://neofussvr.sslcs.cdngc.net"
CLOUD_HOST = "https://cloud-neofussvr.sslcs.cdngc.net"
CLOUD_FALLBACK_HOST = "https://cloud-neofussvr.samsungmobile.com"

# Extra fields Frija adds for the EUX / EUY regions.
_EU_EXTRA = {
    "EUX": [("DEVICE_AID_CODE", "EUX"), ("DEVICE_CC_CODE", "DE"), ("MCC_NUM", "262"), ("MNC_NUM", "01")],
    "EUY": [("DEVICE_AID_CODE", "EUY"), ("DEVICE_CC_CODE", "RS"), ("MCC_NUM", "220"), ("MNC_NUM", "01")],
}


class FUSError(Exception):
    pass


@dataclass
class Firmware:
    model: str = ""
    display_name: str = ""
    version: str = ""
    display_version: str = ""
    os: str = ""
    last_modified: str = ""
    filename: str = ""
    path: str = ""
    size_bytes: int = 0
    crc: int | None = None
    model_type: str = ""
    region: str = ""
    binary_nature: int = 0
    logic_value_factory: str = ""
    logic_value_home: str = ""

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / 1024 / 1024, 2)


def _el(parent, tag, text=None):
    e = ET.SubElement(parent, tag)
    if text is not None:
        e.text = text
    return e


def _data(parent, tag, value):
    _data_el = _el(parent, tag)
    _el(_data_el, "Data", value)
    return _data_el


def _to_bytes(root) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=False)


class FUSClient:
    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.http = requests.Session()
        self.session_cookie = ""
        self.scouter_cookie = ""
        self.enc_nonce = ""
        self.nonce = ""

    # ---- low level -------------------------------------------------------

    def _auth_header(self, echo_nonce: bool) -> str:
        n = self.enc_nonce if echo_nonce else ""
        sig = crypto.auth_signature(self.nonce)
        return f'FUS nonce="{n}", signature="{sig}", nc="", type="", realm="", newauth="1"'

    def _headers(self, auth=None):
        if auth is None:
            auth = 'FUS nonce="", signature="", nc="", type="", realm="", newauth="1"'
        h = {"Cache-Control": "no-cache", "User-Agent": "Kies2.0_FUS", "Authorization": auth}
        cookies = []
        if self.session_cookie:
            cookies.append(f"SESSION={self.session_cookie}")
        if self.scouter_cookie:
            cookies.append(f"SCOUTER={self.scouter_cookie}")
        if cookies:
            h["Cookie"] = "; ".join(cookies)
        return h

    @staticmethod
    def _cookie_value(header: str, name: str) -> str:
        result = ""
        for part in header.replace(",", ";").split(";"):
            if name in part and "=" in part:
                result = part.split("=", 1)[1].strip()
        return result

    def _absorb(self, resp: requests.Response):
        """Pick up cookies and a new nonce from any FUS response."""
        sc = resp.headers.get("Set-Cookie")
        if sc:
            self.session_cookie = self._cookie_value(sc, "SESSION")
            self.scouter_cookie = self._cookie_value(sc, "SCOUTER")
        nonce = resp.headers.get("NONCE")
        if nonce:
            self.enc_nonce = nonce
            self.nonce = crypto.decrypt_nonce(nonce)
        log.debug("SESSION=%s SCOUTER=%s nonce=%s", self.session_cookie, self.scouter_cookie, self.nonce)

    def _post(self, endpoint: str, body: bytes = b"") -> requests.Response:
        hdr = self._headers(self._auth_header(False) if body else None)
        r = self.http.post(FUS_HOST + endpoint, data=body, headers=hdr, timeout=self.timeout)
        self._absorb(r)
        return r

    def generate_nonce(self):
        r = self._post("/NF_DownloadGenerateNonce.do")
        if r.status_code != 200:
            raise FUSError(f"could not get nonce from FUS server (HTTP {r.status_code})")

    @staticmethod
    def _status(xml_text: str) -> int:
        try:
            return int(ET.fromstring(xml_text).findtext("./FUSBody/Results/Status", "0"))
        except (ET.ParseError, ValueError):
            return 0

    # ---- requests --------------------------------------------------------

    def _binary_inform_xml(self, model, csc, imei_sn, pda, csc_ver, phone, binary_nature):
        fw_ver = f"{pda}/{csc_ver}/{phone}/{pda}"
        root = ET.Element("FUSMsg")
        hdr = _el(root, "FUSHdr")
        _el(hdr, "ProtoVer", "1")
        _el(hdr, "SessionID", "0")
        _el(hdr, "MsgID", "1")
        body = _el(root, "FUSBody")
        put = _el(body, "Put")
        _el(put, "CmdID", "1")
        _data(put, "ACCESS_MODE", "2")
        _data(put, "BINARY_NATURE", "1" if binary_nature else "0")
        lang = _el(put, "CLIENT_LANGUAGE")
        _el(lang, "Type", "String")
        _el(lang, "Type", "ISO 3166-1-alpha-3")
        _el(lang, "Data", "1033")
        _data(put, "CLIENT_PRODUCT", "Smart Switch")
        _data(put, "CLIENT_VERSION", "4.4.1.23122_1")
        _data(put, "DEVICE_CONTENTS_DATA_VERSION", pda)
        _data(put, "DEVICE_CSC_CODE2_VERSION", csc_ver)
        _data(put, "DEVICE_FW_VERSION", fw_ver)
        _data(put, "DEVICE_LOCAL_CODE", csc)
        _data(put, "DEVICE_IMEI_PUSH", imei_sn)
        _data(put, "DEVICE_MODEL_NAME", model)
        _data(put, "DEVICE_PDA_CODE1_VERSION", pda)
        _data(put, "DEVICE_PHONE_FONT_VERSION", phone)
        _data(put, "DEVICE_PLATFORM", "Android")
        _data(put, "LOGIC_CHECK", crypto.logic_check(fw_ver, self.nonce))
        for tag, val in _EU_EXTRA.get(csc, []):
            _data(put, tag, val)
        get = _el(body, "Get")
        _el(get, "CmdID", "2")
        _el(get, "LATEST_FW_VERSION")
        return _to_bytes(root)

    def _binary_init_xml(self, filename):
        root = ET.Element("FUSMsg")
        _el(_el(root, "FUSHdr"), "ProtoVer", "1")
        put = _el(_el(root, "FUSBody"), "Put")
        _data(put, "BINARY_FILE_NAME", filename)
        _data(put, "LOGIC_CHECK", crypto.logic_check(filename, self.nonce))
        return b'<?xml version="1.0" encoding="UTF-8"?>' + _to_bytes(root)

    def check(self, model, csc, imei_sn, pda, csc_ver, phone, binary_nature=False) -> Firmware:
        """Ask FUS for the latest firmware. Raises FUSError when none is found."""
        self.generate_nonce()
        r = self._post("/NF_DownloadBinaryInform.do",
                       self._binary_inform_xml(model, csc, imei_sn, pda, csc_ver, phone, binary_nature))
        log.debug("BinaryInform HTTP %s: %s", r.status_code, r.text)
        if r.status_code != 200:
            raise FUSError(f"FUS BinaryInform failed (HTTP {r.status_code})")
        root = ET.fromstring(r.text)
        status = int(root.findtext("./FUSBody/Results/Status", "0"))
        if status != 200:
            raise FUSError(f"FUS returned status {status} (firmware not found, or wrong IMEI/serial for this model/CSC)")

        def put(tag):
            return root.findtext(f"./FUSBody/Put/{tag}/Data") or ""

        latest = root.findtext("./FUSBody/Results/LATEST_FW_VERSION/Data") or ""
        fw = Firmware(
            model=put("DEVICE_MODEL_NAME"),
            display_name=put("DEVICE_MODEL_DISPLAYNAME"),
            version=latest,
            display_version=latest[: latest.rfind("/")] if "/" in latest else latest,
            os=put("LATEST_OS_VERSION"),
            last_modified=put("LAST_MODIFIED"),
            filename=put("BINARY_NAME"),
            path=put("MODEL_PATH"),
            size_bytes=int(put("BINARY_BYTE_SIZE") or 0),
            model_type=put("DEVICE_MODEL_TYPE"),
            region=put("DEVICE_LOCAL_CODE"),
            binary_nature=int(put("BINARY_NATURE") or 0),
        )
        crc = put("BINARY_CRC")
        if crc:
            fw.crc = int(crc)
        if put("LOGIC_OPTION_FACTORY") == "1":
            fw.logic_value_factory = put("LOGIC_VALUE_FACTORY")
        if put("LOGIC_OPTION_HOME") == "1":
            fw.logic_value_home = put("LOGIC_VALUE_HOME")
        return fw

    def open_download(self, fw: Firmware, offset: int = 0, fallback: bool = False) -> requests.Response:
        """Init the binary download and return a streaming response (from `offset`)."""
        self.generate_nonce()
        r = self._post("/NF_DownloadBinaryInitForMass.do", self._binary_init_xml(fw.filename))
        log.debug("BinaryInit HTTP %s: %s", r.status_code, r.text)
        if r.status_code != 200 or self._status(r.text) != 200:
            raise FUSError(f"FUS BinaryInit failed (HTTP {r.status_code}, status {self._status(r.text)})")
        host = CLOUD_FALLBACK_HOST if fallback else CLOUD_HOST
        url = f"{host}/NF_DownloadBinaryForMass.do?file={fw.path}{fw.filename}"
        hdr = self._headers(self._auth_header(True))
        if offset:
            hdr["Range"] = f"bytes={offset}-"
        return self.http.get(url, headers=hdr, stream=True, timeout=25)
