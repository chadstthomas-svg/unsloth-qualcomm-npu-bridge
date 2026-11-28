#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Update-proof runtime hook injecting Qualcomm Hexagon NPU support into Unsloth Studio.

Loaded at Python startup via `unsloth_qualcomm_npu_bridge.pth` in `Lib\\site-packages`.
Instead of modifying Studio source files on disk (which updates overwrite), this hook
registers a `sys.meta_path` import interceptor that augments Studio's NPU, hardware,
prebuilt-installer, and Q4NX-gate modules in memory as they are loaded.

Auto-disables if:
- running on a non-Windows platform,
- `UNSLOTH_QUALCOMM_NPU_DISABLE=1` is set in the environment, or
- upstream Unsloth Studio already ships native Qualcomm GenieX support.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import functools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional

_BRIDGE_HOME = Path(__file__).resolve().parent
_GENIEX_HOST = _BRIDGE_HOME / "geniex_host.py"
_MODELS_JSON = _BRIDGE_HOME / "models.json"
_HOOK_ATTR = "_is_qualcomm_npu_bridge_hook"
_DISPLAY_NAME_SUFFIX = re.compile(r"-npu\d*$", re.IGNORECASE)
_REPO_OWNER = "fastflowlm/"
_POWER_MODE_ENABLED_FALSE = ("0", "false", "no", "off")

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def _tag(fn: Any) -> Any:
    setattr(fn, _HOOK_ATTR, True)
    return fn


def _is_tagged(fn: Any) -> bool:
    return bool(getattr(fn, _HOOK_ATTR, False))


def _npu_enabled() -> bool:
    return os.environ.get("UNSLOTH_STUDIO_ENABLE_NPU", "").strip().lower() not in _POWER_MODE_ENABLED_FALSE


def _reg_value(key: Any, name: str) -> Optional[str]:
    import winreg

    try:
        value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return str(value) if value is not None else None


def _windows_qualcomm_probe() -> dict[str, Any]:
    """Detect Qualcomm Hexagon NPU via Windows PNP registry (`ACPI\\QCOM0D0A`)."""
    if sys.platform != "win32":
        return {"present": False}
    import winreg

    base = r"SYSTEM\CurrentControlSet\Enum\ACPI\QCOM0D0A"
    try:
        qcom = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return {"present": False}
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
                    rf"SYSTEM\CurrentControlSet\Control\Class\{driver_key}",
                ) as driver:
                    driver_version = _reg_value(driver, "DriverVersion")
        except OSError:
            return {"present": False}
    if name and ";" in name:
        name = name.rsplit(";", 1)[1]
    return {
        "present": True,
        "vendor": "qualcomm",
        "family": "Hexagon",
        "name": name or "Qualcomm Hexagon NPU",
        "driver": "installed" if driver_version else None,
        "driver_version": driver_version,
        "firmware_version": None,
        "device_node": None,
    }


def _find_geniex_binary() -> Optional[Path]:
    """Locate `geniex.exe` across env override, PATH, App Paths registry, and Windows install paths."""
    env_bin = os.environ.get("UNSLOTH_GENIEX_BIN", "").strip()
    if env_bin and Path(env_bin).is_file():
        return Path(env_bin)
    which_bin = shutil.which("geniex")
    if which_bin and Path(which_bin).is_file():
        return Path(which_bin)
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    prog_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    candidates: list[Path] = [
        local_app_data / "GenieX" / "geniex.exe",
        local_app_data / "Programs" / "GenieX" / "geniex.exe",
        prog_files / "GenieX" / "geniex.exe",
        local_app_data / "GenieX CLI" / "geniex.exe",
        local_app_data / "Programs" / "GenieX CLI" / "geniex.exe",
        local_app_data / "Microsoft" / "WinGet" / "Links" / "geniex.exe",
        prog_files / "GenieX CLI" / "geniex.exe",
    ]
    if sys.platform == "win32":
        try:
            import winreg

            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(
                        root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\geniex.exe"
                    ) as key:
                        reg_path, _ = winreg.QueryValueEx(key, None)
                        if isinstance(reg_path, str) and reg_path.strip():
                            candidates.insert(0, Path(reg_path.strip()))
                except OSError:
                    pass
        except ImportError:
            pass
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


