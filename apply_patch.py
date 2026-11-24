#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Idempotent patch manager for bridging Qualcomm Hexagon NPU support into Unsloth Studio.

Usage:
  python apply_patch.py --apply
  python apply_patch.py --status
  python apply_patch.py --revert
"""

from __future__ import annotations

import argparse
import os
import py_compile
import shutil
import sys
from pathlib import Path


def _resolve_studio_home() -> Path:
    for env_key in ("UNSLOTH_STUDIO_HOME", "STUDIO_HOME"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return Path(os.path.expanduser(val))
    unsloth_home = os.environ.get("UNSLOTH_HOME", "").strip()
    if unsloth_home:
        return Path(os.path.expanduser(unsloth_home)) / "studio"
    return Path.home() / ".unsloth" / "studio"


STUDIO_HOME = _resolve_studio_home()
STUDIO_ROOT = STUDIO_HOME / "unsloth_studio" / "Lib" / "site-packages" / "studio"
BRIDGE_HOME = Path.home() / ".unsloth" / "qualcomm-npu-bridge"
MARKER = "# [QUALCOMM_HEXAGON_NPU_BRIDGE]"
BAK_SUFFIX = ".qualcomm_npu.bak"


def _patch_npu_py(text: str) -> str:
    target = """def detect_amd_npu() -> dict[str, Any]:
    \"\"\"Return NPU hardware details and whether FastFlowLM supports this platform.\"\"\"
    try:
        if sys.platform.startswith("linux"):
            info = _linux_probe()
        elif sys.platform == "win32":
            info = _windows_probe()
        else:
            info = {"present": False}
    except Exception as exc:  # noqa: BLE001 -- detection must never break startup or /status
        info = {"present": False, "error": str(exc)}
    info["enabled"] = npu_enabled()
    info["supported"] = (
        info["enabled"] and bool(info.get("present")) and info.get("family") == "XDNA2"
    )
    return info"""

    replacement = f"""{MARKER}
def _windows_qualcomm_probe() -> dict[str, Any]:
    import winreg

    base = r"SYSTEM\\CurrentControlSet\\Enum\\ACPI\\QCOM0D0A"
    try:
        qcom = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return {{"present": False}}
    driver_version = None
    name = None
    with qcom:
        try:
            instance = winreg.EnumKey(qcom, 0)
            with winreg.OpenKey(qcom, instance) as device:
                name = _reg_value(device, "FriendlyName") or _reg_value(device, "DeviceDesc")
                driver_key = _reg_value(device, "Driver")
            if driver_key:
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    rf"SYSTEM\\CurrentControlSet\\Control\\Class\\{{driver_key}}",
                ) as driver:
                    driver_version = _reg_value(driver, "DriverVersion")
        except OSError:
            return {{"present": False}}
    if name and ";" in name:
        name = name.rsplit(";", 1)[1]
    return {{
        "present": True,
        "vendor": "qualcomm",
        "family": "Hexagon",
        "name": name or "Qualcomm Hexagon NPU",
        "driver": "installed" if driver_version else None,
        "driver_version": driver_version,
        "firmware_version": None,
        "device_node": None,
    }}


def detect_amd_npu() -> dict[str, Any]:
    \"\"\"Return NPU hardware details and whether the configured NPU runtime supports this platform.\"\"\"
    try:
        if sys.platform.startswith("linux"):
            info = _linux_probe()
        elif sys.platform == "win32":
            info = _windows_probe()
            if not info.get("present"):
                info = _windows_qualcomm_probe()
        else:
            info = {{"present": False}}
    except Exception as exc:  # noqa: BLE001 -- detection must never break startup or /status
        info = {{"present": False, "error": str(exc)}}
    if info.get("present") and "vendor" not in info:
        info["vendor"] = "amd"
    info["enabled"] = npu_enabled()
    info["supported"] = (
        info["enabled"]
        and bool(info.get("present"))
        and info.get("family") in ("XDNA2", "Hexagon")
    )
    return info"""

    if target not in text:
        raise RuntimeError("Anchor not found in backend/utils/hardware/npu.py")
    return text.replace(target, replacement, 1)


def _patch_install_lemonade_prebuilt(text: str) -> str:
    target_1 = """def installed_lemond(root: Path, pins: Optional[dict] = None) -> Optional[Path]:
    \"\"\"The lemond binary of a complete install matching the pin, else None.\"\"\"
    pins = pins or load_pins()
    if direct_flm():
        return installed_flm_host(root, pins)"""

    replacement_1 = f"""{MARKER}
