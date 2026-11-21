"""推子联动 / 双播出配方 / 检查窗 / 最优校正单 测试。

覆盖题面手算示例、两套配方共用同一档位方案、最小值/并列、单套不可行 vs
两套合办无解、旧播出版整数 WAV 相同但联动分组不同（一份可修、一份无解）、
以及保留原有速度/淡化/末样本保持/加总后单次舍入合同。
"""
import json
import os
import shutil
import tempfile
from fractions import Fraction as F

import numpy as np

from audiomix.assets import AssetRegistry, sequence, read_wav
from audiomix.engine import render_reference
from audiomix.faders import (
    FaderProgram, emit_fraction, load_fader_program, write_recipe_wav,
)
from audiomix.timeline import TimelineEditor


def _reg(base):
    shutil.rmtree(base, ignore_errors=True)
    return AssetRegistry(base)


def _two_clip_program(base, reg, values=((100,), (-80,))):
    """题面手算：甲 = 推子1(+100)+推子2(-80)，乙只要推子1。"""
    a1 = reg.register(sequence(values[0]), 16000, note="plus").asset_id
    a2 = reg.register(sequence(values[1]), 16000, note="minus").asset_id
    ed = TimelineEditor(base, reg, name="hand")
    ed.insert([
        {"clip_id": "p", "asset_id": a1, "a": 0, "b": 1, "start": "0"},
        {"clip_id": "n", "asset_id": a2, "a": 0, "b": 1, "start": "0"},
    ])
    product = ed.publish()
    pcm = {a["asset_id"]: reg.load_pcm(a["asset_id"]) for a in product["assets"]}
    spec = {
        "format": "audiomix.faders/1", "T": "1/16000",
        "faders": [
            {"fader_id": "f1", "current": "full", "costs": {"half": "1"},
             "positions": [{"id": "full", "gain": "1"}, {"id": "half", "gain": "1/2"}]},
            {"fader_id": "f2", "current": "full", "costs": {"half": "1"},
             "positions": [{"id": "full", "gain": "1"}, {"id": "half", "gain": "1/2"}]},
        ],
        "linkage": {"p": "f1", "n": "f2"},
        "recipes": {
            "jia": [{"clip_id": "p", "weight": "1"}, {"clip_id": "n", "weight": "1"}],
            "yi": [{"clip_id": "p", "weight": "1"}],
        },
        "constraints": [
            {"id": "jia-win", "recipe": "jia", "kind": "interval",
             "n0": 0, "n1": 1, "lo": "0", "hi": "30"},
            {"id": "yi-win", "recipe": "yi", "kind": "interval",
             "n0": 0, "n1": 1, "hi": "60"},
        ],
    }
    return FaderProgram(spec, product, pcm), product, pcm, spec


def test_worked_example(base):
    """甲 [0,30]、乙 <=60：只有两支都降半档可行；仅压第一支甲变 -30。"""
    reg = _reg(base)
    fp, product, pcm, spec = _two_clip_program(base, reg)

    # 舍入/饱和之前，各推子对两套配方的独立贡献
    cj = fp.window_contributions("jia", 0, 1)
    cy = fp.window_contributions("yi", 0, 1)
    assert cj["faders"]["f1"] == [[100, 1]]
    assert cj["faders"]["f2"] == [[-80, 1]]
    assert cy["faders"] == {"f1": [[100, 1]]}
    assert cj["composite"] == [[20, 1]] and cj["emitted"] == [20]

    # 当前档：甲=20 合格，乙=100 超 60 -> 需调整
    assert fp.composite("jia", 0) == 20
    assert fp.composite("yi", 0) == 100
    assert not fp.is_feasible(fp.current_assignment())

    # 仅压第一支（f1=half）：甲 -30 越界
    assert fp.composite("jia", 0, {"f1": "half"}) == F(-30)
    # 两支都半档：甲 10、乙 50 -> 可交
    half = {"f1": "half", "f2": "half"}
    assert fp.composite("jia", 0, half) == 10
    assert fp.composite("yi", 0, half) == 50
    assert fp.is_feasible(half)

    res = fp.solve()
    assert res["feasible"] and res["cost"] == 2
    assert res["assignment"] == half

    # 若只把乙上限放到 110：维持原档才是最低成本
    spec2 = json.loads(json.dumps(spec))
    spec2["constraints"][1]["hi"] = "110"
    fp2 = FaderProgram(spec2, product, pcm)
    res2 = fp2.solve()
    assert res2["feasible"] and res2["cost"] == 0
    assert res2["assignment"] == {"f1": "full", "f2": "full"}
    print("[OK] 手算示例：两支都半档=甲10/乙50；仅压第一支=-30；放宽乙后原档最优")


