#!/usr/bin/env python3
"""End-to-end verification suite for the Qualcomm Hexagon NPU bridge in Unsloth Studio."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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
        target_model = next(
            (m for m in downloaded if "unsloth--Qwen3-0.6B" in m.id),
            next((m for m in downloaded if "Qwen3-0.6B" in m.id and "QAIRT" not in m.display_name), downloaded[0]),
        )

        if "Qwen3-0.6B" in target_model.id:
            resolved_path = npu.model_path_for_name("Qwen3-0.6B-NPU", start_runtime=False)
            print(f"Resolved 'Qwen3-0.6B-NPU' -> {resolved_path}")
            assert resolved_path == target_model.model_path

        moonbeam = next((m for m in downloaded if "Moon-Beam-8B-A1B-NPU-Q4_0" in m.id), None)
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

    print("\n=== 9. Multi-Machine & SSD-Offload Execution Planner Matrix ===")
    bridge_dir = str(Path(__file__).resolve().parent)
    if bridge_dir not in sys.path:
        sys.path.insert(0, bridge_dir)
    import geniex_host

    live_profile = geniex_host.detect_system_profile()
    print("Live profile:", json.dumps(live_profile))
    assert live_profile["total_ram_gb"] >= 4.0

    models_matrix = [
        {"name": "0.38GB tiny", "size": 0.38, "n_layers": 28, "mtp_path": None},
        {"name": "5.25GB dense", "size": 5.25, "n_layers": 36, "mtp_path": None},
        {"name": "14.8GB MoE+MTP", "size": 14.8, "n_layers": 42, "mtp_path": "mtp-gemma.gguf"},
        {"name": "19.5GB MoE+MTP", "size": 19.5, "n_layers": 48, "mtp_path": "mtp-qwen.gguf"},
        {"name": "95.0GB MoE+MTP", "size": 95.0, "n_layers": 60, "mtp_path": "mtp-flash.gguf"},
    ]
    for ram_gb in (8.0, 16.0, 32.0, 64.0, 128.0):
        sim = {"total_ram_gb": ram_gb, "avail_ram_gb": ram_gb * 0.72, "disk_free_gb": 500.0}
        summaries = []
        for row in models_matrix:
            plan = geniex_host.plan_model_execution(row, sim, requested_ctx=32768)
            first = plan["attempts"][0]
            if row["size"] > ram_gb * 0.65:
                assert plan["tier"] == "hybrid_npu_ssd", f"Expected hybrid_npu_ssd for {row} on {ram_gb}GB"
                assert plan["ssd_offload"] is True
                assert plan["env_overrides"].get("GGML_HEXAGON_RT_REPACK") == "0"
                assert 4 <= first["ngl"] < row["n_layers"]
            else:
                assert plan["tier"] == "in_ram_npu", f"Expected in_ram_npu for {row} on {ram_gb}GB"
                assert plan["ssd_offload"] is False
                assert first["ngl"] == -1
            if row["mtp_path"]:
                assert first["use_mtp"] is True
            total_warm = sum(float(a.get("warm_timeout", 300.0)) for a in plan["attempts"])
            assert total_warm <= 850.0, f"Cumulative warm_timeout {total_warm}s exceeds 850s budget"
            summaries.append(f"{row['name']}={plan['tier']}(ngl={first['ngl']},ctx={plan['ctx']})")
        print(f"  RAM={int(ram_gb):3d}GB -> " + " | ".join(summaries))

    print("\n=== 10. Memory Clamp, Catalog Cache & Crash Auto-Respawn Regression ===")
    moonbeam_row = {"name": "Moon-Beam-8B-NPU", "size": 4.71, "n_layers": 32, "mtp_path": None}
    for avail_gb, expected_ctx in ((5.8, 4096), (8.0, 8192), (11.0, 16384)):
        p_tight = geniex_host.plan_model_execution(
            moonbeam_row,
            {"total_ram_gb": 15.61, "avail_ram_gb": avail_gb, "disk_free_gb": 200.0},
            requested_ctx=16384,
        )
        assert p_tight["tier"] == "in_ram_npu"
        assert p_tight["ctx"] == expected_ctx, (
            f"avail={avail_gb}GB expected ctx={expected_ctx}, got {p_tight['ctx']}"
        )
    print("  10a. Tight avail_ram_gb context clamp passed: avail 5.8/8.0/11.0GB -> ctx 4096/8192/16384")

    p_gemma_tight = geniex_host.plan_model_execution(
        {"size": 13.61, "n_layers": 42, "mtp_path": "mtp.gguf"},
        {"total_ram_gb": 15.61, "avail_ram_gb": 3.23, "disk_free_gb": 65.0},
        requested_ctx=4096,
    )
    assert p_gemma_tight["tier"] == "hybrid_npu_ssd" and p_gemma_tight["attempts"][0]["ngl"] == 0
    assert sum(float(a.get("warm_timeout", 300.0)) for a in p_gemma_tight["attempts"]) <= 850.0
    p_gemma_mid = geniex_host.plan_model_execution(
        {"size": 13.61, "n_layers": 42, "mtp_path": "mtp.gguf"},
        {"total_ram_gb": 15.61, "avail_ram_gb": 6.0, "disk_free_gb": 65.0},
        requested_ctx=4096,
    )
    assert p_gemma_mid["tier"] == "hybrid_npu_ssd"
    assert p_gemma_mid["attempts"][0]["ngl"] == 2 and p_gemma_mid["attempts"][1]["ngl"] == 0
    assert sum(float(a.get("warm_timeout", 300.0)) for a in p_gemma_mid["attempts"]) <= 850.0
    p_inram_tune = geniex_host.plan_model_execution(
        moonbeam_row,
        {"total_ram_gb": 15.61, "avail_ram_gb": 9.0, "disk_free_gb": 50.0},
        requested_ctx=4096,
    )
    assert p_inram_tune["env_overrides"].get("GGML_HEXAGON_DMA64") == "1"
    assert p_inram_tune["env_overrides"].get("GGML_HEXAGON_OPFUSION") == "1"
    assert "GGML_HEXAGON_VMEM" not in p_inram_tune["env_overrides"]
    p_qairt_tune = geniex_host.plan_model_execution(
        {"name": "qualcomm/Qwen3-0.6B", "runtime": "qairt", "size": 0.72, "n_layers": 28},
        {"total_ram_gb": 15.61, "avail_ram_gb": 9.0, "disk_free_gb": 50.0},
        requested_ctx=4096,
    )
    assert p_qairt_tune["env_overrides"].get("GENIEX_DECODE_POLL") == "1"
    assert geniex_host._translate_flm_args("--pmode turbo --spec-type draft-mtp --draft-tokens 4") == [
        "--power-mode",
        "burst",
        "--spec-type",
        "draft-mtp",
        "--draft-tokens",
        "4",
    ]
    print("  10d. Tight/mid-RAM hybrid_npu_ssd (ngl=0/2), HTP/QAIRT tuning & spec pass-through checks passed")

    try:
        re_status = npu.enable()
        flm_ver = (re_status.get("versions") or {}).get("fastflowlm") or ""
        print(f"  Dynamic runtime_hook versions.fastflowlm={flm_ver!r}")
        assert flm_ver.startswith("v") and "GenieX" in flm_ver

        t_cat = []
        for _ in range(3):
            t0 = time.perf_counter()
            cat_rows = npu.catalog()
            t_cat.append(time.perf_counter() - t0)
            assert len(cat_rows) >= 1
        print(
            f"  10b. Catalog timings: call1={t_cat[0]*1000:.1f}ms, "
            f"call2={t_cat[1]*1000:.1f}ms, call3={t_cat[2]*1000:.1f}ms"
        )
        assert t_cat[1] < 0.05 and t_cat[2] < 0.05, f"Expected cached catalog <50ms, got {t_cat}"

        npu.load(target_model.id, context_length=8192)
        upstream2 = npu.upstream()
        client2 = httpx.Client(trust_env=False, timeout=180.0)
        headers2 = {"Authorization": f"Bearer {upstream2.api_key}"}

        kill_res = subprocess.run(
            ["taskkill", "/F", "/IM", "geniex.exe"],
            capture_output=True,
            text=True,
            check=False,
        )
        print(f"  10c. Simulated child crash (taskkill rc={kill_res.returncode}); sending immediate chat...")
        time.sleep(0.5)

        r_respawn = client2.post(
            f"{upstream2.base_url}/chat/completions",
            headers=headers2,
            json={
                "model": upstream2.model,
                "messages": [{"role": "user", "content": "Say 'Respawned!' and nothing else."}],
                "max_tokens": 16,
                "stream": False,
                "think": False,
            },
        )
        assert r_respawn.status_code == 200, f"Expected 200 after respawn, got {r_respawn.status_code}: {r_respawn.text}"
        respawn_reply = r_respawn.json()["choices"][0]["message"]["content"]
        print("  10c. Auto-respawn reply:", repr(respawn_reply))
        assert respawn_reply.strip(), "Expected non-empty reply after child crash respawn"
        npu.unload()
    finally:
        npu.shutdown()

    print("\nALL QUALCOMM HEXAGON NPU BRIDGE TESTS PASSED!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