def _qualcomm_geniex_host() -> Optional[Path]:
    try:
        from backend.utils.hardware.npu import detect_amd_npu
        if detect_amd_npu().get("vendor") != "qualcomm":
            return None
    except Exception:
        return None
    host = Path.home() / ".unsloth" / "qualcomm-npu-bridge" / "geniex_host.py"
    if not host.is_file():
        return None
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    candidates = [
        Path(os.environ.get("UNSLOTH_GENIEX_BIN", "")) if os.environ.get("UNSLOTH_GENIEX_BIN") else None,
        Path(shutil.which("geniex")) if shutil.which("geniex") else None,
        local_app_data / "GenieX CLI" / "geniex.exe",
        local_app_data / "Programs" / "GenieX CLI" / "geniex.exe",
        local_app_data / "Microsoft" / "WinGet" / "Links" / "geniex.exe",
    ]
    if any(c is not None and c.is_file() for c in candidates):
        return host
    return None


def installed_lemond(root: Path, pins: Optional[dict] = None) -> Optional[Path]:
    \"\"\"The lemond binary of a complete install matching the pin, else None.\"\"\"
    qcom_host = _qualcomm_geniex_host()
    if qcom_host is not None:
        return qcom_host
    pins = pins or load_pins()
    if direct_flm():
        return installed_flm_host(root, pins)"""

    target_2 = """def installed_version(lemond: Path) -> Optional[str]:
    \"\"\"The Lemonade version recorded beside an installed lemond, or None.\"\"\"
    marker = _read_marker(lemond.parent)"""

    replacement_2 = """def installed_version(lemond: Path) -> Optional[str]:
    \"\"\"The Lemonade version recorded beside an installed lemond, or None.\"\"\"
    if lemond.name == "geniex_host.py":
        return "geniex-bridge"
    marker = _read_marker(lemond.parent)"""

    target_3 = """    pins = load_pins(pins_path)
    key = host_asset_key()
    if key is None:
        raise RuntimeError(
            "Lemonade publishes FastFlowLM builds for Windows and Linux on x86_64 only."
        )"""

    replacement_3 = """    qcom_host = _qualcomm_geniex_host()
    if qcom_host is not None:
        return qcom_host
    pins = load_pins(pins_path)
    key = host_asset_key()
    if key is None:
        raise RuntimeError(
            "Lemonade publishes FastFlowLM builds for Windows and Linux on x86_64 only."
        )"""

    for idx, (t, r) in enumerate(
        ((target_1, replacement_1), (target_2, replacement_2), (target_3, replacement_3)), 1
    ):
        if t not in text:
            raise RuntimeError(f"Anchor #{idx} not found in install_lemonade_prebuilt.py")
        text = text.replace(t, r, 1)
    return text


def _patch_npu_backend(text: str) -> str:
    # 1. Allow unsloth/ or qualcomm/ repo prefixes ending in -NPU in looks_like_npu_display_name
    t1 = """def looks_like_npu_display_name(name: Optional[str]) -> bool:
    \"\"\"A FastFlowLM repo (`FastFlowLM/<name>`) or a bare name ending in `-NPU<n>`, as shown in the app.\"\"\"
    if not isinstance(name, str):
        return False
    text = name.strip()
    if text.lower().startswith(_REPO_OWNER):
        rest = text[len(_REPO_OWNER) :]
        return bool(rest) and "/" not in rest and ":" not in rest
    return "/" not in text and ":" not in text and bool(_DISPLAY_NAME_SUFFIX.search(text))"""

    r1 = f"""{MARKER}