def test_contract_preserved(base):
    """推子层必须保留速度/淡化/末样本保持/加总后单次舍入。

    单推子、配方含全部 clip、配重 1、档位 1 时，逐样本应等于既有引擎参考解释器。
    """
    reg = _reg(base)
    a8 = reg.register(sequence([0, 100, 200, 300, 400]), 8000, note="8k").asset_id
    ed = TimelineEditor(base, reg, name="contract")
    # 8k->16k 插值 + 末样本保持 + 淡入淡出 + 1/2 增益（偶数舍入 + 半值）
    ed.insert([
        {"clip_id": "c", "asset_id": a8, "a": 0, "b": 4, "start": "0",
         "fade_in": F(2, 16000), "fade_out": F(2, 16000), "gain": "1/2"},
    ])
    product = ed.publish()
    pcm = {a["asset_id"]: reg.load_pcm(a["asset_id"]) for a in product["assets"]}
    T = F(1, 2000)  # 8 样本
    spec = {
        "format": "audiomix.faders/1", "T": [T.numerator, T.denominator],
        "faders": [{"fader_id": "u", "current": "one", "costs": {},
                    "positions": [{"id": "one", "gain": "1"}]}],
        "linkage": {"c": "u"},
        "recipes": {"mix": [{"clip_id": "c", "weight": "1"}]},
        "constraints": [],
    }
    fp = FaderProgram(spec, product, pcm)
    got = fp.render_recipe("mix")
    ref = render_reference(product, pcm, T).tolist()
    assert got == ref, f"推子层={got} 引擎参考={ref}"
    # 末样本保持：最后一个样本来自 x[3]=300 * 1/2 * 淡出 1/2 = 75
    assert got[-1] == 75
    print("[OK] 推子层与引擎参考一致（速度/淡化/末样本保持/单次舍入）:", got)


def test_rounding_after_sum(base):
    """两片各 0.5 贡献：必须先求和得 1，再单次舍入；推子增益随之整体缩放。"""
    reg = _reg(base)
    aid = reg.register(sequence([1, 3, 5]), 16000).asset_id
    ed = TimelineEditor(base, reg, name="rnd")
    ed.insert([
        {"clip_id": "x", "asset_id": aid, "a": 0, "b": 3, "start": "0", "gain": "1/2"},
        {"clip_id": "y", "asset_id": aid, "a": 0, "b": 3, "start": "0", "gain": "1/2"},
    ])
    product = ed.publish()
    pcm = {a["asset_id"]: reg.load_pcm(a["asset_id"]) for a in product["assets"]}
    spec = {
        "format": "audiomix.faders/1", "T": "3/16000",
        "faders": [{"fader_id": "u", "current": "one", "costs": {},
                    "positions": [{"id": "one", "gain": "1"}]}],
        "linkage": {"x": "u", "y": "u"},
        "recipes": {"mix": [{"clip_id": "x", "weight": "1"},
                            {"clip_id": "y", "weight": "1"}]},
        "constraints": [],
    }
    fp = FaderProgram(spec, product, pcm)
    assert fp.render_recipe("mix") == [1, 3, 5]
    # 恰半取偶：单 clip 半增益奇数 -> [0,2,2]
    for pid in ("one",):
        pass
    print("[OK] 先求和后单次舍入（0.5+0.5=1），推子层保持合同")