_GENIEX_VERSION_CACHE: Optional[tuple[float, str]] = None


def _cached_geniex_version(binary: Path) -> str:
    """Return cached 'vX.Y.Z (GenieX)' (or 'GenieX' fallback) from `geniex --version`."""
    global _GENIEX_VERSION_CACHE
    now = time.monotonic()
    if _GENIEX_VERSION_CACHE is not None and (now - _GENIEX_VERSION_CACHE[0]) < 60.0:
        return _GENIEX_VERSION_CACHE[1]
    label = "GenieX"
    try:
        kwargs: dict[str, Any] = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
        done = subprocess.run(
            [str(binary), "--version", "--skip-update"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
            **kwargs,
        )
        output = (done.stdout or "") + (done.stderr or "")
        for line in output.splitlines():
            if "GenieX CLI Version:" in line:
                ver = line.split(":", 1)[1].strip().lstrip("v")
                if ver:
                    label = f"v{ver} (GenieX)"
                break
    except (OSError, subprocess.SubprocessError):
        pass
    _GENIEX_VERSION_CACHE = (now, label)
    return label


def _is_qualcomm_hw() -> bool:
    """Return True when Qualcomm Hexagon NPU hardware is active on this machine."""
    for mod_name in (
        "utils.hardware.npu",
        "backend.utils.hardware.npu",
        "studio.backend.utils.hardware.npu",
    ):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "detect_amd_npu"):
            try:
                return mod.detect_amd_npu().get("vendor") == "qualcomm"
            except Exception:
                pass
    return bool(_windows_qualcomm_probe().get("present"))


def _qualcomm_geniex_host() -> Optional[Path]:
    """Return `geniex_host.py` when running on Qualcomm NPU with `geniex.exe` installed."""
    if not _is_qualcomm_hw():
        return None
    if not _GENIEX_HOST.is_file():
        return None
    return _GENIEX_HOST if _find_geniex_binary() is not None else None


def _lookup_curated_hf_repo(checkpoint: str) -> Optional[str]:
    """Look up or derive an `-NPU` Hugging Face repo string for a GenieX checkpoint."""
    ckpt = checkpoint.strip()
    if not ckpt:
        return None
    ckpt_lower = ckpt.lower()
    try:
        raw = json.loads(_MODELS_JSON.read_text(encoding="utf-8"))
        for entry in raw.get("models") or ():
            if not isinstance(entry, dict):
                continue
            entry_ckpt = str(entry.get("checkpoint") or "").strip().lower()
            if entry_ckpt == ckpt_lower and isinstance(entry.get("hf_repo"), str):
                return entry["hf_repo"]
    except (OSError, ValueError):
        pass
    if "/" not in ckpt:
        return None
    base, _, tag = ckpt.partition(":")
    base = base.strip()
    tag_l = tag.strip().lower()
    if base.lower().endswith("-gguf"):
        base = base[:-5]
    if tag_l == "w4a16" or base.lower().startswith("qualcomm/"):
        if base.upper().endswith("-NPU"):
            base = base[:-4]
        if not base.upper().endswith("-QAIRT-NPU"):
            base = f"{base}-QAIRT-NPU"
    elif not base.upper().endswith("-NPU"):
        base = f"{base}-NPU"
    return base


def _upstream_already_supports_qualcomm(module: Any) -> bool:
    """Detect whether upstream Studio already natively implements Qualcomm NPU support."""
    module_file = getattr(module, "__file__", None)
    if module_file:
        path = Path(module_file).resolve()
        for parent in path.parents:
            if parent.name == "studio" and (parent / "backend" / "core" / "inference" / "geniex_host.py").is_file():
                return True
            if parent.name == "site-packages":
                break
    native_probe = getattr(module, "_windows_qualcomm_probe", None)
    if native_probe is not None and not _is_tagged(native_probe):
        return True
    return False


