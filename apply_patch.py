#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Update-proof shim manager for bridging Qualcomm Hexagon NPU support into Unsloth Studio.

Installs a lightweight `.pth` startup hook (`unsloth_qualcomm_npu_bridge.pth`) inside
Unsloth Studio's `Lib\\site-packages` so Studio updates never overwrite or break the shim,
while leaving all stock Unsloth Studio source files untouched on disk.

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
SITE_PACKAGES = STUDIO_HOME / "unsloth_studio" / "Lib" / "site-packages"
STUDIO_ROOT = SITE_PACKAGES / "studio"
BRIDGE_HOME = Path.home() / ".unsloth" / "qualcomm-npu-bridge"
PTH_FILE = SITE_PACKAGES / "unsloth_qualcomm_npu_bridge.pth"
PTH_CONTENT = (
    'import os, sys; _d = os.environ.get("UNSLOTH_QUALCOMM_BRIDGE_HOME") '
    'or os.path.join(os.path.expanduser("~"), ".unsloth", "qualcomm-npu-bridge"); '
    'os.path.isfile(os.path.join(_d, "runtime_hook.py")) '
    'and (sys.path.insert(0, _d) if _d not in sys.path else None, '
    '__import__("runtime_hook").install())\n'
)

MARKER = "# [QUALCOMM_HEXAGON_NPU_BRIDGE]"
BAK_SUFFIX = ".qualcomm_npu.bak"

TARGETS = (
    "backend/utils/hardware/npu.py",
    "install_lemonade_prebuilt.py",
    "backend/core/inference/npu_backend.py",
    "backend/utils/hardware/hardware.py",
    "backend/utils/q4nx_export_gate.py",
    "backend/core/inference/npu_compat.py",
)

RUNTIME_FILES = (
    "runtime_hook.py",
    "geniex_host.py",
    "convert_to_hexagon.py",
    "models.json",
    "apply_patch.py",
    "test_bridge.py",
)


def _legacy_patched_files() -> list[tuple[str, Path, Path]]:
    found: list[tuple[str, Path, Path]] = []
    for rel in TARGETS:
        path = STUDIO_ROOT / rel
        bak = path.with_name(path.name + BAK_SUFFIX)
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            if MARKER in content:
                found.append((rel, path, bak))
    return found


def _merge_models_json(src: Path, dst: Path) -> None:
    """Merge new curated presets from `src` into `dst` without dropping custom entries."""
    import json

    if not dst.is_file():
        shutil.copy2(src, dst)
        print(f"[SYNCED] {dst}")
        return
    try:
        src_data = json.loads(src.read_text(encoding="utf-8"))
        dst_data = json.loads(dst.read_text(encoding="utf-8"))
        src_models = src_data.get("models") or []
        dst_models = dst_data.get("models") or []
        by_ckpt = {
            str(m.get("checkpoint") or "").strip().lower(): i
            for i, m in enumerate(dst_models)
            if isinstance(m, dict) and m.get("checkpoint")
        }
        added = 0
        updated = 0
        for entry in src_models:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("checkpoint") or "").strip().lower()
            if not key:
                continue
            if key not in by_ckpt:
                dst_models.append(entry)
                by_ckpt[key] = len(dst_models) - 1
                added += 1
            else:
                # Backfill new metadata keys (n_layers, mtp_file, min_ram_gb) cleanly
                existing = dst_models[by_ckpt[key]]
                for k, v in entry.items():
                    if k not in existing:
                        existing[k] = v
                        updated += 1
        if added or updated:
            dst_data["models"] = dst_models
            dst.write_text(json.dumps(dst_data, indent=2) + "\n", encoding="utf-8")
            print(f"[MERGED] {dst} (+{added} models, +{updated} metadata fields)")
    except Exception:
        shutil.copy2(src, dst)
        print(f"[SYNCED] {dst}")


def _sync_bridge_runtime_files() -> None:
    """Copy runtime_hook.py, geniex_host.py, and models.json into ~/.unsloth/qualcomm-npu-bridge."""
    src_dir = Path(__file__).resolve().parent
    BRIDGE_HOME.mkdir(parents=True, exist_ok=True)
    if src_dir.resolve() == BRIDGE_HOME.resolve():
        return
    for name in RUNTIME_FILES:
        src = src_dir / name
        dst = BRIDGE_HOME / name
        if src.is_file():
            if name == "models.json":
                _merge_models_json(src, dst)
                continue
            shutil.copy2(src, dst)
            print(f"[SYNCED] {dst}")


