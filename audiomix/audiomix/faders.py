"""推子联动 / 双播出配方 / 检查窗约束 / 最优校正单。

在既有 audiomix 发布产品之上增加一层“稳定 clip 联动关系”：

- **推子（fader）** 有有限个精确有理增益档（position）；同一推子驱动的所有
  clip 必须共用同一档（“若两次出现共用一个推子，则必须选同一档”）。
  同一素材可由不同推子独立控制——联动关系登记在 *clip* 上，不登记在素材上。
- **两套（或更多）单声道播出配方**：每套配方列出参与的 clip 与有理配重；
  某推子对某配方第 n 个输出样本的“未舍入、未饱和”贡献为
  ``gain(position) * Σ weight * base_clip(n)``，其中 base 已含原有速度/淡化/
  增益/末样本保持语义。两套配方可由同一批推子 / clip 以不同配重复用。
- **检查窗**：``interval`` 要求未饱和合成值逐样本落在闭区间；
  ``lower_sum`` 要求实际出带整数样本之和 ≥ 下限。同一推子档位方案必须全过。
- **最优校正单**：在全部检查窗下最小化“从当前档调过去”的人工成本总和；
  并列最优取任一。找不到可行方案时，区分整套/单套不可行并给出互相排斥的
  实际检查。

所有增益、配重、区间端点、成本均为精确 ``Fraction``；合成值以有理数精确
相加，最后才按合同做一次“恰半取偶”舍入并饱和到 int16。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable

from .engine import (
    OUTPUT_RATE, output_sample_count, product_clip_view, validate_product,
)
from .rationals import as_r, dump_r

INT16_MIN = -32768
INT16_MAX = 32767

FADER_FORMAT = "audiomix.faders/1"


class FaderError(ValueError):
    """联动关系 / 配方 / 约束 / 档位方案非法。"""


# ============================ 精确舍入与饱和 ============================ #

def round_half_even_scaled(num: int, den: int) -> int:
    """把 num/den 按“恰半取偶”舍入到整数（den > 0，支持负分子）。"""
    if den <= 0:
        raise ValueError("分母必须为正")
    q, r = divmod(num, den)          # q = floor, 0 <= r < den
    twice = 2 * r
    if twice < den:
        return q
    if twice > den:
        return q + 1
    return q if q % 2 == 0 else q + 1


def emit_scaled(total_scaled: int, scale: int) -> int:
    """精确合成值（total_scaled/scale）-> 实际出带整数样本。"""
    return max(INT16_MIN, min(INT16_MAX,
                              round_half_even_scaled(total_scaled, scale)))


def emit_fraction(total: Fraction) -> int:
    return max(INT16_MIN, min(INT16_MAX,
                              round_half_even_scaled(total.numerator,
                                                     total.denominator)))


def _ceil_frac(f: Fraction) -> int:
    return -((-f.numerator) // f.denominator)


def _floor_frac(f: Fraction) -> int:
    return f.numerator // f.denominator


# ================================ 数据模型 ================================ #

@dataclass(frozen=True)
class Fader:
    fader_id: str
    positions: tuple[tuple[str, Fraction], ...]   # (position_id, gain)
    current: str
    costs: dict[str, Fraction]                     # 非当前档 -> 人工成本

    def position_ids(self) -> list[str]:
        return [p for p, _ in self.positions]

    def gain(self, pid: str) -> Fraction:
        for p, g in self.positions:
            if p == pid:
                return g
        raise FaderError(f"推子 {self.fader_id} 无档位 {pid}")

    def cost_to(self, pid: str) -> Fraction:
        if pid == self.current:
            return Fraction(0)
        if pid not in self.costs:
            raise FaderError(f"推子 {self.fader_id} 缺少到档位 {pid} 的人工成本")
        return self.costs[pid]

    def min_cost(self) -> Fraction:
        return min(self.cost_to(p) for p in self.position_ids())


@dataclass(frozen=True)
class Constraint:
    cid: str
    recipe: str
    kind: str                       # "interval" | "lower_sum"
    n0: int
    n1: int
    lo: Fraction | None = None      # interval 下界（未饱和合成值）
    hi: Fraction | None = None      # interval 上界
    lower: int | None = None        # lower_sum 实际整数样本之和下限

    def describe(self) -> dict[str, Any]:
        d = {"id": self.cid, "recipe": self.recipe, "kind": self.kind,
             "window": [self.n0, self.n1]}
        if self.kind == "interval":
            d["lo"] = None if self.lo is None else dump_r(self.lo)
            d["hi"] = None if self.hi is None else dump_r(self.hi)
        else:
            d["lower"] = self.lower
        return d


def _parse_fader(obj: dict[str, Any]) -> Fader:
    fid = str(obj["fader_id"])
    positions = []
    seen_ids, seen_gains = set(), set()
    for p in obj["positions"]:
        pid = str(p["id"])
        gain = as_r(p["gain"])
        if pid in seen_ids:
            raise FaderError(f"推子 {fid} 档位 id 重复: {pid}")
        if gain in seen_gains:
            raise FaderError(f"推子 {fid} 档位增益重复: {gain}")
        seen_ids.add(pid)
        seen_gains.add(gain)
        positions.append((pid, gain))
    if not positions:
        raise FaderError(f"推子 {fid} 没有档位")
    current = str(obj["current"])
    if current not in seen_ids:
        raise FaderError(f"推子 {fid} 当前档 {current} 不在档位表中")
    costs: dict[str, Fraction] = {}
    for pid, raw in (obj.get("costs") or {}).items():
        if pid not in seen_ids:
            raise FaderError(f"推子 {fid} 成本指向未知档位 {pid}")
        c = as_r(raw)
        if c < 0:
            raise FaderError(f"推子 {fid} 到 {pid} 的人工成本不能为负")
        if pid != current:
            costs[pid] = c
    for pid in seen_ids:
        if pid != current and pid not in costs:
            raise FaderError(f"推子 {fid} 缺少到档位 {pid} 的人工成本")
    return Fader(fid, tuple(positions), current, costs)


def _parse_constraint(obj: dict[str, Any]) -> Constraint:
    cid = str(obj["id"])
    recipe = str(obj["recipe"])
    kind = str(obj["kind"])
    n0, n1 = int(obj["n0"]), int(obj["n1"])
    if kind == "interval":
        lo = None if obj.get("lo") is None else as_r(obj["lo"])
        hi = None if obj.get("hi") is None else as_r(obj["hi"])
        if lo is None and hi is None:
            raise FaderError(f"检查 {cid}（interval）至少需要 lo 或 hi")
        if lo is not None and hi is not None and lo > hi:
            raise FaderError(f"检查 {cid} 区间下界大于上界")
        return Constraint(cid, recipe, kind, n0, n1, lo=lo, hi=hi)
    if kind == "lower_sum":
        lower = int(obj["lower"])
        return Constraint(cid, recipe, kind, n0, n1, lower=lower)
    raise FaderError(f"检查 {cid} 类型未知: {kind}")


# ============================ 精确基贡献计算 ============================ #

def _clip_base(view_clip: dict[str, Any], x, n: int) -> Fraction:
    """clip 在第 n 个输出样本上的精确基线贡献（含速度/淡化/gain/末样本保持）。

    “基线”不含推子增益；推子档位作为额外乘子叠加，因此同一推子可整体缩放
    其驱动的全部 clip。采样位置/插值/淡化边界语义与 engine 参考解释器一致。
    """
    t = Fraction(n, OUTPUT_RATE)
    start, end = view_clip["start"], view_clip["end"]
    if not (start <= t < end):
        return Fraction(0)

    a, b = view_clip["a"], view_clip["b"]
    p = a + (t - start) * view_clip["rate"] * view_clip["speed"]
    i = p.numerator // p.denominator           # p >= a >= 0 -> floor
    if i >= b - 1:                             # 末样本保持，绝不越界
        value = Fraction(int(x[b - 1]))
    else:
        frac = p - i
        x0 = int(x[i])
        x1 = int(x[i + 1])
        value = Fraction(x0) + frac * Fraction(x1 - x0)

    # 淡化：0 时长 -> 因子恒 1；边界用严格小于
    fin = Fraction(1)
    if view_clip["fade_in"] > 0 and (t - start) < view_clip["fade_in"]:
        fin = (t - start) / view_clip["fade_in"]
    fout = Fraction(1)
    if view_clip["fade_out"] > 0:
        remain = end - t
        if remain < view_clip["fade_out"]:
            fout = remain / view_clip["fade_out"]
    return value * view_clip["gain"] * fin * fout


# ================================ 主体 ================================ #

class FaderProgram:
    """一套“发布产品 + 推子联动 + 播出配方 + 检查窗”的可求解模型。"""

    def __init__(self, spec: dict[str, Any], product: dict[str, Any],
                 pcm_by_asset: dict[str, Any], spec_dir: str = "."):
        if spec.get("format") != FADER_FORMAT:
            raise FaderError(f"联动产品 format 必须为 {FADER_FORMAT}")
        validate_product(product, pcm_by_asset)
        self.raw_spec = spec
        self.product = product
        self.pcm = pcm_by_asset
        self.spec_dir = spec_dir

        self.faders: dict[str, Fader] = {}
        for obj in spec.get("faders", []):
            f = _parse_fader(obj)
            if f.fader_id in self.faders:
                raise FaderError(f"推子 id 重复: {f.fader_id}")
            self.faders[f.fader_id] = f

        # 联动关系：clip_id -> fader_id
        self.linkage: dict[str, str] = {}
        for cid, fid in (spec.get("linkage") or {}).items():
            if fid not in self.faders:
                raise FaderError(f"clip {cid} 联动到未知推子 {fid}")
            self.linkage[str(cid)] = str(fid)

        self.recipes: dict[str, list[tuple[str, Fraction]]] = {}
        for name, items in (spec.get("recipes") or {}).items():
            entries = []
            seen = set()
            for entry in items:
                cid = str(entry["clip_id"])
                if cid in seen:
                    raise FaderError(f"配方 {name} 重复列出 clip {cid}")
                seen.add(cid)
                w = as_r(entry["weight"])
                entries.append((cid, w))
            self.recipes[str(name)] = entries
        if not self.recipes:
            raise FaderError("至少需要一套播出配方")

        self.constraints = [_parse_constraint(o) for o in spec.get("constraints", [])]
        if len({c.cid for c in self.constraints}) != len(self.constraints):
            raise FaderError("检查 id 重复")

        # 时长 T：产品或显式
        self.T = as_r(spec["T"]) if spec.get("T") is not None else self._default_T()
        if self.T <= 0:
            raise FaderError("T 必须为正")
        self.N = output_sample_count(self.T)

        self.views = {v["clip_id"]: v for v in product_clip_view(product)}
        self._base_cache: dict[tuple[str, int], Fraction] = {}
        self._validate_relations()
        self.linkage_digest = self._linkage_digest()

    # ---------------------------- 校验 ---------------------------- #
    def _default_T(self) -> Fraction:
        view = product_clip_view(self.product)
        return max((c["end"] for c in view), default=Fraction(0))

    def _validate_relations(self) -> None:
        clip_ids = set(self.views)
        for name, entries in self.recipes.items():
            for cid, _ in entries:
                if cid not in clip_ids:
                    raise FaderError(f"配方 {name} 引用未知 clip {cid}")
                if cid not in self.linkage:
                    raise FaderError(f"配方 {name} 的 clip {cid} 未登记联动推子")
        for cid, fid in self.linkage.items():
            if cid not in clip_ids:
                raise FaderError(f"联动关系引用未知 clip {cid}")
            if fid not in self.faders:
                raise FaderError(f"clip {cid} 联动到未知推子 {fid}")
        for c in self.constraints:
            if c.recipe not in self.recipes:
                raise FaderError(f"检查 {c.cid} 引用未知配方 {c.recipe}")
            if not (0 <= c.n0 < c.n1 <= self.N):
                raise FaderError(
                    f"检查 {c.cid} 窗口 [{c.n0},{c.n1}) 超出输出样本数 {self.N}")

    def _linkage_digest(self) -> str:
        canonical = {
            "faders": {fid: {
                "positions": [[p, dump_r(g)] for p, g in f.positions],
                "current": f.current,
                "costs": {p: dump_r(v) for p, v in sorted(f.costs.items())},
            } for fid, f in sorted(self.faders.items())},
            "linkage": dict(sorted(self.linkage.items())),
            "recipes": {r: [[cid, dump_r(w)] for cid, w in e]
                        for r, e in sorted(self.recipes.items())},
        }
        return hashlib.sha256(json.dumps(
            canonical, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode()).hexdigest()

    # ------------------------- 基贡献与配方合成 ------------------------- #
    def clip_base(self, clip_id: str, n: int) -> Fraction:
        key = (clip_id, n)
        val = self._base_cache.get(key)
        if val is None:
            val = _clip_base(self.views[clip_id], self.pcm[self.views[clip_id]["asset_id"]], n)
            self._base_cache[key] = val
        return val

    def fader_base(self, recipe: str, fader_id: str, n: int) -> Fraction:
        """配方在该样本、该推子上的“基线”贡献（不含推子档位增益）。"""
        total = Fraction(0)
        for cid, w in self.recipes[recipe]:
            if self.linkage.get(cid) == fader_id:
                total += w * self.clip_base(cid, n)
        return total

    def recipe_faders(self, recipe: str) -> list[str]:
        seen = []
        for cid, _ in self.recipes[recipe]:
            fid = self.linkage.get(cid)
            if fid is not None and fid not in seen:
                seen.append(fid)
        return sorted(seen)

    def composite(self, recipe: str, n: int, assignment: dict[str, str] | None = None
                  ) -> Fraction:
        """未舍入、未饱和的精确合成值。"""
        assignment = self.normalized_assignment(assignment)
        total = Fraction(0)
        for fid in self.recipe_faders(recipe):
            base = self.fader_base(recipe, fid, n)
            if base:
                total += self.faders[fid].gain(assignment[fid]) * base
        return total

    def contribution(self, recipe: str, fader_id: str, n: int,
                     assignment: dict[str, str] | None = None) -> Fraction:
        """单推子对配方的未舍入、未饱和贡献。"""
        assignment = self.normalized_assignment(assignment)
        base = self.fader_base(recipe, fader_id, n)
        if not base:
            return Fraction(0)
        return self.faders[fader_id].gain(assignment[fader_id]) * base

    def window_contributions(self, recipe: str, n0: int, n1: int,
                             assignment: dict[str, str] | None = None
                             ) -> dict[str, Any]:
        """检查窗内各推子对某配方的舍入前/饱和前贡献（精确有理数）。

        返回 ``faders``（推子 -> 逐样本贡献）、``composite``（逐样本合成）与
        ``emitted``（实际出带整数样本，单次舍入后饱和）。
        """
        assignment = self.normalized_assignment(assignment)
        faders = {}
        for fid in self.recipe_faders(recipe):
            faders[fid] = [dump_r(self.contribution(recipe, fid, n, assignment))
                           for n in range(n0, n1)]
        composite = [self.composite(recipe, n, assignment) for n in range(n0, n1)]
        return {
            "recipe": recipe, "window": [n0, n1],
            "assignment": dict(sorted(assignment.items())),
            "faders": faders,
            "composite": [dump_r(v) for v in composite],
            "emitted": [emit_fraction(v) for v in composite],
        }

    def current_assignment(self) -> dict[str, str]:
        return {fid: f.current for fid, f in self.faders.items()}

    def normalized_assignment(self, assignment: dict[str, str] | None) -> dict[str, str]:
        result = self.current_assignment()
        if assignment:
            for fid, pid in assignment.items():
                if fid not in self.faders:
                    raise FaderError(f"方案引用未知推子 {fid}")
                if pid not in self.faders[fid].position_ids():
                    raise FaderError(f"推子 {fid} 无档位 {pid}")
                result[fid] = pid
        return result

    def cost(self, assignment: dict[str, str]) -> Fraction:
        return sum((self.faders[fid].cost_to(pid)
                    for fid, pid in assignment.items()), Fraction(0))

    # --------------------------- 检查窗求值 --------------------------- #
    def check_constraint(self, c: Constraint, assignment: dict[str, str],
                         recipe: str | None = None) -> dict[str, Any]:
        """逐样本核对单个检查窗，返回精确的通过/失败详情。"""
        recipe = recipe or c.recipe
        raw = [self.composite(recipe, n, assignment) for n in range(c.n0, c.n1)]
        emitted = [emit_fraction(v) for v in raw]
        if c.kind == "interval":
            bad = []
            for k, v in enumerate(raw):
                if (c.lo is not None and v < c.lo) or (c.hi is not None and v > c.hi):
                    bad.append({"n": c.n0 + k, "raw": dump_r(v)})
            return {
                "check": c.describe(), "pass": not bad,
                "raw": [dump_r(v) for v in raw],
                "emitted": emitted,
                "violations": bad,
            }
        total = sum(emitted)
        return {"check": c.describe(), "pass": total >= int(c.lower or 0),
                "emitted_sum": total, "emitted": emitted, "lower": c.lower}

    def check_assignment(self, assignment: dict[str, str] | None = None,
                         constraints: Iterable[Constraint] | None = None
                         ) -> list[dict[str, Any]]:
        assignment = self.normalized_assignment(assignment)
        cs = self.constraints if constraints is None else list(constraints)
        return [self.check_constraint(c, assignment) for c in cs]

    def is_feasible(self, assignment: dict[str, str]) -> bool:
        return all(r["pass"] for r in self.check_assignment(assignment))

    # --------------------------- 精确整数缩放 --------------------------- #
    def _scaled_constraint(self, c: Constraint) -> "_Scaled":
        num = c.n1 - c.n0
        # 每个推子、每个档位在该窗内的精确贡献
        frac_vecs: dict[str, dict[str, list[Fraction]]] = {}
        for fid in self.recipe_faders(c.recipe):
            bases = [self.fader_base(c.recipe, fid, n) for n in range(c.n0, c.n1)]
            if not any(b != 0 for b in bases):
                continue
            per_pos: dict[str, list[Fraction]] = {}
            for pid in self.faders[fid].position_ids():
                g = self.faders[fid].gain(pid)
                per_pos[pid] = [g * b for b in bases]
            # 若全部档位在窗内恒零，则此推子不影响该检查
            if not any(v != 0 for vec in per_pos.values() for v in vec):
                continue
            frac_vecs[fid] = per_pos

        scale = 1
        for per_pos in frac_vecs.values():
            for vec in per_pos.values():
                for v in vec:
                    if v:
                        scale = scale * v.denominator // math.gcd(scale, v.denominator)

        vecs: dict[str, dict[str, list[int]]] = {}
        vmin: dict[str, list[int]] = {}
        vmax: dict[str, list[int]] = {}
        for fid, per_pos in frac_vecs.items():
            vecs[fid] = {pid: [int(v * scale) for v in vec]
                         for pid, vec in per_pos.items()}
            cols = list(vecs[fid].values())
            vmin[fid] = [min(col[i] for col in cols) for i in range(num)]
            vmax[fid] = [max(col[i] for col in cols) for i in range(num)]

        lo_int = None if c.lo is None else _ceil_frac(c.lo * scale)
        hi_int = None if c.hi is None else _floor_frac(c.hi * scale)
        return _Scaled(cid=c.cid, recipe=c.recipe, kind=c.kind,
                       n0=c.n0, n1=c.n1, lo_int=lo_int, hi_int=hi_int,
                       lower=c.lower, scale=scale,
                       faders=sorted(vecs), vecs=vecs, vmin=vmin, vmax=vmax)

    # ----------------------------- 求解器 ----------------------------- #
    def solve(self, minimize_cost: bool = True,
              constraints: Iterable[Constraint] | None = None
              ) -> dict[str, Any]:
        """求满足全部检查窗的档位方案；minimize_cost=False 只判可行。

        返回 ``{"feasible", "assignment", "cost", "components"}``；
        可行时 assignment 覆盖所有推子（未受影响推子保持当前档）。
        """
        cs = self.constraints if constraints is None else list(constraints)
        scaled = [self._scaled_constraint(c) for c in cs]

        # 常数检查（不受任何推子影响）先判掉
        constant = [s for s in scaled if not s.faders]
        for s in constant:
            if not self._scaled_satisfied(s, [0] * (s.n1 - s.n0)):
                return {"feasible": False, "assignment": None, "cost": None,
                        "failed_constant": s.cid}

        # 用并查集按“同一检查窗共同影响”划分推子连通分量
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for s in scaled:
            if s.faders:
                union(s.faders[0], s.faders[0])
                for other in s.faders[1:]:
                    union(s.faders[0], other)

        groups: dict[str, list[_Scaled]] = {}
        for s in scaled:
            if s.faders:
                groups.setdefault(find(s.faders[0]), []).append(s)

        assignment = self.current_assignment()
        total_cost = Fraction(0)
        for root, cons in groups.items():
            faders = sorted({fid for s in cons for fid in s.faders})
            res = self._solve_component(faders, cons, minimize_cost)
            if not res["feasible"]:
                return {"feasible": False, "assignment": None, "cost": None,
                        "failed_component": faders}
            for fid, pid in res["assignment"].items():
                assignment[fid] = pid
            total_cost += res["cost"]

        return {"feasible": True, "assignment": assignment,
                "cost": total_cost, "components": len(groups)}

    def _solve_component(self, faders: list[str], cons: list["_Scaled"],
                         minimize_cost: bool) -> dict[str, Any]:
        # 推子排序：影响面大（跨检查/样本的总跨度）优先，利于剪枝
        def impact(fid: str) -> Fraction:
            tot = Fraction(0)
            for s in cons:
                if fid in s.vecs:
                    tot += sum(abs(a - b) for a, b in
                               zip(s.vmax[fid], s.vmin[fid]))
            return tot
        order = sorted(faders, key=lambda f: (-impact(f), f))

        costs = {fid: {pid: self.faders[fid].cost_to(pid)
                       for pid in self.faders[fid].position_ids()}
                 for fid in order}

        # suffix[d][ci] = 处理完第 d..end 个推子能给第 ci 检查带来的逐样本 min/max
        Ws = [s.n1 - s.n0 for s in cons]
        suffix_min: list[list[list[int]]] = []
        suffix_max: list[list[list[int]]] = []
        for s in cons:
            W = s.n1 - s.n0
            suf_min = [[0] * W for _ in range(len(order) + 1)]
            suf_max = [[0] * W for _ in range(len(order) + 1)]
            for d in range(len(order) - 1, -1, -1):
                fid = order[d]
                if fid in s.vecs:
                    suf_min[d] = [a + b for a, b in zip(suf_min[d + 1], s.vmin[fid])]
                    suf_max[d] = [a + b for a, b in zip(suf_max[d + 1], s.vmax[fid])]
                else:
                    suf_min[d] = suf_min[d + 1]
                    suf_max[d] = suf_max[d + 1]
            suffix_min.append(suf_min)
            suffix_max.append(suf_max)

        def still_possible(agg: tuple[int, ...], d: int) -> bool:
            """部分聚合 agg（各检查拼接的逐样本和）在第 d 层是否仍可完成。"""
            pos = 0
            for ci, s in enumerate(cons):
                W = Ws[ci]
                seg = agg[pos:pos + W]
                pos += W
                rmin = suffix_min[ci][d]
                rmax = suffix_max[ci][d]
                if s.kind == "interval":
                    for i in range(W):
                        if s.hi_int is not None and seg[i] + rmin[i] > s.hi_int:
                            return False
                        if s.lo_int is not None and seg[i] + rmax[i] < s.lo_int:
                            return False
                else:
                    upper = 0
                    for i in range(W):
                        upper += emit_scaled(seg[i] + rmax[i], s.scale)
                    if upper < int(s.lower or 0):
                        return False
            return True

        def final_ok(agg: tuple[int, ...]) -> bool:
            pos = 0
            for ci, s in enumerate(cons):
                W = Ws[ci]
                seg = list(agg[pos:pos + W])
                pos += W
                if not self._scaled_satisfied(s, seg):
                    return False
            return True

        zero = tuple(0 for _ in range(sum(Ws)))
        # ---- DP：状态 = 各检查的逐样本聚合向量；值 = 到达该状态的最低成本 ----
        # 可行性只取决于最终聚合，因此按状态合并是精确的。保留父指针以便回溯。
        MAX_STATES = 400_000
        # parent_layers[d][state] = (prev_state, position_id)
        parent_layers: list[dict[tuple[int, ...], tuple[tuple[int, ...], str]]] = []
        dp: dict[tuple[int, ...], Fraction] = {zero: Fraction(0)}
        exploded = False
        for d, fid in enumerate(order):
            ndp: dict[tuple[int, ...], Fraction] = {}
            parent: dict[tuple[int, ...], tuple[tuple[int, ...], str]] = {}
            choices = sorted(costs[fid].items(), key=lambda kv: kv[1])
            for agg, c in dp.items():
                for pid, add in choices:
                    vec: list[int] = []
                    pos = 0
                    ok = True
                    for ci, s in enumerate(cons):
                        W = Ws[ci]
                        if fid in s.vecs:
                            v = s.vecs[fid][pid]
                            seg = [agg[pos + i] + v[i] for i in range(W)]
                        else:
                            seg = list(agg[pos:pos + W])
                        pos += W
                        if s.kind == "interval":
                            rmin = suffix_min[ci][d + 1]
                            rmax = suffix_max[ci][d + 1]
                            for i in range(W):
                                if s.hi_int is not None and seg[i] + rmin[i] > s.hi_int:
                                    ok = False
                                    break
                                if s.lo_int is not None and seg[i] + rmax[i] < s.lo_int:
                                    ok = False
                                    break
                        if not ok:
                            break
                        vec.extend(seg)
                    if not ok:
                        continue
                    key = tuple(vec)
                    if not still_possible(key, d + 1):
                        continue
                    nc = c + add
                    prev = ndp.get(key)
                    if prev is None or nc < prev:
                        ndp[key] = nc
                        parent[key] = (agg, pid)
            if len(ndp) > MAX_STATES:
                exploded = True
                break
            parent_layers.append(parent)
            dp = ndp
            if not dp:
                return {"feasible": False}

        if not exploded:
            best_state = None
            best_cost = None
            for agg, c in dp.items():
                if final_ok(agg) and (best_cost is None or c < best_cost):
                    best_cost, best_state = c, agg
            if best_state is None:
                return {"feasible": False}
            assignment: dict[str, str] = {}
            state = best_state
            for d in range(len(order) - 1, -1, -1):
                prev, pid = parent_layers[d][state]
                assignment[order[d]] = pid
                state = prev
            return {"feasible": True,
                    "assignment": {fid: assignment[fid] for fid in order},
                    "cost": best_cost}

        # ---- 兜底：状态爆炸时用分支定界 DFS（仍精确） ----
        return self._solve_component_dfs(order, cons, costs, suffix_min,
                                         suffix_max, minimize_cost)

    def _solve_component_dfs(self, order, cons, costs, suffix_min,
                             suffix_max, minimize_cost) -> dict[str, Any]:
        cons_of: dict[str, list[int]] = {fid: [] for fid in order}
        for ci, s in enumerate(cons):
            for fid in s.faders:
                cons_of[fid].append(ci)
        suffix_cost = [Fraction(0)] * (len(order) + 1)
        for d in range(len(order) - 1, -1, -1):
            suffix_cost[d] = suffix_cost[d + 1] + min(costs[order[d]].values())

        cur = [[0] * (s.n1 - s.n0) for s in cons]
        chosen: dict[str, str] = {}
        best: dict[str, Any] = {"cost": None, "assignment": None, "found": False}

        def partial_ok(ci, base, vec, rem_min, rem_max):
            s = cons[ci]
            if s.kind == "interval":
                for i in range(len(base)):
                    if s.hi_int is not None and base[i] + vec[i] + rem_min[i] > s.hi_int:
                        return False
                    if s.lo_int is not None and base[i] + vec[i] + rem_max[i] < s.lo_int:
                        return False
                return True
            upper = sum(emit_scaled(base[i] + vec[i] + rem_max[i], s.scale)
                        for i in range(len(base)))
            return upper >= int(s.lower or 0)

        def search(prune: bool, first_only: bool) -> None:
            for ci in range(len(cons)):
                cur[ci] = [0] * (cons[ci].n1 - cons[ci].n0)
            chosen.clear()

            def dfs(d, cost):
                if prune and best["found"] and cost + suffix_cost[d] >= best["cost"]:
                    return False
                if d == len(order):
                    for ci, s in enumerate(cons):
                        if not self._scaled_satisfied(s, cur[ci]):
                            return False
                    if prune and best["found"] and cost >= best["cost"]:
                        return False
                    best.update(found=True, cost=cost, assignment=dict(chosen))
                    return True
                fid = order[d]
                affected = cons_of[fid]
                for pid in sorted(self.faders[fid].position_ids(),
                                  key=lambda q: costs[fid][q]):
                    extra = cost + costs[fid][pid]
                    if prune and best["found"] and extra + suffix_cost[d + 1] >= best["cost"]:
                        continue
                    if not all(partial_ok(ci, cur[ci], cons[ci].vecs[fid][pid],
                                          suffix_min[ci][d + 1], suffix_max[ci][d + 1])
                               for ci in affected):
                        continue
                    for ci in affected:
                        cur[ci] = [a + b for a, b in zip(cur[ci], cons[ci].vecs[fid][pid])]
                    chosen[fid] = pid
                    found = dfs(d + 1, extra)
                    chosen.pop(fid, None)
                    for ci in affected:
                        cur[ci] = [a - b for a, b in zip(cur[ci], cons[ci].vecs[fid][pid])]
                    if found and first_only:
                        return True
                return False

            dfs(0, Fraction(0))

        search(prune=False, first_only=True)
        if not best["found"]:
            return {"feasible": False}
        if minimize_cost:
            first = dict(best)
            search(prune=True, first_only=False)
            if best["cost"] > first["cost"]:
                best.update(first)
        return {"feasible": True, "assignment": best["assignment"],
                "cost": best["cost"]}

    @staticmethod
    def _scaled_satisfied(s: "_Scaled", cur: list[int]) -> bool:
        if s.kind == "interval":
            for v in cur:
                if s.lo_int is not None and v < s.lo_int:
                    return False
                if s.hi_int is not None and v > s.hi_int:
                    return False
            return True
        total = sum(emit_scaled(v, s.scale) for v in cur)
        return total >= int(s.lower or 0)

    # --------------------------- 不可行解释 --------------------------- #
    def explain_infeasible(self) -> dict[str, Any]:
        """区分“单套本就不可行”与“两套合办才无解”，给出互相排斥的检查。"""
        per_recipe: dict[str, bool] = {}
        for name in self.recipes:
            cs = [c for c in self.constraints if c.recipe == name]
            per_recipe[name] = self.solve(minimize_cost=False,
                                          constraints=cs)["feasible"] if cs else True

        overall = self.solve(minimize_cost=False)
        if overall["feasible"]:
            raise FaderError("方案可行，无需解释不可行")

        if not all(per_recipe.values()):
            bad = [n for n, ok in per_recipe.items() if not ok]
            pool = [c for c in self.constraints if c.recipe in bad]
            conflict = self._minimal_conflict(pool)
            classification = "single_recipe_infeasible"
            reason = f"配方 {bad} 单独即不可行"
        else:
            conflict = self._minimal_conflict(self.constraints)
            classification = "joint_only_infeasible"
            reason = "各配方单独均可行，但两套合办无解"

        return {
            "feasible": False,
            "classification": classification,
            "per_recipe_feasible": per_recipe,
            "mutually_exclusive_checks": [c.describe() for c in conflict],
            "reason": reason,
        }

    def _minimal_conflict(self, pool: list[Constraint]) -> list[Constraint]:
        if not pool:
            return []
        # 单条即不可行
        for c in pool:
            if not self.solve(minimize_cost=False, constraints=[c])["feasible"]:
                return [c]
        # 增量加入，一旦不可行则尽量删减
        current: list[Constraint] = []
        for c in pool:
            current.append(c)
            if not self.solve(minimize_cost=False, constraints=current)["feasible"]:
                changed = True
                while changed:
                    changed = False
                    for x in list(current):
                        trial = [y for y in current if y is not x]
                        if trial and not self.solve(minimize_cost=False,
                                                    constraints=trial)["feasible"]:
                            current = trial
                            changed = True
                            break
                return current
        return pool

    # --------------------------- 全片渲染 --------------------------- #
    def render_recipe(self, recipe: str, assignment: dict[str, str] | None = None,
                      pcm: dict[str, Any] | None = None) -> list[int]:
        """按合同渲染整套配方：先精确求和，再单次舍入，再饱和。"""
        assignment = self.normalized_assignment(assignment)
        pcm = pcm or self.pcm
        out = []
        for n in range(self.N):
            total = Fraction(0)
            for cid, w in self.recipes[recipe]:
                fid = self.linkage[cid]
                v = self.views[cid]
                base = _clip_base(v, pcm[v["asset_id"]], n)
                if base:
                    total += w * base * self.faders[fid].gain(assignment[fid])
            out.append(emit_fraction(total))
        return out

    def verify_from_pcm(self, assignment: dict[str, str] | None = None,
                        pcm: dict[str, Any] | None = None
                        ) -> dict[str, Any]:
        """用原始素材字节按所选档位重做两套实播核对（全新对象，无缓存）。"""
        assignment = self.normalized_assignment(assignment)
        pcm = pcm or self.pcm
        fresh = FaderProgram(self.raw_spec, self.product, pcm, self.spec_dir)
        reports = fresh.check_assignment(assignment)
        per_recipe = {}
        for name in fresh.recipes:
            samples = fresh.render_recipe(name, assignment, pcm=pcm)
            per_recipe[name] = {"n_samples": len(samples), "samples": samples}
        return {
            "ok": all(r["pass"] for r in reports),
            "checks": reports,
            "per_recipe": per_recipe,
        }

    # --------------------------- 校正单输出 --------------------------- #
    def correction_report(self, assignment: dict[str, str] | None = None,
                          wav_paths: dict[str, str] | None = None,
                          verified: bool = False) -> dict[str, Any]:
        assignment = self.normalized_assignment(assignment)
        changes = []
        for fid in sorted(self.faders):
            f = self.faders[fid]
            pid = assignment[fid]
            if pid != f.current:
                changes.append({
                    "fader_id": fid, "from": f.current, "to": pid,
                    "gain": dump_r(f.gain(pid)),
                    "cost": dump_r(f.cost_to(pid)),
                })
        return {
            "format": "audiomix.faders.correction/1",
            "feasible": True,
            "product_id": self.product.get("product_id"),
            "linkage_digest": self.linkage_digest,
            "T": dump_r(self.T),
            "n_samples": self.N,
            "total_cost": dump_r(self.cost(assignment)),
            "changes": changes,
            "assignment": dict(sorted(assignment.items())),
            "checks": [
                {"check": r["check"], "pass": r["pass"],
                 **({"emitted_sum": r["emitted_sum"], "lower": r["lower"]}
                    if "emitted_sum" in r else {})}
                for r in self.check_assignment(assignment)
            ],
            "wav": wav_paths or {},
            "verified_from_original_pcm": verified,
        }


@dataclass
class _Scaled:
    cid: str
    recipe: str
    kind: str
    n0: int
    n1: int
    lo_int: int | None
    hi_int: int | None
    lower: int | None
    scale: int
    faders: list[str]
    vecs: dict[str, dict[str, list[int]]]
    vmin: dict[str, list[int]]
    vmax: dict[str, list[int]]


# ============================ 装载与 WAV 导出 ============================ #

def load_fader_program(spec_path: str, registry_root: str) -> FaderProgram:
    """从联动规格 JSON + 素材登记仓装载模型。

    规格可给出 ``product``（产品文件路径，相对规格文件解析）或直接内嵌
    自包含 ``product`` 对象；二者都要能独立复算。
    """
    import os
    from .assets import AssetRegistry
    from .engine import load_product
    spec_path = os.path.abspath(spec_path)
    spec_dir = os.path.dirname(spec_path)
    with open(spec_path, encoding="utf-8") as fh:
        spec = json.load(fh)
    raw_product = spec.get("product")
    if raw_product is None and isinstance(spec.get("product_body"), dict):
        raw_product = spec["product_body"]
    if isinstance(raw_product, dict):
        product = raw_product
    elif isinstance(raw_product, str):
        product_path = raw_product
        if not os.path.isabs(product_path):
            product_path = os.path.join(spec_dir, product_path)
        product = load_product(product_path)
    else:
        raise FaderError("联动规格缺少 product（路径或内嵌产品对象）")
    reg = AssetRegistry(registry_root)
    pcm = {a["asset_id"]: reg.load_pcm(a["asset_id"]) for a in product["assets"]}
    return FaderProgram(spec, product, pcm, spec_dir=spec_dir)


def write_recipe_wav(path: str, samples: list[int], rate: int = OUTPUT_RATE) -> str:
    import numpy as np
    from .assets import write_wav
    write_wav(path, np.asarray(samples, dtype=np.int16), rate)
    return path