def _resolve_qualcomm_npu_luid() -> Optional[int]:
    """Read `DEVPKEY_GPU_LUID` for the Qualcomm NPU adapter via `cfgmgr32.dll` (<1 ms)."""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Enum\ACPI\QCOM0D0A",
        ) as qcom_key:
            qinst = f"ACPI\\QCOM0D0A\\{winreg.EnumKey(qcom_key, 0)}"

        class _Guid(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        class _DevPropKey(ctypes.Structure):
            _fields_ = [("fmtid", _Guid), ("pid", wintypes.ULONG)]

        pkey = _DevPropKey(
            _Guid(
                0x60B193CB,
                0x5276,
                0x4D0F,
                (ctypes.c_ubyte * 8)(0x96, 0xFC, 0xF1, 0x73, 0xAB, 0xAD, 0x3E, 0xC6),
            ),
            2,
        )
        cfg = ctypes.WinDLL("cfgmgr32")
        devinst = wintypes.DWORD()
        if cfg.CM_Locate_DevNodeW(ctypes.byref(devinst), ctypes.c_wchar_p(qinst), 0) != 0:
            return None
        ptype = wintypes.ULONG()
        qluid = ctypes.c_uint64()
        qsize = wintypes.ULONG(ctypes.sizeof(qluid))
        if (
            cfg.CM_Get_DevNode_PropertyW(
                devinst,
                ctypes.byref(pkey),
                ctypes.byref(ptype),
                ctypes.byref(qluid),
                ctypes.byref(qsize),
                0,
            )
            == 0
            and qluid.value > 0
        ):
            return int(qluid.value)
    except Exception:
        return None
    return None


def _patch_npu_module(mod: Any) -> None:
    if _upstream_already_supports_qualcomm(mod):
        return
    orig_detect = getattr(mod, "detect_amd_npu", None)
    if orig_detect is None or _is_tagged(orig_detect):
        return

    mod._windows_qualcomm_probe = _tag(_windows_qualcomm_probe)

    @functools.wraps(orig_detect)
    def wrapped_detect_amd_npu() -> dict[str, Any]:
        info = orig_detect()
        if not info.get("present") and sys.platform == "win32":
            qinfo = _windows_qualcomm_probe()
            if qinfo.get("present"):
                enabled = _npu_enabled()
                qinfo["enabled"] = enabled
                qinfo["supported"] = enabled and qinfo.get("family") in ("XDNA2", "Hexagon")
                return qinfo
        if info.get("present") and "vendor" not in info:
            info["vendor"] = "amd"
        return info

    mod.detect_amd_npu = _tag(wrapped_detect_amd_npu)


def _patch_installer_module(mod: Any) -> None:
    if _upstream_already_supports_qualcomm(mod) or hasattr(mod, "GENIEX_HOST"):
        return

    orig_installed_lemond = getattr(mod, "installed_lemond", None)
    if orig_installed_lemond is not None and not _is_tagged(orig_installed_lemond):

        @functools.wraps(orig_installed_lemond)
        def wrapped_installed_lemond(root: Path, pins: Optional[dict] = None) -> Optional[Path]:
            qcom_host = _qualcomm_geniex_host()
            if qcom_host is not None:
                return qcom_host
            return orig_installed_lemond(root, pins)

        mod.installed_lemond = _tag(wrapped_installed_lemond)

    orig_installed_version = getattr(mod, "installed_version", None)
    if orig_installed_version is not None and not _is_tagged(orig_installed_version):

        @functools.wraps(orig_installed_version)
        def wrapped_installed_version(lemond: Path) -> Optional[str]:
            if Path(lemond).name == "geniex_host.py":
                return "geniex-bridge"
            return orig_installed_version(lemond)

        mod.installed_version = _tag(wrapped_installed_version)

    orig_install = getattr(mod, "install", None)
    if orig_install is not None and not _is_tagged(orig_install):

        @functools.wraps(orig_install)
        def wrapped_install(*args: Any, **kwargs: Any) -> Path:
            qcom_host = _qualcomm_geniex_host()
            if qcom_host is not None:
                return qcom_host
            return orig_install(*args, **kwargs)

        mod.install = _tag(wrapped_install)


def _patch_npu_backend_module(mod: Any) -> None:
    if _upstream_already_supports_qualcomm(mod):
        return

    # If npu_backend bound detect_amd_npu before utils.hardware.npu was patched, rebind it.
    bound_detect = getattr(mod, "detect_amd_npu", None)
    if bound_detect is not None and not _is_tagged(bound_detect):
        for npu_mod_name in ("utils.hardware.npu", "backend.utils.hardware.npu", "studio.backend.utils.hardware.npu"):
            npu_mod = sys.modules.get(npu_mod_name)
            if npu_mod is not None and not _is_tagged(getattr(npu_mod, "detect_amd_npu", None)):
                _patch_npu_module(npu_mod)
            if npu_mod is not None and _is_tagged(getattr(npu_mod, "detect_amd_npu", None)):
                mod.detect_amd_npu = npu_mod.detect_amd_npu
                break

    orig_looks_like = getattr(mod, "looks_like_npu_display_name", None)
    if orig_looks_like is not None and not _is_tagged(orig_looks_like):

        @functools.wraps(orig_looks_like)
        def wrapped_looks_like(name: Optional[str]) -> bool:
            if orig_looks_like(name):
                return True
            if not isinstance(name, str):
                return False
            text = name.strip()
            if text.lower().startswith(_REPO_OWNER):
                rest = text[len(_REPO_OWNER) :]
                return bool(rest) and "/" not in rest and ":" not in rest
            if ":" not in text and bool(_DISPLAY_NAME_SUFFIX.search(text)):
                return text.count("/") <= 1
            return False

        mod.looks_like_npu_display_name = _tag(wrapped_looks_like)

    orig_read_repo = getattr(mod, "read_model_repo", None)
    if orig_read_repo is not None and not _is_tagged(orig_read_repo):

        @functools.wraps(orig_read_repo)
        def wrapped_read_model_repo(flm_binary: Optional[Path], checkpoint: str) -> tuple[Optional[str], Optional[str]]:
            hf_repo, hf_revision = orig_read_repo(flm_binary, checkpoint)
            if not hf_repo and _is_qualcomm_hw() and checkpoint:
                hf_repo = _lookup_curated_hf_repo(str(checkpoint))
            return hf_repo, hf_revision

        mod.read_model_repo = _tag(wrapped_read_model_repo)

    cls = getattr(mod, "LemonadeNpuBackend", None)
    if cls is None:
        return

    if not hasattr(cls, "_is_qualcomm"):
        cls._is_qualcomm = lambda self: self.hardware().get("vendor") == "qualcomm"

    orig_installed_flm_ver = getattr(cls, "_installed_flm_version", None)
    if orig_installed_flm_ver is not None and not _is_tagged(orig_installed_flm_ver):

        @functools.wraps(orig_installed_flm_ver)
        def wrapped_installed_flm_version(self: Any) -> Optional[str]:
            if self._is_qualcomm():
                if self._installed_lemond() is None:
                    return None
                geniex_bin = _find_geniex_binary()
                return _cached_geniex_version(geniex_bin) if geniex_bin is not None else "GenieX"
            return orig_installed_flm_ver(self)

        cls._installed_flm_version = _tag(wrapped_installed_flm_version)

    orig_flm_ver = getattr(cls, "_flm_version", None)
    if orig_flm_ver is not None and not _is_tagged(orig_flm_ver):

        @functools.wraps(orig_flm_ver)
        def wrapped_flm_version(self: Any, binary: Optional[Path] = None) -> Optional[str]:
            if self._is_qualcomm():
                return None
            return orig_flm_ver(self, binary)

        cls._flm_version = _tag(wrapped_flm_version)

    orig_require_supported = getattr(cls, "_require_supported", None)
    if orig_require_supported is not None and not _is_tagged(orig_require_supported):

        @functools.wraps(orig_require_supported)
        def wrapped_require_supported(self: Any) -> None:
            hardware = self.hardware()
            if self._is_qualcomm():
                if hardware.get("enabled") is False:
                    raise mod.NpuError(
                        "The Qualcomm Hexagon NPU is turned off by UNSLOTH_STUDIO_ENABLE_NPU. "
                        "Unset it or set it to 1, then restart Unsloth Studio to use the NPU."
                    )
                return
            orig_require_supported(self)

        cls._require_supported = _tag(wrapped_require_supported)

    orig_validate = getattr(cls, "_validate", None)
    if orig_validate is not None and not _is_tagged(orig_validate):

        @functools.wraps(orig_validate)
        def wrapped_validate(self: Any) -> dict[str, Any]:
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
            return orig_validate(self)

        cls._validate = _tag(wrapped_validate)

    orig_local_rows = getattr(cls, "_local_rows", None)
    if orig_local_rows is not None and not _is_tagged(orig_local_rows):

        @functools.wraps(orig_local_rows)
        def wrapped_local_rows(self: Any, catalog: list[Any], window_gb: Optional[float]) -> list[Any]:
            if self._is_qualcomm():
                return []
            return orig_local_rows(self, catalog, window_gb)

        cls._local_rows = _tag(wrapped_local_rows)


def _patch_hardware_module(mod: Any) -> None:
    if _upstream_already_supports_qualcomm(mod):
        return
    orig_load_pct = getattr(mod, "npu_load_percent", None)
    if orig_load_pct is None or _is_tagged(orig_load_pct):
        return

    @functools.wraps(orig_load_pct)
    def wrapped_npu_load_percent() -> Optional[float]:
        if sys.platform != "win32":
            return orig_load_pct()
        lock = getattr(mod, "_npu_load_lock", None)
        if lock is None:
            return orig_load_pct()
        try:
            with lock:
                if mod._npu_hardware is None:
                    from utils.hardware.npu import detect_amd_npu

                    mod._npu_hardware = detect_amd_npu()
                if mod._npu_hardware.get("vendor") != "qualcomm":
                    # Non-Qualcomm machine: release our outer lock path via orig if reentrant isn't safe
                    pass
                else:
                    if not mod._npu_hardware.get("supported"):
                        return None
                    now = time.monotonic()
                    cached = mod._npu_load_cache
                    load_ttl = getattr(mod, "_NPU_LOAD_TTL_S", 3.0)
                    luid_ttl = getattr(mod, "_NPU_LUID_TTL_S", 60.0)
                    if cached is not None and now - cached[0] < max(load_ttl, 4 * cached[1]):
                        return cached[2]
                    if mod._npu_luid is None and now - mod._npu_luid_at >= luid_ttl:
                        mod._npu_luid = _resolve_qualcomm_npu_luid()
                        mod._npu_luid_at = now
                    load: Optional[float] = None
                    if mod._npu_luid is not None:
                        samples_fn = getattr(mod, "_rocm_windows_adapter_engine_samples", None)
                        luid_fn = getattr(mod, "_engine_instance_luid", None)
                        if samples_fn is not None and luid_fn is not None:
                            samples = samples_fn(mod._npu_luid)
                            if samples:
                                by_eng: dict[str, float] = {}
                                for inst, val in samples:
                                    if luid_fn(inst) == mod._npu_luid and val >= 0:
                                        m = re.search(r"_eng_(\d+)", inst)
                                        if m:
                                            by_eng[m.group(1)] = by_eng.get(m.group(1), 0.0) + float(val)
                                load = round(min(max(by_eng.values()), 100.0), 1) if by_eng else 0.0
                            else:
                                load = 0.0
                        else:
                            load = mod._npu_engine_load(mod._npu_luid)
                    done = time.monotonic()
                    mod._npu_load_cache = (done, done - now, load)
                    return load
        except Exception:
            mod._npu_luid = None
            mod._npu_luid_at = 0.0
            return None
        return orig_load_pct()

    mod.npu_load_percent = _tag(wrapped_npu_load_percent)


def _patch_q4nx_gate_module(mod: Any) -> None:
    if _upstream_already_supports_qualcomm(mod):
        return
    orig_allowed = getattr(mod, "q4nx_export_allowed", None)
    if orig_allowed is None or _is_tagged(orig_allowed):
        return

    @functools.wraps(orig_allowed)
    def wrapped_q4nx_export_allowed() -> bool:
        if _is_qualcomm_hw():
            return False
        return orig_allowed()

    mod.q4nx_export_allowed = _tag(wrapped_q4nx_export_allowed)


def _patch_npu_compat_module(mod: Any) -> None:
    if _upstream_already_supports_qualcomm(mod):
        return
    orig_current = getattr(mod, "current", None)
    if orig_current is None or _is_tagged(orig_current):
        return

    @functools.wraps(orig_current)
    def wrapped_current(install_converter: bool = True) -> dict:
        if _is_qualcomm_hw():
            return mod.build_compat(None, None)
        return orig_current(install_converter=install_converter)

    mod.current = _tag(wrapped_current)


_PATCHERS: dict[str, Callable[[Any], None]] = {}
for _name in ("utils.hardware.npu", "backend.utils.hardware.npu", "studio.backend.utils.hardware.npu"):
    _PATCHERS[_name] = _patch_npu_module
for _name in ("install_lemonade_prebuilt", "studio.install_lemonade_prebuilt"):
    _PATCHERS[_name] = _patch_installer_module
for _name in ("core.inference.npu_backend", "backend.core.inference.npu_backend", "studio.backend.core.inference.npu_backend"):
    _PATCHERS[_name] = _patch_npu_backend_module
for _name in ("utils.hardware.hardware", "backend.utils.hardware.hardware", "studio.backend.utils.hardware.hardware"):
    _PATCHERS[_name] = _patch_hardware_module
for _name in ("utils.q4nx_export_gate", "backend.utils.q4nx_export_gate", "studio.backend.utils.q4nx_export_gate"):
    _PATCHERS[_name] = _patch_q4nx_gate_module
for _name in ("core.inference.npu_compat", "backend.core.inference.npu_compat", "studio.backend.core.inference.npu_compat"):
    _PATCHERS[_name] = _patch_npu_compat_module


class _PatchingLoader:
    """Loader proxy that runs `patch_fn(module)` immediately after `orig.exec_module`."""

    _is_qualcomm_npu_bridge_loader = True

    def __init__(self, orig_loader: Any, patch_fn: Callable[[Any], None]) -> None:
        self._orig = orig_loader
        self._patch_fn = patch_fn

    def create_module(self, spec: Any) -> Any:
        create = getattr(self._orig, "create_module", None)
        return create(spec) if create is not None else None

    def exec_module(self, module: Any) -> None:
        self._orig.exec_module(module)
        self._patch_fn(module)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._orig, item)


