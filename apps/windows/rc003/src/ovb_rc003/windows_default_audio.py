"""Raw ctypes + COM query for the Windows default audio capture endpoint.

Uses the IMMDeviceEnumerator / IMMDevice / IPropertyStore APIs from
``mmdeviceapi.h`` / ``propsys.h``. No third-party dependency, no auto-
selection, no Windows-side mutation - this module only READS.

Why this exists:
    ``audio_output.AudioOutput.enumerate_input_endpoints`` (PortAudio) and
    ``windows_diagnostics.check_dji_mic_2_input`` see what *PortAudio* knows
    about. PortAudio's view does not always match Windows's own default-input
    pick (XRBM-031 "检查与修复"). When a user changes the Windows default
    input and our diagnostic still reports the old device, the user has no
    way to tell whether the app's read is stale, Windows itself is stale, or
    some middleman (VB-CABLE, WASAPI exclusive mode) is in the way. The
    diagnostics page now surfaces the *Windows* answer alongside the
    *PortAudio* answer so the user can see at a glance which one is which.

What this module deliberately is NOT:
    - Not a Windows-side mutator. There is no ``SetDefaultEndpoint`` path.
      A voice-bridge app that silently changes the user's mic is a much
      worse failure mode than one that only reports.
    - Not a PortAudio replacement. PortAudio is still the source of truth
      for whether the app can actually open an endpoint for capture.
    - Not exposed to the GUI layer. It is consumed only by
      ``windows_diagnostics.check_windows_default_input``, which itself runs
      under the existing ``_isolated`` subprocess guard.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Optional


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _make_iid(s: str) -> _GUID:
    g = _GUID()
    parts = s.split("-")
    g.Data1 = int(parts[0], 16)
    g.Data2 = int(parts[1], 16)
    g.Data3 = int(parts[2], 16)
    b = bytes.fromhex(parts[3] + parts[4])
    for i, byte in enumerate(b):
        g.Data4[i] = byte
    return g


# IIDs from Windows SDK 10.0.26100 / ``mmdeviceapi.h`` / ``propsys.h``.
# CLSID_MMDeviceEnumerator = {BCDE0395-E52F-467C-8E3D-C4579291692E}
_CLSID_MMDeviceEnumerator = _make_iid("bcde0395-e52f-467c-8e3d-c4579291692e")
# IID_IUnknown = {00000000-0000-0000-C000-000000000046}
_IID_IUnknown = _make_iid("00000000-0000-0000-c000-000000000046")
# IID_IMMDeviceEnumerator = {A95664D2-9614-4F35-A746-DE8DB63617E6}
_IID_IMMDeviceEnumerator = _make_iid("a95664d2-9614-4f35-a746-de8db63617e6")
# IID_IMMDevice = {D666063F-1587-4E43-81F1-B948E807363F}
_IID_IMMDevice = _make_iid("d666063f-1587-4e43-81f1-b948e807363f")
# IID_IPropertyStore = {886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}
_IID_IPropertyStore = _make_iid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")
# PKEY_Device_FriendlyName = {A45C254E-DF1C-4EFD-8020-67D146A850E0}, 14
_PKEY_FmtId = _make_iid("a45c254e-df1c-4efd-8020-67d146a850e0")
_PKEY_Pid = 14


_ole32 = None


def _ole32_dll():
    """Lazy bind + argtype setup for ole32. Doing this at import time would
    crash off-Windows on the windll lookup; the function-level lazy bind
    keeps the module importable on any platform even though it only ever
    does useful work on Windows."""
    global _ole32
    if _ole32 is None:
        _ole32 = ctypes.windll.ole32
        _ole32.CoInitialize.argtypes = [wintypes.LPVOID]
        _ole32.CoInitialize.restype = ctypes.c_long
        _ole32.CoCreateInstance.argtypes = [
            ctypes.POINTER(_GUID),
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(wintypes.LPVOID),
        ]
        _ole32.CoCreateInstance.restype = ctypes.c_long
    return _ole32


def _vtable_slot(ptr, slot):
    """Read the function pointer at vtable slot ``slot`` of an IUnknown-
    derived object. Slot 0 is QueryInterface, slot 1 AddRef, slot 2 Release;
    the interface-specific methods start at slot 3."""
    vtbl = ctypes.c_void_p.from_address(ptr).value
    return ctypes.c_void_p.from_address(
        vtbl + slot * ctypes.sizeof(ctypes.c_void_p)
    ).value


def _qi(ptr, iid):
    """QueryInterface. Returns a new interface pointer or raises OSError."""
    qi_addr = _vtable_slot(ptr, 0)
    qi_func = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        wintypes.LPVOID,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(wintypes.LPVOID),
    )(qi_addr)
    out = wintypes.LPVOID()
    hr = qi_func(ptr, ctypes.byref(iid), ctypes.byref(out))
    if hr != 0 or not out.value:
        raise OSError(f"QueryInterface failed hr=0x{hr & 0xFFFFFFFF:08X}")
    return out.value


def _release(ptr):
    rel_addr = _vtable_slot(ptr, 2)
    rel_func = ctypes.WINFUNCTYPE(ctypes.c_ulong, wintypes.LPVOID)(rel_addr)
    rel_func(ptr)


class _PROPVARIANT(ctypes.Structure):
    # ``PROPVARIANT`` is a discriminated union; we only ever read the
    # ``VT_LPWSTR`` (vt == 31) arm, so a 16-byte struct (4 uint16 header +
    # one pointer-sized union slot) is the simplest faithful layout on x64.
    _fields_ = [
        ("vt", ctypes.c_uint16),
        ("wReserved1", ctypes.c_uint16),
        ("wReserved2", ctypes.c_uint16),
        ("wReserved3", ctypes.c_uint16),
        ("p", ctypes.c_void_p),
    ]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]


def query_default_capture_endpoint_name() -> Optional[str]:
    """Return the friendly name of Windows's current default capture (input)
    endpoint at ``eConsole`` role, or ``None`` if it cannot be determined.

    Never raises - off-Windows or on any COM/ctypes failure, returns
    ``None``. Callers must be prepared to render "未知" / "无法读取" instead
    of a device name, not crash.
    """
    if sys.platform != "win32":
        return None
    try:
        ole32 = _ole32_dll()
        ole32.CoInitialize(None)

        # CoCreateInstance(CLSID_MMDeviceEnumerator, NULL,
        #                  CLSCTX_INPROC_SERVER=0x1, IID_IMMDeviceEnumerator)
        p = wintypes.LPVOID()
        hr = ole32.CoCreateInstance(
            ctypes.byref(_CLSID_MMDeviceEnumerator),
            None,
            0x1,
            ctypes.byref(_IID_IMMDeviceEnumerator),
            ctypes.byref(p),
        )
        if hr != 0 or not p.value:
            return None
        p_enum = p.value
        try:
            # IMMDeviceEnumerator::GetDefaultAudioEndpoint(eCapture=1, eConsole=0)
            # IMMDeviceEnumerator vtable:
            #   0=QI, 1=AddRef, 2=Release,
            #   3=EnumAudioEndpoints, 4=GetDefaultAudioEndpoint,
            #   5=GetDevice, 6=RegisterEndpointNotificationCallback,
            #   7=UnregisterEndpointNotificationCallback
            get_def_addr = _vtable_slot(p_enum, 4)
            get_def = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                wintypes.LPVOID,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.POINTER(wintypes.LPVOID),
            )(get_def_addr)
            p_dev = wintypes.LPVOID()
            hr = get_def(p_enum, 1, 0, ctypes.byref(p_dev))
            if hr != 0 or not p_dev.value:
                return None
            p_dev_ptr = p_dev.value
            try:
                # IMMDevice::OpenPropertyStore(STGM_READ=0, &pStore)
                # IMMDevice vtable:
                #   0=QI, 1=AddRef, 2=Release,
                #   3=Activate, 4=OpenPropertyStore,
                #   5=GetId, 6=GetState
                open_addr = _vtable_slot(p_dev_ptr, 4)
                open_func = ctypes.WINFUNCTYPE(
                    ctypes.c_long,
                    wintypes.LPVOID,
                    ctypes.c_int,
                    ctypes.POINTER(wintypes.LPVOID),
                )(open_addr)
                p_store = wintypes.LPVOID()
                hr = open_func(p_dev_ptr, 0, ctypes.byref(p_store))
                if hr != 0 or not p_store.value:
                    return None
                p_store_ptr = p_store.value
                try:
                    # QI the property store to IPropertyStore.
                    p_ips = _qi(p_store_ptr, _IID_IPropertyStore)
                    try:
                        # IPropertyStore::GetValue(key, *pv)
                        # IPropertyStore vtable:
                        #   0=QI, 1=AddRef, 2=Release,
                        #   3=GetCount, 4=GetAt, 5=GetValue
                        get_val_addr = _vtable_slot(p_ips, 5)
                        get_val = ctypes.WINFUNCTYPE(
                            ctypes.c_long,
                            wintypes.LPVOID,
                            ctypes.c_void_p,  # PROPERTYKEY*
                            ctypes.POINTER(_PROPVARIANT),
                        )(get_val_addr)
                        key = _PROPERTYKEY()
                        key.fmtid = _PKEY_FmtId
                        key.pid = _PKEY_Pid
                        prop = _PROPVARIANT()
                        hr = get_val(p_ips, ctypes.byref(key), ctypes.byref(prop))
                        if hr != 0:
                            return None
                        if prop.vt != 31:  # VT_LPWSTR
                            return None
                        if not prop.p:
                            return None
                        return ctypes.wstring_at(prop.p)
                    finally:
                        _release(p_ips)
                finally:
                    _release(p_store_ptr)
            finally:
                _release(p_dev_ptr)
        finally:
            _release(p_enum)
    except Exception:
        return None
