# 多采样率剪辑时间轴与真实混音档案（audiomix）

纯 Python 3.11 实现（NumPy/SciPy/标准 `wave`），无视频、无音频设备、无网页。
两个独立服务：

1. **可独立编辑的音频时间轴服务**（`audiomix/timeline.py`）：插入/移动/修改/删除/
   区间查询/发布，clip 身份独立于素材，有理数时间，不渲染即可点查询。
2. **实际混音数据仓**（`audiomix/archive.py`）：只消费不可变发布产品与实际素材字节，
   固定 16kHz 输出，按固定样本块存档，内容寻址复用，支持范围读取与逐块追溯；
   不读取编辑器私有操作日志，也接受外部合法时间轴产品。

## 目录

```
audiomix/
  rationals.py   精确有理数（Fraction）表示与边界整数运算
  assets.py      PCM WAV 生成/读取 + 素材登记仓（asset_id=正文 SHA-256，绑定摘要/样本数）
  timeline.py    时间轴编辑器（整笔校验、点查询、不可变发布产品）
  engine.py      混音引擎：逐样本参考解释器 + 等价的 NumPy 分块向量化渲染
  archive.py     混音数据仓：内容寻址块存储 + SQLite 依赖索引 + 增量渲染 + 读取/追溯
  faders.py      推子联动 + 双播出配方 + 检查窗约束 + 最少成本校正单/无解分类
  cli.py         命令行入口
tests/           手算案例、数据仓案例、边界语义、推子联动测试
demo/            30 素材/100 clip/120s 规模案例、推子联动演示与 CLI 端到端走查
run_all_checks.sh 一键全量验证
```

## 核心语义（均为精确有理数，杜绝浮点边界误差）

- 素材：单声道有符号 16bit PCM，采样率仅 8000/16000/48000；
  `asset_id = SHA-256(PCM 正文)`，同字节内容永远同一身份，绑定摘要与样本数。
- clip：`clip_id` 与素材独立；同一素材可被任意多 clip 反复引用；多 clip 允许重叠。
  时长 `(b-a)/(源采样率×速度)`，参数全部为正/非负有理数。
- 编辑整笔拒绝：未知素材、源区间 `[a,b)` 越界、淡化之和超过 clip 时长等，
  单次/批量操作要么全部生效要么状态不变。
- 输出样本：固定 16kHz，总时长 T 显式给定，输出恰为所有满足
  `0 ≤ n/16000 < T` 的样本（数量 `ceil(16000T)`），区间一律左闭右开，
  不因浮点四舍五入多一个样本。
- 源映射：实数样本位置 `p = a + (t-start)×源采样率×速度`；相邻样本线性插值；
  `p` 到达最后一个样本之后（仍在 clip 时长内）保持末样本值，绝不读区间外。
- 淡化：增益乘 0→1 线性淡入、1→0 线性淡出；淡化时长为 0 时因子恒 1；
  边界用严格小于判定（`d < fade_in`、`remain < fade_out`）。
- **舍入只做一次**：所有有效 clip 的浮点贡献全部相加后，才做“恰半取偶”
  （banker's rounding）一次，再饱和到 `[-32768, 32767]`。单片段先裁剪/先舍入
  再相加是不同语义，本工程不采用。
- 块：每块 1600 样本（0.1s）。块存档记录峰值、实际削顶样本数、所用 clip/asset。

## 复用依据（为什么更正后不会误用旧块）

块键 `block_key = SHA-256(规范JSON{输出采样率, 块序号, 该块重叠到的全部 clip
完整身份与全部转换参数, 这些 clip 引用素材的摘要})`。

- 刻意**不含 product_id、不含 T**：时间轴更正产生新产品时，依赖逐项相同的
  未受影响块仍可复用；块内容与总时长无关。
- 任何重叠 clip 的参数变化或素材正文（摘要）变化都会令键改变 → 新产品不可能
  命中旧块。规模案例中移动一个 clip，1200 块里仅 2 块重渲染、1198 块复用。