class _QualcommNpuFinder:
    """`sys.meta_path` finder wrapping target Studio modules with Qualcomm NPU hooks."""

    _is_qualcomm_npu_bridge_finder = True

    def __init__(self) -> None:
        self._local = threading.local()

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        patch_fn = _PATCHERS.get(fullname)
        if patch_fn is None:
            return None
        if getattr(self._local, "active", False):
            return None
        self._local.active = True
        try:
            for finder in list(sys.meta_path):
                if finder is self or getattr(finder, "_is_qualcomm_npu_bridge_finder", False):
                    continue
                find_spec = getattr(finder, "find_spec", None)
                if find_spec is None:
                    continue
                spec = find_spec(fullname, path, target)
                if spec is not None:
                    loader = getattr(spec, "loader", None)
                    if (
                        loader is not None
                        and hasattr(loader, "exec_module")
                        and not getattr(loader, "_is_qualcomm_npu_bridge_loader", False)
                    ):
                        spec.loader = _PatchingLoader(loader, patch_fn)
                    return spec
            return None
        finally:
            self._local.active = False

    def invalidate_caches(self) -> None:
        pass


def is_installed() -> bool:
    """Return True if the Qualcomm NPU `sys.meta_path` hook is currently registered."""
    return any(getattr(f, "_is_qualcomm_npu_bridge_finder", False) for f in sys.meta_path)


def install() -> bool:
    """Register the update-proof import hook (idempotent) and patch already-loaded targets."""
    global _INSTALLED
    if sys.platform != "win32":
        return False
    if os.environ.get("UNSLOTH_QUALCOMM_NPU_DISABLE", "").strip() == "1":
        return False
    with _INSTALL_LOCK:
        if not is_installed():
            sys.meta_path.insert(0, _QualcommNpuFinder())
        for mod_name, patch_fn in _PATCHERS.items():
            mod = sys.modules.get(mod_name)
            if mod is not None:
                patch_fn(mod)
        _INSTALLED = True
        return True