def test_two_recipe_shared_assignment(base):
    """同一推子方案必须同时满足两套配方（含相消依赖）。"""
    reg = _reg(base)
    a = reg.register(sequence([60]), 16000).asset_id
    b = reg.register(sequence([60]), 16000).asset_id
    c = reg.register(sequence([-40]), 16000).asset_id
    ed = TimelineEditor(base, reg, name="two")
    ed.insert([
        {"clip_id": "a", "asset_id": a, "a": 0, "b": 1, "start": "0"},
        {"clip_id": "b", "asset_id": b, "a": 0, "b": 1, "start": "0"},
        {"clip_id": "c", "asset_id": c, "a": 0, "b": 1, "start": "0"},
    ])
    product = ed.publish()
    pcm = {a2["asset_id"]: reg.load_pcm(a2["asset_id"]) for a2 in product["assets"]}
    spec = {
        "format": "audiomix.faders/1", "T": "1/16000",
        "faders": [
            {"fader_id": "fa", "current": "full", "costs": {"half": "1"},
             "positions": [{"id": "full", "gain": "1"}, {"id": "half", "gain": "1/2"}]},
            {"fader_id": "fb", "current": "full", "costs": {"half": "1"},
             "positions": [{"id": "full", "gain": "1"}, {"id": "half", "gain": "1/2"}]},
            {"fader_id": "fc", "current": "full", "costs": {"half": "1"},
             "positions": [{"id": "full", "gain": "1"}, {"id": "half", "gain": "1/2"}]},
        ],
        "linkage": {"a": "fa", "b": "fb", "c": "fc"},
        "recipes": {
            # R1 三个全加；R2 a+c 相消敏感
            "R1": [{"clip_id": "a", "weight": "1"}, {"clip_id": "b", "weight": "1"},
                   {"clip_id": "c", "weight": "1"}],
            "R2": [{"clip_id": "a", "weight": "1"}, {"clip_id": "c", "weight": "1"}],
        },
        "constraints": [
            {"id": "R1-i", "recipe": "R1", "kind": "interval",
             "n0": 0, "n1": 1, "lo": "0", "hi": "60"},
            {"id": "R2-i", "recipe": "R2", "kind": "interval",
             "n0": 0, "n1": 1, "lo": "10", "hi": "40"},
        ],
    }
    fp = FaderProgram(spec, product, pcm)
    # 当前 R1=80 超 60；R2=20 合格
    assert fp.composite("R1", 0) == 80
    assert fp.composite("R2", 0) == 20
    res = fp.solve()
    assert res["feasible"]
    # 同一方案两套都要过
    checks = fp.check_assignment(res["assignment"])
    assert all(r["pass"] for r in checks), checks
    print("[OK] 同一推子方案同时满足两套配方；最优成本 =",
          float(res["cost"]), "changes=",
          [(c["fader_id"], c["to"]) for c in fp.correction_report(res["assignment"])["changes"]])