## 命令速览（有理数接受 `1/3`、`0.25`、`7` 等写法）

```bash
# 素材登记（序列/斜坡/正弦/静音，自动生成 PCM WAV）
python3 -m audiomix.cli asset-seq --root DIR/registry --rate 16000 --values 0,100,200,300
python3 -m audiomix.cli asset-list --root DIR/registry

# 编辑
python3 -m audiomix.cli tl-insert --root DIR/registry --clip-id c1 \
    --asset-id AID --a 0 --b 4 --start 1/4000 --speed 2 --gain 3/2 \
    --fade-in 1/16000 --fade-out 1/8000
python3 -m audiomix.cli tl-move   --root DIR/registry --clip-id c1 --new-start 1/8000
python3 -m audiomix.cli tl-modify --root DIR/registry --clip-id c1 --set gain=1 --set speed=1
python3 -m audiomix.cli tl-delete --root DIR/registry --clip-id c1
python3 -m audiomix.cli tl-query  --root DIR/registry --lo 0 --hi 1/8000
python3 -m audiomix.cli tl-at     --root DIR/registry --time 1/16000   # 不渲染点查询
python3 -m audiomix.cli tl-publish --root DIR/registry --out product.json

# 校验/渲染/读取/追溯
python3 -m audiomix.cli verify-product --product product.json --registry-root DIR/registry
python3 -m audiomix.cli render-compare --product product.json \
    --registry-root DIR/registry --T 120          # 分块渲染 vs 独立逐样本解释器
python3 -m audiomix.cli render --archive-root DIR/archive --product product.json \
    --registry-root DIR/registry --T 120 --render-id r1
python3 -m audiomix.cli read  --archive-root DIR/archive --render-id r1 --start 0 --end 1600
python3 -m audiomix.cli trace --archive-root DIR/archive --render-id r1 --start 0 --end 3200
python3 -m audiomix.cli export-wav --archive-root DIR/archive --render-id r1 --out out.wav
python3 -m audiomix.cli verify-integrity --archive-root DIR/archive

# 推子联动 / 双播出配方（在发布产品之上，全部精确有理数）
python3 -m audiomix.cli fader-contrib --spec faders.json --registry-root DIR/registry \
    --recipe jia --n0 0 --n1 1600     # 检查窗内各推子舍入前/饱和前贡献
python3 -m audiomix.cli fader-solve   --spec faders.json --registry-root DIR/registry
python3 -m audiomix.cli fader-correct --spec faders.json --registry-root DIR/registry \
    --out correction.json             # 最少人工成本校正单 + 两份真实 WAV
python3 -m audiomix.cli fader-explain --spec faders.json --registry-root DIR/registry
                                      # 无解时区分单套/合办并列出互相排斥的检查
```

完整端到端示例：`bash demo/cli_walkthrough.sh`；推子联动演示：
`python3 -m demo.fader_demo`；全量验证：`bash run_all_checks.sh`。

## 推子联动与双播出配方（`audiomix/faders.py`）

在既有不可变发布产品之上再叠一层“稳定 clip 联动关系”。产品与素材字节仍是
唯一的实播依据，联动规格（`audiomix.faders/1`）单独保存：

```jsonc
{
  "format": "audiomix.faders/1", "T": "1/16000",
  "product": "product.json",            // 或直接内嵌 "product_body": {…}
  "faders": [                            // 每支推子：有限精确有理增益档 + 人工成本
    {"fader_id": "f1", "current": "full", "costs": {"half": "1"},
     "positions": [{"id": "full", "gain": "1"}, {"id": "half", "gain": "1/2"}]}
  ],
  "linkage": {"clip_p": "f1", "clip_n": "f2"},   // 共用推子 => 必须同档；同素材可分属不同推子
  "recipes": {                           // 两套单声道播出配方：参与的 clip + 有理配重
    "jia": [{"clip_id": "clip_p", "weight": "1"}, {"clip_id": "clip_n", "weight": "1"}],
    "yi":  [{"clip_id": "clip_p", "weight": "1"}]
  },
  "constraints": [                       // 检查窗（左闭右开样本区间）
    {"id": "jia", "recipe": "jia", "kind": "interval",  "n0": 0, "n1": 1, "lo": "0", "hi": "30"},
    {"id": "yi",  "recipe": "yi",  "kind": "lower_sum", "n0": 0, "n1": 1, "lower": 60}
  ]
}
```