def looks_like_npu_display_name(name: Optional[str]) -> bool:
    \"\"\"A FastFlowLM/Qualcomm NPU repo or a bare name ending in `-NPU<n>`, as shown in the app.\"\"\"
    if not isinstance(name, str):
        return False
    text = name.strip()
    if text.lower().startswith(_REPO_OWNER):
        rest = text[len(_REPO_OWNER) :]
        return bool(rest) and "/" not in rest and ":" not in rest
    if ":" not in text and bool(_DISPLAY_NAME_SUFFIX.search(text)):
        if "/" not in text or text.lower().startswith(("unsloth/", "qualcomm/", "nexaai/")):
            return True
    return False"""

    # 2. Add _is_qualcomm() and Qualcomm-aware _installed_flm_version()
    t2 = """    def hardware(self) -> dict[str, Any]:
        if self._hardware is None:
            self._hardware = detect_amd_npu()
        return self._hardware"""

    r2 = """    def hardware(self) -> dict[str, Any]:
        if self._hardware is None:
            self._hardware = detect_amd_npu()
        return self._hardware

    def _is_qualcomm(self) -> bool:
        return self.hardware().get("vendor") == "qualcomm\""""

    t3 = """    def _installed_flm_version(self) -> Optional[str]:
        binary = self._flm_binary()"""

    r3 = """    def _installed_flm_version(self) -> Optional[str]:
        if self._is_qualcomm():
            return "v0.11.0 (GenieX)" if self._installed_lemond() is not None else None
        binary = self._flm_binary()"""

    # 3. _require_supported() for Qualcomm
    t4 = """    def _require_supported(self) -> None:
        hardware = self.hardware()
        if not hardware.get("present"):
            raise NpuError("No AMD Ryzen AI NPU was found on this machine.")
        if hardware.get("family") == "XDNA2" and hardware.get("enabled") is False:
            raise NpuError(
                "The AMD NPU is turned off by UNSLOTH_STUDIO_ENABLE_NPU. Unset it or set it "
                "to 1, then restart Unsloth Studio to use the NPU."
            )
        if not hardware.get("supported"):
            raise NpuError(
                "FastFlowLM needs an XDNA 2 NPU (Ryzen AI 300/400 or Ryzen AI Max); "
                "this NPU is XDNA 1."
            )"""

    r4 = """    def _require_supported(self) -> None:
        hardware = self.hardware()
        if not hardware.get("present"):
            raise NpuError("No supported NPU was found on this machine.")
        if self._is_qualcomm():
            if hardware.get("enabled") is False:
                raise NpuError(
                    "The Qualcomm Hexagon NPU is turned off by UNSLOTH_STUDIO_ENABLE_NPU. "
                    "Unset it or set it to 1, then restart Unsloth Studio to use the NPU."
                )
            return
        if hardware.get("family") == "XDNA2" and hardware.get("enabled") is False:
            raise NpuError(
                "The AMD NPU is turned off by UNSLOTH_STUDIO_ENABLE_NPU. Unset it or set it "
                "to 1, then restart Unsloth Studio to use the NPU."
            )
        if not hardware.get("supported"):
            raise NpuError(
                "FastFlowLM needs an XDNA 2 NPU (Ryzen AI 300/400 or Ryzen AI Max); "
                "this NPU is XDNA 1."
            )"""

    # 4. enable() and _validate() for Qualcomm
    t5 = """                # lemond keeps the old FastFlowLM and answers success when an update download fails.
                expected, actual = _pinned_flm_version(), self._flm_version()
                if expected and actual and actual != expected:"""

    r5 = """                # lemond keeps the old FastFlowLM and answers success when an update download fails.
                expected, actual = _pinned_flm_version(), self._flm_version()
                if not self._is_qualcomm() and expected and actual and actual != expected:"""

    t6 = """    def _validate(self) -> dict[str, Any]:
        binary = self._flm_binary()
        if binary is None:
            return {"ready": False, "problems": ["FastFlowLM was not found after installing it."]}"""

    r6 = """    def _validate(self) -> dict[str, Any]:
        if self._is_qualcomm():
            host = self._installed_lemond()
            if host is None:
                return {
                    "ready": False,
                    "problems": [
                        "Qualcomm GenieX CLI (geniex.exe) was not found. "
                        "Install it with: winget install --id Qualcomm.GenieX.CLI -e"
                    ],
                }
            return {
                "ready": True,
                "problems": [],
                "report": {"ready": True, "vendor": "qualcomm", "host": str(host)},
            }
        binary = self._flm_binary()
        if binary is None:
            return {"ready": False, "problems": ["FastFlowLM was not found after installing it."]}"""

    # 5. catalog() hf_repo fallback and skip local_q4nx on Qualcomm
    t7 = """            files = None if downloaded else self._model_files(checkpoint, flm_binary)
            hf_repo, hf_revision = read_model_repo(flm_binary, checkpoint)
            models.append("""

    r7 = """            files = None if downloaded else self._model_files(checkpoint, flm_binary)
            hf_repo, hf_revision = read_model_repo(flm_binary, checkpoint)
            if not hf_repo and isinstance(row.get("hf_repo"), str):
                hf_repo = row.get("hf_repo")
            models.append("""

    t8 = """        models.extend(self._local_rows(models, window_gb))
        return sorted(models, key = lambda model: (not model.downloaded, model.id))"""

    r8 = """        if not self._is_qualcomm():
            models.extend(self._local_rows(models, window_gb))
        return sorted(models, key = lambda model: (not model.downloaded, model.id))"""

    for idx, (t, r) in enumerate(
        ((t1, r1), (t2, r2), (t3, r3), (t4, r4), (t5, r5), (t6, r6), (t7, r7), (t8, r8)), 1
    ):
        if t not in text:
            raise RuntimeError(f"Anchor #{idx} not found in backend/core/inference/npu_backend.py")
        text = text.replace(t, r, 1)
    return text