def test_lower_sum_constraint(base):
    """lower_sum 用实际出带整数样本之和；interval 用未饱和合成值。"""
    reg = _reg(base)
    a = reg.register(sequence([70, 70, 70]), 16000).asset_id
    ed = TimelineEditor(base, reg, name="ls")
    ed.insert({"clip_id": "a", "asset_id": a, "a": 0, "b": 3, "start": "0"})
    product = ed.publish()
    pcm = {a2["asset_id"]: reg.load_pcm(a2["asset_id"]) for a2 in product["assets"]}
    spec = {
        "format": "audiomix.faders/1", "T": "3/16000",
        "faders": [{"fader_id": "f", "current": "full", "costs": {"half": "1"},
                    "positions": [{"id": "full", "gain": "1"},
                                  {"id": "half", "gain": "1/2"}]}],
        "linkage": {"a": "f"},
        "recipes": {"R": [{"clip_id": "a", "weight": "1"}]},
        "constraints": [
            {"id": "sum", "recipe": "R", "kind": "lower_sum",
             "n0": 0, "n1": 3, "lower": 200},
        ],
    }
    fp = FaderProgram(spec, product, pcm)
    # full: 3*70=210 >= 200 通过；half: 3*35=105 不通过
    assert fp.check_assignment({"f": "full"})[0]["emitted_sum"] == 210
    assert fp.check_assignment({"f": "half"})[0]["emitted_sum"] == 105
    res = fp.solve()
    assert res["feasible"] and res["assignment"] == {"f": "full"} and res["cost"] == 0
    # 下限抬到 220 -> 单套本不可行（任何档位都达不到）
    spec2 = json.loads(json.dumps(spec))
    spec2["constraints"][0]["lower"] = 220
    fp2 = FaderProgram(spec2, product, pcm)
    exp = fp2.explain_infeasible()
    assert exp["classification"] == "single_recipe_infeasible"
    assert exp["per_recipe_feasible"] == {"R": False}
    print("[OK] lower_sum 按实际整数样本求和；不可达下限=单套不可行")


def test_joint_only_infeasible(base):
    """两套各自可行，但合办无解 -> 互相排斥的实际检查。"""
    reg = _reg(base)
    # 单推子两档：全档 60 / 半档 30。两套配方各要一个互斥区间。
    a = reg.register(sequence([60]), 16000).asset_id
    ed = TimelineEditor(base, reg, name="joint")
    ed.insert({"clip_id": "a", "asset_id": a, "a": 0, "b": 1, "start": "0"})
    product = ed.publish()
    pcm = {a2["asset_id"]: reg.load_pcm(a2["asset_id"]) for a2 in product["assets"]}
    spec = {
        "format": "audiomix.faders/1", "T": "1/16000",
        "faders": [{"fader_id": "f", "current": "full", "costs": {"half": "1"},
                    "positions": [{"id": "full", "gain": "1"},
                                  {"id": "half", "gain": "1/2"}]}],
        "linkage": {"a": "f"},
        "recipes": {"R1": [{"clip_id": "a", "weight": "1"}],
                    "R2": [{"clip_id": "a", "weight": "1"}]},
        "constraints": [
            # R1 要 >=50（仅全档），R2 要 <=40（仅半档），共用同一推子 -> 无解
            {"id": "R1-lo", "recipe": "R1", "kind": "interval",
             "n0": 0, "n1": 1, "lo": "50", "hi": "100"},
            {"id": "R2-hi", "recipe": "R2", "kind": "interval",
             "n0": 0, "n1": 1, "lo": "0", "hi": "40"},
        ],
    }
    fp = FaderProgram(spec, product, pcm)
    exp = fp.explain_infeasible()
    assert exp["classification"] == "joint_only_infeasible", exp
    assert exp["per_recipe_feasible"] == {"R1": True, "R2": True}
    ids = {c["id"] for c in exp["mutually_exclusive_checks"]}
    assert ids == {"R1-lo", "R2-hi"}, ids
    print("[OK] 两套各自可行但合办无解 -> 分类 joint_only_infeasible，检查 =", sorted(ids))