def status() -> int:
    pth_ok = PTH_FILE.is_file() and "runtime_hook" in PTH_FILE.read_text(encoding="utf-8", errors="replace")
    print(f"[{'INSTALLED' if pth_ok else 'MISSING'}] .pth hook: {PTH_FILE}")

    runtime_ok = True
    for name in ("runtime_hook.py", "geniex_host.py", "models.json"):
        p = BRIDGE_HOME / name
        exists = p.is_file()
        runtime_ok = runtime_ok and exists
        print(f"[{'OK' if exists else 'MISSING'}] runtime file: {p}")

    hook_mod = sys.modules.get("runtime_hook")
    hook_active = bool(hook_mod is not None and getattr(hook_mod, "is_installed", lambda: False)())
    print(f"[{'ACTIVE' if hook_active else 'INACTIVE'}] in-process runtime_hook")

    for rel in TARGETS:
        path = STUDIO_ROOT / rel
        bak = path.with_name(path.name + BAK_SUFFIX)
        content = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        legacy = MARKER in content
        state = "LEGACY-PATCHED" if legacy else "STOCK / UNMODIFIED"
        extra = f" (legacy_backup={'yes' if bak.is_file() else 'no'})" if bak.is_file() else ""
        print(f"[{state}] {rel}{extra}")

    return 0 if (pth_ok and runtime_ok) else 1


def apply_patches() -> int:
    _sync_bridge_runtime_files()
    for required in ("runtime_hook.py", "geniex_host.py", "models.json"):
        if not (BRIDGE_HOME / required).is_file():
            raise FileNotFoundError(f"Missing required runtime file: {BRIDGE_HOME / required}")
    if not SITE_PACKAGES.is_dir():
        raise FileNotFoundError(f"Unsloth Studio site-packages not found at: {SITE_PACKAGES}")

    legacy = _legacy_patched_files()
    if legacy:
        print("[WARNING] Legacy inline source patches detected in Studio files:")
        for rel, _path, bak in legacy:
            print(f"  - {rel} (backup={'yes' if bak.is_file() else 'no'})")
        print(
            "  Run `--revert-legacy` to restore `.qualcomm_npu.bak` backups if they match your "
            "installed Studio version, or update/reinstall Studio (`uv pip install --upgrade unsloth`)."
        )

    PTH_FILE.write_text(PTH_CONTENT, encoding="utf-8")
    print(f"[INSTALLED] {PTH_FILE}")
    return 0


def revert_patches() -> int:
    if PTH_FILE.is_file():
        PTH_FILE.unlink()
        print(f"[REMOVED] {PTH_FILE}")
    else:
        print(f"[ABSENT] {PTH_FILE}")

    for rel, path, bak in _legacy_patched_files():
        if bak.is_file():
            shutil.copy2(bak, path)
            py_compile.compile(str(path), doraise=True)
            print(f"[REVERTED LEGACY] {rel}")
        else:
            print(f"[NO BACKUP FOR LEGACY PATCH] {rel}")
    return 0


def revert_legacy() -> int:
    legacy = _legacy_patched_files()
    if not legacy:
        print("[OK] All target Studio files are already clean stock.")
        return 0
    for rel, path, bak in legacy:
        if bak.is_file():
            shutil.copy2(bak, path)
            py_compile.compile(str(path), doraise=True)
            print(f"[REVERTED LEGACY] {rel}")
        else:
            print(f"[NO BACKUP] {rel}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply", action="store_true", help="Install update-proof Qualcomm Hexagon NPU .pth hook")
    group.add_argument("--revert", action="store_true", help="Remove .pth hook (and revert legacy inline patches if present)")
    group.add_argument("--revert-legacy", action="store_true", help="Revert legacy inline patches from .qualcomm_npu.bak backups")
    group.add_argument("--status", action="store_true", help="Check .pth hook, runtime files, and Studio source cleanliness")
    args = parser.parse_args()
    if args.status:
        return status()
    if args.apply:
        return apply_patches()
    if args.revert:
        return revert_patches()
    if args.revert_legacy:
        return revert_legacy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