def _patch_hardware_py(text: str) -> str:
    target = """            try:
                # Once a minute, not per sample, when the NPU reports no LUID.
                if _npu_luid is None and now - _npu_luid_at >= _NPU_LUID_TTL_S:
                    raw = (_rocm_windows_counter_stdout_uncached(_NPU_LUID_PS) or "").strip()
                    _npu_luid = int(raw) if raw.isdigit() and int(raw) else None
                    _npu_luid_at = now
                load = _npu_engine_load(_npu_luid) if _npu_luid is not None else None"""

    replacement = f"""            {MARKER}
            try:
                # Once a minute, not per sample, when the NPU reports no LUID.
                if _npu_luid is None and now - _npu_luid_at >= _NPU_LUID_TTL_S:
                    if _npu_hardware.get("vendor") == "qualcomm":
                        try:
                            import ctypes
                            import winreg
                            from ctypes import wintypes

                            with winreg.OpenKey(
                                winreg.HKEY_LOCAL_MACHINE,
                                r"SYSTEM\\CurrentControlSet\\Enum\\ACPI\\QCOM0D0A",
                            ) as _qk:
                                _qinst = f"ACPI\\\\QCOM0D0A\\\\{{winreg.EnumKey(_qk, 0)}}"

                            class _Guid(ctypes.Structure):
                                _fields_ = [
                                    ("Data1", wintypes.DWORD),
                                    ("Data2", wintypes.WORD),
                                    ("Data3", wintypes.WORD),
                                    ("Data4", ctypes.c_ubyte * 8),
                                ]

                            class _DevPropKey(ctypes.Structure):
                                _fields_ = [("fmtid", _Guid), ("pid", wintypes.ULONG)]

                            _pkey = _DevPropKey(
                                _Guid(
                                    0x60B193CB,
                                    0x5276,
                                    0x4D0F,
                                    (ctypes.c_ubyte * 8)(0x96, 0xFC, 0xF1, 0x73, 0xAB, 0xAD, 0x3E, 0xC6),
                                ),
                                2,
                            )
                            _cfg = ctypes.WinDLL("cfgmgr32")
                            _devinst = wintypes.DWORD()
                            if _cfg.CM_Locate_DevNodeW(ctypes.byref(_devinst), ctypes.c_wchar_p(_qinst), 0) == 0:
                                _ptype = wintypes.ULONG()
                                _qluid = ctypes.c_uint64()
                                _qsize = wintypes.ULONG(ctypes.sizeof(_qluid))
                                if (
                                    _cfg.CM_Get_DevNode_PropertyW(
                                        _devinst,
                                        ctypes.byref(_pkey),
                                        ctypes.byref(_ptype),
                                        ctypes.byref(_qluid),
                                        ctypes.byref(_qsize),
                                        0,
                                    )
                                    == 0
                                    and _qluid.value > 0
                                ):
                                    _npu_luid = int(_qluid.value)
                        except Exception:
                            _npu_luid = None
                    else:
                        raw = (_rocm_windows_counter_stdout_uncached(_NPU_LUID_PS) or "").strip()
                        _npu_luid = int(raw) if raw.isdigit() and int(raw) else None
                    _npu_luid_at = now
                if _npu_luid is not None and _npu_hardware.get("vendor") == "qualcomm":
                    _samples = _rocm_windows_adapter_engine_samples(_npu_luid)
                    if _samples:
                        _by_eng: dict[str, float] = {{}}
                        for _inst, _val in _samples:
                            if _engine_instance_luid(_inst) == _npu_luid and _val >= 0:
                                _m = re.search(r"_eng_(\\d+)", _inst)
                                if _m:
                                    _by_eng[_m.group(1)] = _by_eng.get(_m.group(1), 0.0) + float(_val)
                        load = round(min(max(_by_eng.values()), 100.0), 1) if _by_eng else 0.0
                    else:
                        load = 0.0
                else:
                    load = _npu_engine_load(_npu_luid) if _npu_luid is not None else None"""

    if target not in text:
        raise RuntimeError("Anchor not found in backend/utils/hardware/hardware.py")
    return text.replace(target, replacement, 1)