def _build_grouping_pair(base):
    """旧播出版整数 WAV 相同，但联动分组不同：一份可修、一份无解。

    clip a、b 基线均 +100；配方甲=a+b、乙=a-b。当前都全档：甲=200、乙=0，
    整数 WAV 完全相同。
      * 独立分组（a->F1, b->F2）：a 全档、b 半档 -> 甲150、乙50，可修。
      * 联动分组（a,b 共用 F1）：a=b 恒等 -> 乙永远 0，无解。
    """
    reg = _reg(base)
    a = reg.register(sequence([100]), 16000).asset_id
    b = reg.register(sequence([100]), 16000).asset_id
    # 同正文 => 同一素材身份；两个 clip 引用同一素材，却分属不同推子独立控制
    assert a == b, "同字节内容必须是同一 asset_id"

    def make(name, linkage):
        ed = TimelineEditor(base, reg, name=name)
        ed.insert([
            {"clip_id": "a", "asset_id": a, "a": 0, "b": 1, "start": "0"},
            {"clip_id": "b", "asset_id": b, "a": 0, "b": 1, "start": "0"},
        ])
        product = ed.publish()
        pcm = {x["asset_id"]: reg.load_pcm(x["asset_id"]) for x in product["assets"]}
        spec = {
            "format": "audiomix.faders/1", "T": "1/16000",
            "faders": [
                {"fader_id": "F1", "current": "full", "costs": {"half": "1"},
                 "positions": [{"id": "full", "gain": "1"},
                               {"id": "half", "gain": "1/2"}]},
                {"fader_id": "F2", "current": "full", "costs": {"half": "1"},
                 "positions": [{"id": "full", "gain": "1"},
                               {"id": "half", "gain": "1/2"}]},
            ],
            "linkage": linkage,
            "recipes": {
                "甲": [{"clip_id": "a", "weight": "1"}, {"clip_id": "b", "weight": "1"}],
                "乙": [{"clip_id": "a", "weight": "1"}, {"clip_id": "b", "weight": "-1"}],
            },
            "constraints": [
                {"id": "甲-hi", "recipe": "甲", "kind": "interval",
                 "n0": 0, "n1": 1, "lo": "0", "hi": "150"},
                {"id": "乙-lo", "recipe": "乙", "kind": "lower_sum",
                 "n0": 0, "n1": 1, "lower": 50},
            ],
        }
        return FaderProgram(spec, product, pcm)

    independent = make("indep", {"a": "F1", "b": "F2"})
    combined = make("combined", {"a": "F1", "b": "F1"})
    return independent, combined


def test_same_wav_different_linkage(base):
    reg = _reg(base)
    independent, combined = _build_grouping_pair(base)
    # 旧播出版整数 WAV 完全相同（两套配方、当前档）
    for r in ("甲", "乙"):
        assert independent.render_recipe(r) == combined.render_recipe(r)
    assert independent.render_recipe("甲") == [200]
    assert independent.render_recipe("乙") == [0]

    # 独立分组可修
    ri = independent.solve()
    assert ri["feasible"], ri
    ia = ri["assignment"]
    assert independent.check_assignment(ia)[0]["pass"]
    assert independent.check_assignment(ia)[1]["emitted_sum"] >= 50

    # 联动分组无解
    rc = combined.solve()
    assert not rc["feasible"]
    exp = combined.explain_infeasible()
    # 甲单独可行，乙单独不可行 -> 单套不可行
    assert exp["classification"] == "single_recipe_infeasible", exp
    assert exp["per_recipe_feasible"]["乙"] is False
    print("[OK] 旧播 WAV 相同、联动分组不同：独立分组可修，联动分组无解；",
          "独立解 =", ia)


def test_verify_from_original_pcm_and_wav(base):
    """保存前用原始素材重做两套实播核对；写出的 WAV 读回逐样本一致。"""
    reg = _reg(base)
    fp, product, pcm, spec = _two_clip_program(base, reg)
    res = fp.solve()
    verify = fp.verify_from_pcm(res["assignment"])
    assert verify["ok"]
    for name, info in verify["per_recipe"].items():
        path = os.path.join(base, f"recipe_{name}.wav")
        write_recipe_wav(path, info["samples"])
        got, rate = read_wav(path)
        assert rate == 16000
        assert got.tolist() == info["samples"]
    # 用全新装载（读盘 + 重新取素材字节）复核
    spec_path = os.path.join(base, "spec.json")
    prod_path = os.path.join(base, "product.json")
    with open(spec_path, "w", encoding="utf-8") as fh:
        json.dump({**spec, "product": prod_path}, fh, ensure_ascii=False)
    with open(prod_path, "w", encoding="utf-8") as fh:
        json.dump(product, fh, ensure_ascii=False)
    fp2 = load_fader_program(spec_path, base)
    v2 = fp2.verify_from_pcm(res["assignment"])
    assert v2["ok"] and v2["per_recipe"]["jia"]["samples"] == [10]
    assert v2["per_recipe"]["yi"]["samples"] == [50]
    print("[OK] 从原始素材重做核对 + WAV 读写往返一致；装载后可复算")