- **联动关系登记在 clip 上**：若两次出现共用一个推子则选同一档；同一素材可由
  不同推子独立控制（同一 asset 可有多个 clip 分属不同推子）。
- **贡献可独立给出**：`fader-contrib` / `window_contributions` 返回检查窗内每支
  推子对左右两套配方各自的“舍入前、饱和前”精确有理贡献，以及合成值与出带整数。
- **两套检查窗、同一档位方案**：`interval` 约束未饱和合成值逐样本落在闭区间；
  `lower_sum` 约束实际出带整数样本之和达到下限。同一推子方案必须两套都过。
- **最少人工成本**：`fader-solve` / `fader-correct` 求全局最小成本档位方案
  （精确 DP，等价状态合并；超大规模回退精确分支定界），并列最优取其一。
- **合同不变**：贡献仍含原有速度/淡化/末样本保持；所有 clip 浮点/有理贡献先
  求和，再做唯一一次“恰半取偶”舍入，最后饱和。校验与渲染都走精确 `Fraction`，
  与既有 `engine` 参考解释器逐样本一致（见 `tests/test_faders.py`）。
- **保存前重做核对**：`fader-correct` 先用原始素材字节 + 所选档位，在全新对象上
  重做两套实播核对，通过后才写校正单与两份真实 16kHz WAV。
- **无解分类**：`fader-explain` 区分 `single_recipe_infeasible`（单套本就不可行）
  与 `joint_only_infeasible`（两套各自可行、合办才无解），并给出互相排斥的实际检查。

## 验证策略（不使用任何外部混音结果）

- `tests/test_hand_calc.py`：短整数序列素材，10 个案例人工推导每个输出样本
  （跨采样率插值、末样本保持、2 倍/半倍速、淡化边界、重叠相消、削顶、
  恰半取偶、跨块边界、整笔拒绝、48k 抽点），并要求**手算值 = 逐样本参考
  解释器 = 分块向量化渲染 = 逐块拼接**四者一致。
- `tests/test_archive.py`：内容寻址复用、移动 clip 的增量渲染、素材摘要变化
  零误用、外部产品篡改拒绝、编辑器日志移走后旧 render 仍可读。
- `tests/test_edge.py`：非整数 T、左闭右开边界、非法修改回滚、持久化重开、
  “先求和后单次舍入”与“逐片段先舍入”的语义区分。
- `demo/scale_demo.py`：30 素材（每采样率 10 个）、100 clip、120 秒/192 万样本，
  含全部要求情形；窗口化逐样本交叉核对，输出 WAV、块峰值、削顶数与来源索引。
- `tests/test_faders.py`：题面手算示例（两支降半档 => 甲 10/乙 50；仅压第一支
  => 甲 -30；放宽乙后原档成本 0）、推子层与既有引擎参考逐样本一致（速度/淡化/
  末样本保持/单次舍入）、两套配方共用方案、相消依赖、`lower_sum` 实际整数和、
  `joint_only_infeasible` 分类与互相排斥检查、旧播 WAV 相同而联动分组不同
  （一份可修/一份无解）、并列最优、从原始素材重做核对往返。
- `demo/fader_demo.py`：上述手算示例、双配方贡献、真实 WAV 输出与不可行分类的
  端到端走查（等价状态合并 DP 对 18 支全耦合推子亦为毫秒级）。

参考解释器（`engine.render_reference`）是独立的纯 Python 逐样本 Fraction 实现，
与向量化渲染路径代码完全分开，仅共享产品装载/校验，作为语义基准。