def _patch_q4nx_export_gate(text: str) -> str:
    target = """    return bool(status.get("supported")) and bool(status.get("ready"))"""
    replacement = f"""    {MARKER}
    if (status.get("hardware") or {{}}).get("vendor") == "qualcomm":
        return False
    return bool(status.get("supported")) and bool(status.get("ready"))"""
    if target not in text:
        raise RuntimeError("Anchor not found in backend/utils/q4nx_export_gate.py")
    return text.replace(target, replacement, 1)


def _patch_npu_compat(text: str) -> str:
    target = """    if not backend.hardware().get("supported"):
        return build_compat(None, None)"""
    replacement = f"""    {MARKER}
    if not backend.hardware().get("supported") or backend.hardware().get("vendor") == "qualcomm":
        return build_compat(None, None)"""
    if target not in text:
        raise RuntimeError("Anchor not found in backend/core/inference/npu_compat.py")
    return text.replace(target, replacement, 1)


TARGETS = [
    ("backend/utils/hardware/npu.py", _patch_npu_py),
    ("install_lemonade_prebuilt.py", _patch_install_lemonade_prebuilt),
    ("backend/core/inference/npu_backend.py", _patch_npu_backend),
    ("backend/utils/hardware/hardware.py", _patch_hardware_py),
    ("backend/utils/q4nx_export_gate.py", _patch_q4nx_export_gate),
    ("backend/core/inference/npu_compat.py", _patch_npu_compat),
]


def status() -> int:
    all_patched = True
    for rel, _fn in TARGETS:
        path = STUDIO_ROOT / rel
        bak = path.with_name(path.name + BAK_SUFFIX)
        content = path.read_text(encoding="utf-8") if path.is_file() else ""
        patched = MARKER in content
        all_patched = all_patched and patched
        print(f"[{'PATCHED' if patched else 'UNPATCHED'}] {rel} (backup={'yes' if bak.is_file() else 'no'})")
    return 0 if all_patched else 1


def _sync_bridge_runtime_files() -> None:
    """Ensure geniex_host.py and models.json are installed in ~/.unsloth/qualcomm-npu-bridge."""
    src_dir = Path(__file__).resolve().parent
    BRIDGE_HOME.mkdir(parents=True, exist_ok=True)
    if src_dir.resolve() == BRIDGE_HOME.resolve():
        return
    for name in ("geniex_host.py", "models.json", "apply_patch.py", "test_bridge.py"):
        src = src_dir / name
        dst = BRIDGE_HOME / name
        if src.is_file():
            if name == "models.json" and dst.is_file():
                continue  # Preserve user-customized models.json
            shutil.copy2(src, dst)
            print(f"[SYNCED] {dst}")


def apply_patches() -> int:
    _sync_bridge_runtime_files()
    for rel, fn in TARGETS:
        path = STUDIO_ROOT / rel
        if not path.is_file():
            raise FileNotFoundError(f"Missing target file: {path}")
        content = path.read_text(encoding="utf-8")
        if MARKER in content:
            print(f"[SKIP] {rel} already patched")
            continue
        bak = path.with_name(path.name + BAK_SUFFIX)
        if not bak.is_file():
            shutil.copy2(path, bak)
        patched = fn(content)
        path.write_text(patched, encoding="utf-8")
        py_compile.compile(str(path), doraise=True)
        print(f"[APPLIED] {rel}")
    return 0


def revert_patches() -> int:
    for rel, _fn in TARGETS:
        path = STUDIO_ROOT / rel
        bak = path.with_name(path.name + BAK_SUFFIX)
        if bak.is_file():
            shutil.copy2(bak, path)
            py_compile.compile(str(path), doraise=True)
            print(f"[REVERTED] {rel}")
        else:
            print(f"[NO BACKUP] {rel}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply", action="store_true", help="Apply Qualcomm Hexagon NPU patches")
    group.add_argument("--revert", action="store_true", help="Revert to original Unsloth Studio files")
    group.add_argument("--status", action="store_true", help="Check patch status")
    args = parser.parse_args()
    if args.status:
        return status()
    if args.apply:
        return apply_patches()
    if args.revert:
        return revert_patches()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