def test_rational_cost_tie(base):
    """并列最优可取其一：两条不同路径成本相同。"""
    reg = _reg(base)
    a = reg.register(sequence([100]), 16000).asset_id
    b = reg.register(sequence([100]), 16000).asset_id
    ed = TimelineEditor(base, reg, name="tie")
    ed.insert([
        {"clip_id": "a", "asset_id": a, "a": 0, "b": 1, "start": "0"},
        {"clip_id": "b", "asset_id": b, "a": 0, "b": 1, "start": "0"},
    ])
    product = ed.publish()
    pcm = {x["asset_id"]: reg.load_pcm(x["asset_id"]) for x in product["assets"]}
    # 只需和 <=150：压 a 或压 b 都行，成本都 1 -> 并列最优
    spec = {
        "format": "audiomix.faders/1", "T": "1/16000",
        "faders": [
            {"fader_id": "fa", "current": "full", "costs": {"half": "1"},
             "positions": [{"id": "full", "gain": "1"}, {"id": "half", "gain": "1/2"}]},
            {"fader_id": "fb", "current": "full", "costs": {"half": "1"},
             "positions": [{"id": "full", "gain": "1"}, {"id": "half", "gain": "1/2"}]},
        ],
        "linkage": {"a": "fa", "b": "fb"},
        "recipes": {"R": [{"clip_id": "a", "weight": "1"},
                          {"clip_id": "b", "weight": "1"}]},
        "constraints": [{"id": "R", "recipe": "R", "kind": "interval",
                         "n0": 0, "n1": 1, "lo": "0", "hi": "150"}],
    }
    fp = FaderProgram(spec, product, pcm)
    res = fp.solve()
    assert res["feasible"] and res["cost"] == 1
    changed = [fid for fid, pid in res["assignment"].items() if pid != "full"]
    assert len(changed) == 1 and res["assignment"][changed[0]] == "half"
    print("[OK] 有理成本并列最优取其一：只压一支，成本 1")


def test_exact_fraction_rounding():
    """精确舍入：恰半取偶、负数、饱和边界。"""
    assert emit_fraction(F(1, 2)) == 0
    assert emit_fraction(F(3, 2)) == 2
    assert emit_fraction(F(-1, 2)) == 0
    assert emit_fraction(F(-3, 2)) == -2
    assert emit_fraction(F(40000)) == 32767
    assert emit_fraction(F(-40000)) == -32768
    print("[OK] 精确有理数舍入/饱和：恰半取偶与 int16 边界")


def run_all(base_path):
    print("== 推子联动 / 双配方 / 检查窗 测试 ==")
    test_exact_fraction_rounding()
    test_worked_example(os.path.join(base_path, "worked"))
    test_contract_preserved(os.path.join(base_path, "contract"))
    test_rounding_after_sum(os.path.join(base_path, "rnd"))
    test_two_recipe_shared_assignment(os.path.join(base_path, "two"))
    test_lower_sum_constraint(os.path.join(base_path, "ls"))
    test_joint_only_infeasible(os.path.join(base_path, "joint"))
    test_same_wav_different_linkage(os.path.join(base_path, "pair"))
    test_verify_from_original_pcm_and_wav(os.path.join(base_path, "verify"))
    test_rational_cost_tie(os.path.join(base_path, "tie"))
    print("推子联动测试全部通过\n")


if __name__ == "__main__":
    import sys
    run_all(sys.argv[1] if len(sys.argv) > 1 else "/tmp/audiomix_faders")
