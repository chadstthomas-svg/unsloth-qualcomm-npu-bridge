#!/usr/bin/env python3
"""End-to-end verification suite for the Qualcomm Hexagon NPU bridge in Unsloth Studio."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx


def _resolve_studio_pkg() -> Path:
    for env_key in ("UNSLOTH_STUDIO_HOME", "STUDIO_HOME"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return Path(os.path.expanduser(val)) / "unsloth_studio" / "Lib" / "site-packages" / "studio"
    return Path.home() / ".unsloth" / "studio" / "unsloth_studio" / "Lib" / "site-packages" / "studio"


STUDIO_PKG = _resolve_studio_pkg()
STUDIO_BACKEND = STUDIO_PKG / "backend"
for p in (str(STUDIO_BACKEND), str(STUDIO_PKG)):
    if p not in sys.path:
        sys.path.insert(0, p)

STOCK_TARGETS = (
    "backend/utils/hardware/npu.py",
    "install_lemonade_prebuilt.py",
    "backend/core/inference/npu_backend.py",
    "backend/utils/hardware/hardware.py",
    "backend/utils/q4nx_export_gate.py",
    "backend/core/inference/npu_compat.py",
)
LEGACY_MARKER = "# [QUALCOMM_HEXAGON_NPU_BRIDGE]"


def main() -> int:
    print("=== 0. Update-Proof .pth Runtime Hook & Clean Stock Studio Verification ===")
    hook_mod = sys.modules.get("runtime_hook")
    assert hook_mod is not None and hook_mod.is_installed(), (
        "Expected runtime_hook to be auto-installed via Lib/site-packages/unsloth_qualcomm_npu_bridge.pth"
    )
    print(f"runtime_hook loaded from: {hook_mod.__file__} (is_installed=True)")
    for rel in STOCK_TARGETS:
        p = STUDIO_PKG / rel
        assert p.is_file(), f"Missing Studio file: {p}"
        txt = p.read_text(encoding="utf-8", errors="replace")
        assert LEGACY_MARKER not in txt, f"Found legacy inline patch marker in {p}"
    print("All 6 target Unsloth Studio source files verified clean stock (0 inline edits).")

    print("\n=== 1. Hardware Detection (detect_amd_npu) ===")
    from utils.hardware.npu import detect_amd_npu

    hw = detect_amd_npu()
    print(json.dumps(hw, indent=2))
    assert hw.get("present") is True, "Expected NPU present=True"
    assert hw.get("vendor") == "qualcomm", f"Expected vendor='qualcomm', got {hw.get('vendor')}"
    assert hw.get("family") == "Hexagon", f"Expected family='Hexagon', got {hw.get('family')}"
    assert hw.get("supported") is True, "Expected NPU supported=True"

    print("\n=== 2. Windows NPU LUID Utilization Probe (npu_load_percent) ===")
    from utils.hardware import hardware as hw_mod

    load_pct = hw_mod.npu_load_percent()
    print(f"Resolved _npu_luid={hw_mod._npu_luid}, current npu_load_percent={load_pct}")
    assert hw_mod._npu_luid is not None and hw_mod._npu_luid > 0, "Expected valid Windows NPU LUID"

    print("\n=== 3. AMD Q4NX Gates Disabled on Qualcomm ===")
    from core.inference import npu_compat
    from utils.q4nx_export_gate import q4nx_export_allowed

    compat = npu_compat.current()
    print("npu_compat.current():", json.dumps(compat))
    assert compat.get("fastflowlm_known") is False
    assert compat.get("converter_known") is False

    print("\n=== 4. LemonadeNpuBackend.enable() & status() ===")
    from core.inference.npu_backend import get_npu_backend, looks_like_npu_display_name

    assert looks_like_npu_display_name("Qwen3-0.6B-NPU") is True
    assert looks_like_npu_display_name("unsloth/Qwen3-0.6B-NPU") is True

    npu = get_npu_backend()
    try:
        enabled_status = npu.enable()
        print("Status after enable():", json.dumps(enabled_status, indent=2))
        assert enabled_status["supported"] is True
        assert enabled_status["runtime_installed"] is True
        assert enabled_status["ready"] is True
        assert enabled_status["state"] == "ready"
        assert q4nx_export_allowed() is False, "Q4NX export should remain False on Qualcomm"

        print("\n=== 5. LemonadeNpuBackend.catalog() & Name Resolution ===")
        catalog = npu.catalog()
        print(f"Catalog count: {len(catalog)}")
        for m in catalog:
            print(
                f"  - {m.id} | display={m.display_name} | downloaded={m.downloaded} "
                f"| size_gb={m.size_gb} | fits_memory={m.fits_memory}"
            )
        downloaded = [m for m in catalog if m.downloaded]
        assert downloaded, "Expected at least one downloaded NPU model"
        target_model = next((m for m in downloaded if "Qwen3-0.6B" in m.id), downloaded[0])

        if "Qwen3-0.6B" in target_model.id:
            resolved_path = npu.model_path_for_name("Qwen3-0.6B-NPU", start_runtime=False)
            print(f"Resolved 'Qwen3-0.6B-NPU' -> {resolved_path}")
            assert resolved_path == target_model.model_path

        moonbeam = next((m for m in downloaded if "Moon-Beam-8B-A1B-GGUF-Q4_0" in m.id), None)
        if moonbeam is not None:
            mb_resolved = npu.model_path_for_name("tablinum/Moon-Beam-8B-A1B-NPU", start_runtime=False)
            print(f"Resolved 'tablinum/Moon-Beam-8B-A1B-NPU' -> {mb_resolved}")
            assert mb_resolved == moonbeam.model_path

        print(f"\n=== 6. LemonadeNpuBackend.load({target_model.id!r}) ===")
        loaded = npu.load(target_model.id, context_length=4096)
        print("Loaded model:", loaded.to_json())
        assert npu.is_loaded is True
        upstream = npu.upstream()
        print("ManagedUpstream:", upstream)

        print("\n=== 7. Non-Streaming & Streaming Chat via ManagedUpstream ===")
        client = httpx.Client(trust_env=False, timeout=120.0)
        headers = {"Authorization": f"Bearer {upstream.api_key}"}

        # Non-streaming with Studio's 'think': False field
        r1 = client.post(
            f"{upstream.base_url}/chat/completions",
            headers=headers,
            json={
                "model": upstream.model,
                "messages": [{"role": "user", "content": "What is 2 + 2? Reply in one short sentence."}],
                "max_tokens": 32,
                "stream": False,
                "think": False,
            },
        )
        print("Non-streaming status:", r1.status_code)
        assert r1.status_code == 200, r1.text
        data1 = r1.json()
        reply1 = data1["choices"][0]["message"]["content"]
        print("Non-streaming reply:", repr(reply1))
        print("Non-streaming timings:", data1.get("timings"))

        # Streaming SSE verifying injected timings on final finish_reason chunk
        streamed_text = []
        final_timings = None
        with client.stream(
            "POST",
            f"{upstream.base_url}/chat/completions",
            headers=headers,
            json={
                "model": upstream.model,
                "messages": [{"role": "user", "content": "Say 'Hexagon NPU active!' and nothing else."}],
                "max_tokens": 24,
                "stream": True,
                "think": False,
            },
        ) as r2:
            assert r2.status_code == 200
            for line in r2.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload_str = line[5:].strip()
                if payload_str == "[DONE]":
                    break
                chunk = json.loads(payload_str)
                delta = ((chunk.get("choices") or [{}])[0]).get("delta") or {}
                if delta.get("content"):
                    streamed_text.append(delta["content"])
                if "timings" in chunk:
                    final_timings = chunk["timings"]

        joined = "".join(streamed_text)
        print("Streaming reply:", repr(joined))
        print("Streaming injected timings:", final_timings)
        assert joined.strip(), "Expected non-empty streaming reply"
        assert final_timings is not None and final_timings.get("predicted_per_second", 0) > 0

        print("\n=== 8. LemonadeNpuBackend.unload() & shutdown() ===")
        unloaded_path = npu.unload()
        print("Unloaded:", unloaded_path)
        assert unloaded_path == target_model.model_path
        assert npu.is_loaded is False
    finally:
        npu.shutdown()

    post_shutdown_status = npu.status()
    print("Post-shutdown status (persisted validation):", json.dumps(post_shutdown_status, indent=2))
    assert post_shutdown_status["ready"] is True, "Expected ready=True persisted via npu_validated.json"
    print("\nALL QUALCOMM HEXAGON NPU BRIDGE TESTS PASSED!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
