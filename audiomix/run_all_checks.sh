#!/usr/bin/env bash
# 一键验证：手算案例 + 数据仓案例 + 边界语义 + 推子联动/双配方 + 规模案例（30 素材/100 clip/120s）
set -euo pipefail
cd "$(dirname "$0")"
python3 -m tests.test_hand_calc /tmp/audiomix_verify_hand
python3 -m tests.test_archive
python3 -m tests.test_edge
python3 -m tests.test_faders /tmp/audiomix_verify_faders
python3 -m demo.scale_demo /tmp/audiomix_verify_scale
python3 -m demo.fader_demo /tmp/audiomix_verify_fader_demo
echo
echo "================ 全量验证通过 ================"
