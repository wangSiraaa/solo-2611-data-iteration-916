"""推子联动 / 双播出配方演示：手算示例 + 联动分组对照 + 不可行分类。

全部使用本地生成的短 PCM 素材，沿用既有的普通混音客户
（AssetRegistry / TimelineEditor / publish）产生发布产品；随后在该产品之上
登记推子联动关系与两套单声道播出配方，演示：

1. 题面手算示例：甲含两支推子 (+100,-80)、乙只含第一支 (+100)；
   甲要求 [0,30]、乙 <=60 -> 两支都降半档（甲 10、乙 50）为最少人工成本 2。
   仅压第一支会令甲变 -30；若把乙上限放宽到 110，则维持原档成本 0 最优。
2. 舍入/饱和前各推子对左右两套配方的独立贡献（逐样本精确有理数）。
3. 旧播出版整数 WAV 完全相同、但联动分组不同的一对编辑产品：
   独立分组可修、共用推子分组无解。
4. 不可行时区分单套本就不可行 vs 两套合办才无解，给出互相排斥的实际检查。
5. 保存前用原始素材按所选档位重做两套实播核对，并写出两份真实 WAV。
"""
import json
import os
import shutil
import sys

from fractions import Fraction as F

from audiomix.assets import AssetRegistry, read_wav, sequence
from audiomix.faders import FaderProgram, write_recipe_wav
from audiomix.timeline import TimelineEditor

BASE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/audiomix_fader_demo"


def _two_clip(base, reg):
    a_plus = reg.register(sequence([100]), 16000, note="+100").asset_id
    a_minus = reg.register(sequence([-80]), 16000, note="-80").asset_id
    ed = TimelineEditor(base, reg, name="demo")
    ed.insert([
        {"clip_id": "p", "asset_id": a_plus, "a": 0, "b": 1, "start": "0"},
        {"clip_id": "n", "asset_id": a_minus, "a": 0, "b": 1, "start": "0"},
    ])
    product = ed.publish()
    pcm = {a["asset_id"]: reg.load_pcm(a["asset_id"]) for a in product["assets"]}
    faders = [
        {"fader_id": "f1", "current": "full", "costs": {"half": "1"},
         "positions": [{"id": "full", "gain": "1"}, {"id": "half", "gain": "1/2"}]},
        {"fader_id": "f2", "current": "full", "costs": {"half": "1"},
         "positions": [{"id": "full", "gain": "1"}, {"id": "half", "gain": "1/2"}]},
    ]
    base_spec = {
        "format": "audiomix.faders/1", "T": "1/16000",
        "faders": faders, "linkage": {"p": "f1", "n": "f2"},
        "recipes": {
            "甲": [{"clip_id": "p", "weight": "1"}, {"clip_id": "n", "weight": "1"}],
            "乙": [{"clip_id": "p", "weight": "1"}],
        },
        "constraints": [
            {"id": "甲-区间", "recipe": "甲", "kind": "interval",
             "n0": 0, "n1": 1, "lo": "0", "hi": "30"},
            {"id": "乙-上限", "recipe": "乙", "kind": "interval",
             "n0": 0, "n1": 1, "hi": "60"},
        ],
    }
    return product, pcm, base_spec


def main() -> None:
    shutil.rmtree(BASE, ignore_errors=True)
    os.makedirs(BASE, exist_ok=True)
    reg = AssetRegistry(os.path.join(BASE, "registry"))
    product, pcm, spec = _two_clip(os.path.join(BASE, "registry"), reg)

    print("== 1. 舍入/饱和前，各推子对两套配方的独立贡献（当前档） ==")
    fp = FaderProgram(spec, product, pcm)
    for recipe in ("甲", "乙"):
        c = fp.window_contributions(recipe, 0, 1)
        print(f"  {recipe}: 推子贡献={c['faders']}  合成={c['composite']}  "
              f"出带整数={c['emitted']}")

    print("\n== 2. 最优校正（甲∈[0,30]、乙≤60） ==")
    res = fp.solve()
    print(f"  可行={res['feasible']} 成本={res['cost']} "
          f"方案={res['assignment']}")
    print(f"  半档/半档 -> 甲={fp.composite('甲', 0, {'f1': 'half', 'f2': 'half'})}, "
          f"乙={fp.composite('乙', 0, {'f1': 'half', 'f2': 'half'})}")
    print(f"  仅压第一支 -> 甲={fp.composite('甲', 0, {'f1': 'half'})}")

    print("\n== 3. 乙上限放宽到 110：维持原档成本 0 ==")
    relaxed = json.loads(json.dumps(spec))
    relaxed["constraints"][1]["hi"] = "110"
    res0 = FaderProgram(relaxed, product, pcm).solve()
    print(f"  成本={res0['cost']} 方案={res0['assignment']}")

    print("\n== 4. 保存前用原始素材重做两套实播核对 + 写两份真实 WAV ==")
    verify = fp.verify_from_pcm(res["assignment"])
    wav = {}
    for name, info in verify["per_recipe"].items():
        path = os.path.join(BASE, f"recipe_{name}.wav")
        write_recipe_wav(path, info["samples"])
        wav[name] = path
        got, rate = read_wav(path)
        print(f"  {name}.wav: {rate} Hz, 样本={got.tolist()}")
    assert verify["ok"]
    correction = fp.correction_report(res["assignment"], wav_paths=wav, verified=True)
    with open(os.path.join(BASE, "correction.json"), "w", encoding="utf-8") as fh:
        json.dump(correction, fh, ensure_ascii=False, indent=2)
    print(f"  校正单: {os.path.join(BASE, 'correction.json')}")

    # ---- 5. 相同整数 WAV、不同联动分组：一份可修、一份无解 ----
    print("\n== 5. 旧播整数 WAV 相同、联动分组不同 ==")
    reg2 = AssetRegistry(os.path.join(BASE, "pair_registry"))
    a = reg2.register(sequence([100]), 16000).asset_id
    b = reg2.register(sequence([100]), 16000).asset_id

    def grouping(name, linkage):
        ed = TimelineEditor(os.path.join(BASE, "pair_registry"), reg2, name=name)
        ed.insert([
            {"clip_id": "a", "asset_id": a, "a": 0, "b": 1, "start": "0"},
            {"clip_id": "b", "asset_id": b, "a": 0, "b": 1, "start": "0"},
        ])
        prod = ed.publish()
        pc = {x["asset_id"]: reg2.load_pcm(x["asset_id"]) for x in prod["assets"]}
        sp = {
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
                {"id": "甲-上限", "recipe": "甲", "kind": "interval",
                 "n0": 0, "n1": 1, "lo": "0", "hi": "150"},
                {"id": "乙-下限", "recipe": "乙", "kind": "lower_sum",
                 "n0": 0, "n1": 1, "lower": 50},
            ],
        }
        return FaderProgram(sp, prod, pc)

    indep = grouping("indep", {"a": "F1", "b": "F2"})
    comb = grouping("comb", {"a": "F1", "b": "F1"})
    same = [indep.render_recipe(r) == comb.render_recipe(r) for r in ("甲", "乙")]
    print(f"  两份当前整数 WAV 相同: 甲={indep.render_recipe('甲')} "
          f"乙={indep.render_recipe('乙')}  (逐配方一致={same})")
    ri = indep.solve()
    print(f"  独立分组可修: 可行={ri['feasible']} 解={ri['assignment']}")
    rc = comb.solve()
    exp = comb.explain_infeasible()
    print(f"  共用推子分组无解: 分类={exp['classification']} "
          f"各配方单独可行={exp['per_recipe_feasible']}")
    print(f"  互相排斥的实际检查: {[c['id'] for c in exp['mutually_exclusive_checks']]}")

    # ---- 6. 两套各自可行但合办无解 ----
    print("\n== 6. 两套各自可行、合办无解（互相排斥的检查） ==")
    reg3 = AssetRegistry(os.path.join(BASE, "joint_registry"))
    aid = reg3.register(sequence([60]), 16000).asset_id
    ed = TimelineEditor(os.path.join(BASE, "joint_registry"), reg3, name="joint")
    ed.insert({"clip_id": "a", "asset_id": aid, "a": 0, "b": 1, "start": "0"})
    prod = ed.publish()
    pc = {x["asset_id"]: reg3.load_pcm(x["asset_id"]) for x in prod["assets"]}
    sp = {
        "format": "audiomix.faders/1", "T": "1/16000",
        "faders": [{"fader_id": "f", "current": "full", "costs": {"half": "1"},
                    "positions": [{"id": "full", "gain": "1"},
                                  {"id": "half", "gain": "1/2"}]}],
        "linkage": {"a": "f"},
        "recipes": {"甲": [{"clip_id": "a", "weight": "1"}],
                    "乙": [{"clip_id": "a", "weight": "1"}]},
        "constraints": [
            {"id": "甲-要≥50", "recipe": "甲", "kind": "interval",
             "n0": 0, "n1": 1, "lo": "50", "hi": "100"},
            {"id": "乙-要≤40", "recipe": "乙", "kind": "interval",
             "n0": 0, "n1": 1, "lo": "0", "hi": "40"},
        ],
    }
    fpj = FaderProgram(sp, prod, pc)
    exp = fpj.explain_infeasible()
    print(f"  分类={exp['classification']} 各配方单独可行={exp['per_recipe_feasible']}")
    print(f"  互相排斥的检查: {[c['id'] for c in exp['mutually_exclusive_checks']]}")
    print(f"  reason: {exp['reason']}")

    print(f"\n全部演示完成。输出目录: {BASE}")


if __name__ == "__main__":
    main()
