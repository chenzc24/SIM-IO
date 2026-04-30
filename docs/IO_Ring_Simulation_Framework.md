# IO Ring Virtuoso 仿真自动化框架

## 总体目标

将 IO Pad Ring 的功能验证从"人工搭 testbench + 手动跑仿真 + 肉眼看波形"推进到"AI 读配置 → 自动建 symbol / 写 netlist → 驱动 spectre → 比对结果 → 输出 pass/fail 报告"的闭环。

---

## Step 1：Symbol 自动生成

### 1.1 目标

从 schematic 导出 symbol，使 pad ring 被封装为矩形黑盒（左侧输入、右侧输出、顶部/底部供电），暴露接口供 testbench 调用。

### 1.2 子任务分解

| # | 子任务 | 说明 |
|---|--------|------|
| 1.2.1 | 手动走一遍 symbol 生成流程 | Create → Cellview → From Symbol，记录每一步操作 |
| 1.2.2 | 开启 CIW 监视，抓取 skill code | `hiSetCIWLogLevel(3)` 或 `drSetDebugLevel(3)`，捕获完整的 skill 调用序列 |
| 1.2.3 | 分析 CDF 传播机制 | 确认 schematic CDF 参数（model、w、l 等）是否自动继承到 symbol；如果不继承，需要显式 `cdfCreateBaseClassCDF` + `cdfCopyCDF` |
| 1.2.4 | 设计 symbol 布局约定 | 固定 pin 位置规则：信号输入→左侧、信号输出→右侧、VDD→顶部、VSS→底部；pin 间距等距分布 |
| 1.2.5 | 编写 symbol 生成 skill 脚本 | 基于抓取的 skill code，参数化 pin 列表和位置，实现 `createSymbol(cellName pinList layoutRule)` |
| 1.2.6 | 验证：实例化 symbol，右键 Properties | 检查 CDF 参数完整性和默认值是否与 schematic 一致 |

### 1.3 CDF 不匹配问题深度分析

CDF 不匹配是 symbol 生成中最常见的坑，表现形式和根因：

| 现象 | 根因 | 解法 |
|------|------|------|
| symbol 实例化后 Properties 为空 | CDF 未从 schematic 继承 | `cdfCopyCDF("lib" "schematicCell" "symbolCell")` |
| 部分 CDF 参数丢失 | CDF 参数的 `propDisplayMode` 设为 `nil` | 显式设置 `cdfId->propDisplayMode = t` |
| 修改 CDF 默认值不生效 | instCDF 与 cellCDF 优先级冲突 | 确认修改的是 cell 级 CDF |
| bus pin 在 symbol 上展开 | pin 名含 `<>` 但 symbol 未处理 | 使用 `leCreatePin` 时指定 `accessDir` 和 bus 展开规则 |

**关键原则**：symbol 的 CDF 必须是 schematic CDF 的真子集（只暴露需要用户配置的参数），而不是简单复制。

### 1.4 Symbol 布局规范设计

```
        VDD ─────────────────────
       ┌──────────────────────────┐
  IN0 ─┤                          ├─ OUT0
  IN1 ─┤      PAD RING SYMBOL     ├─ OUT1
  IN2 ─┤                          ├─ OUT2
  ...  ┤                          ├─ ...
       └──────────────────────────┘
        VSS ─────────────────────

  规则：
  - 矩形外框固定尺寸（如 10um × 8um）
  - 左侧 pin 等间距排列，y 坐标 = y_start + i * pitch
  - 右侧 pin 对称排列
  - 供电 pin 放顶部/底部，居中
  - pin label = net name，字体统一
  - pin direction = input/output/bidirectional，必须与 schematic terminal 一致
```

### 1.5 AI 可操作接口设计

AI 通过 Bridge 调用 Virtuoso 的接口应为：

```python
# 输入
{
    "library": "myLib",
    "cell": "pad_ring_top",
    "pins": [
        {"name": "IN0",   "direction": "input",  "side": "left"},
        {"name": "OUT0",  "direction": "output", "side": "right"},
        {"name": "VDD",   "direction": "inputOutput", "side": "top"},
        {"name": "VSS",   "direction": "inputOutput", "side": "bottom"},
    ],
    "cdf_params": ["model", "w", "l", "m"]  # 需暴露的 CDF 参数
}

# 输出
{
    "status": "success",
    "symbol_cellview": "myLib/pad_ring_top/symbol",
    "pin_count": 4,
    "cdf_match": true  # CDF 校验结果
}
```

---

## Step 2：Testbench 搭建与仿真

### 2.1 两条路径对比

| 维度 | 路径一：Symbol + Virtuoso 连线 | 路径二：AI 直接写 Netlist |
|------|-------------------------------|--------------------------|
| **原理** | AI 操作 Virtuoso 图形化建 testbench cellview，用 skill 连线，然后导出 netlist | AI 根据信号功能直接生成 spectre netlist 文本 |
| **优点** | netlist 由 Virtuoso 导出，语法和拓扑天然正确；CDF 参数自动展开 | 速度快，不依赖 Virtuoso 图形界面，流程极简 |
| **缺点** | 依赖 skill 连线的可靠性；坐标定位复杂 | netlist 语法容错为零；subckt 端口顺序、实例参数格式一点错就全挂 |
| **风险等级** | 中（连错线可复现、可调试） | 高（netlist 瑕疵 → 仿真直接报错，调试困难） |
| **适用场景** | 主路径，所有常规验证 | 仅适用于已验证拓扑的参数扫描变体 |

**决策**：主走路径一，路径二作为已验证拓扑的快速变体手段（先有路径一的 golden netlist，再基于它做模板化修改）。

### 2.2 路径一：Symbol-based Testbench 详细设计

#### 2.2.1 Testbench 架构

```
┌────────────────── Testbench Cellview ──────────────────────┐
│                                                             │
│   VDD_SRC ──┬───── VDD (pad_ring_symbol top)               │
│             │                                               │
│   VSS_SRC ──┴───── VSS (pad_ring_symbol bottom)            │
│                                                             │
│   VPULSE ────────── IN0 (pad_ring_symbol left)             │
│   VPULSE ────────── IN1 (pad_ring_symbol left)             │
│                                                             │
│   CLOAD ─────────── OUT0 (pad_ring_symbol right)           │
│   CLOAD ─────────── OUT1 (pad_ring_symbol right)           │
│                                                             │
│   ┌────────────────────────────────────┐                    │
│   │      PAD RING SYMBOL (DUT)        │                    │
│   └────────────────────────────────────┘                    │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

#### 2.2.2 连线策略

连线不是"画线"，而是"放 instance + 创建 net"：

1. **放置 DUT instance**：`dbCreateInst` 放置 symbol
2. **放置激励源 instance**：`dbCreateInst` 放置 VDC/VPULSE/VPWL
3. **放置负载 instance**：`dbCreateInst` 放置 CAP/RES
4. **创建 net 并连接**：
   - 获取 pin 的 `accessDir` 和坐标
   - `dbCreateNet` 创建 net
   - `dbCreateFigFromNet` 创建 wire
   - 或直接用 `dbAddFigToNet` + `leCreateWire`

**坑**：Virtuoso 的 wire 必须沿 grid 对齐，否则 DRC 会报 off-grid 错误。坐标计算必须基于 `mfgGrid`。

#### 2.2.3 激励源模板（按 pad 类型）

| Pad 类型 | 激励源 | 关键参数 | 目的 |
|----------|--------|----------|------|
| 数字输入 | VPULSE | v1=0, v2=VDD, period=T, rise=tr, fall=tf | 验证输入响应 |
| 数字输出 | VDC（上拉）+ CLOAD | vdc=VDD, c=c_load | 验证驱动能力 |
| 双向（做输入） | VPULSE | 同数字输入 | 验证输入模式 |
| 双向（做输出） | VDC + CLOAD | 同数字输出 | 验证输出模式 |
| 模拟信号 | VPWL / VSIN | 自定义波形 | 验证模拟通路线性度 |
| 供电 | VDC | vdc=VDD/VSS | 提供偏置 |

#### 2.2.4 仿真配置

```python
sim_config = {
    "simulator": "spectre",
    "analysis": [
        {"type": "tran", "stop": "10u", "errpreset": "moderate"},
        {"type": "dc",   "save": "allpub"},
    ],
    "save_signals": [
        "IN*",      # 所有输入
        "OUT*",     # 所有输出
        "VDD", "VSS",
        "DUT:*",    # DUT 内部关键节点（如需要）
    ],
    "model_files": ["${PDK_PATH}/models.scs"],
}
```

### 2.3 路径二：Netlist 模板化（基于路径一的 golden netlist）

路径二不是"从头写 netlist"，而是"从 golden netlist 提取模板 → 参数化替换"：

```
Golden netlist (由 Virtuoso 导出)
    ↓  提取拓扑骨架
Template netlist (参数化)
    ↓  替换参数
Variant netlist (具体配置)
    ↓  spectre 直接运行
仿真结果
```

这样路径二的风险被限制在"参数替换"这一步，而非"整个 netlist 语法"。

---

## Step 3：仿真结果收集与映射表构建

### 3.1 需要收集的 case 分类

#### 正确连接（Pass Case）

| # | Pad 类型 | 连接方式 | 预期关键指标 |
|---|----------|----------|-------------|
| P1 | 数字输入 | VPULSE → pad_in | 输出跟随输入，delay < t_spec |
| P2 | 数字输出 | pad_out → CLOAD | VOH/VOL 满足 spec，slew rate 合格 |
| P3 | 数字双向（输入模式） | OE=0, VPULSE → pad_io | 同 P1 |
| P4 | 数字双向（输出模式） | OE=1, pad_io → CLOAD | 同 P2 |
| P5 | 模拟信号 | VSIN → pad_ana | 增益 ≈ 1，THD < spec |
| P6 | 模拟供电 | VDC → pad_vdd | 静态电流在 spec 内 |
| P7 | 地 | VDC(0) → pad_vss | 压降 < spec |

#### 错误连接（Fail Case）

| # | 错误类型 | 表现 | 仿真中的征兆 |
|---|----------|------|-------------|
| F1 | 数字方向接反 | 输出当输入用 | 输出端电压被拉到中间态/VPULSE 钳位 |
| F2 | 双向 OE 悬空 | OE 无驱动 | pad_io 呈高阻，输出不确定 |
| F3 | 供电缺失 | VDD 未接 | 所有输出 = 0 或高阻 |
| F4 | 输出短路 | OUT 接 VSS | 电流过大 → 收敛失败或 I(VSS) 异常 |
| F5 | 模拟端接数字激励 | VSIN 幅度超 VDD | 信号削顶 |

### 3.2 映射表结构设计

```python
golden_mapping = {
    "digital_input": {
        "stimulus": {"type": "VPULSE", "params": {"v1": 0, "v2": 1.8, "period": "100n"}},
        "expected": {
            "V(OUT)": {"type": "digital_follow", "delay_max": "5n", "vo_min": 1.62, "vo_max": 1.8},
            "I(VDD)": {"type": "range", "min": 0, "max": "1m"},
        },
        "failure_signatures": {
            "constant_mid": "direction_mismatch",    # 输出卡在 VDD/2
            "zero_output": "missing_power",          # 输出始终为 0
            "large_current": "output_short",         # 电流异常大
        }
    },
    "digital_output": {
        "stimulus": {"type": "VDC", "params": {"vdc": 1.8}},
        "load": {"type": "CAP", "params": {"c": "10p"}},
        "expected": {
            "V(OUT)": {"type": "digital_drive", "voh_min": 1.62, "vol_max": 0.18, "slew_min": "1n"},
        },
        "failure_signatures": {
            "slow_slew": "insufficient_drive",
            "voltage_dip": "overloaded",
        }
    },
    # ... analog, bidirectional, power ...
}
```

### 3.3 数据采集方式

仿真完成后，从 spectre 输出中提取数据：

1. **PSF 文件解析**：读取 `tran.tran/tran.tran` 中的波形数据
2. **关键测量**：用 spectre 内置 measurement 或后处理提取
   - 延迟：`delay(V(IN), V(OUT), 0.5, 0.5, "rising")`
   - 压摆率：`slew(V(OUT), 0.1, 0.9)`
   - 功耗：`avg(I(VDD) * V(VDD))`
3. **CSV 导出**：将测量结果导出为结构化 CSV，供 AI 比对

---

## Step 4：自动化验证逻辑

> 原 plan 中 Step 4 缺失，此处补全——这是"AI 比对报错"的核心逻辑。

### 4.1 验证流程

```
仿真结果 (PSF/CSV)
    ↓
特征提取（延迟、电平、电流、斜率）
    ↓
与 golden_mapping 比对
    ↓
┌─────────────────────────────────┐
│  三种判定结果                      │
│  ✓ PASS：所有指标在容差内           │
│  ⚠ WARNING：指标偏移但未越限        │
│  ✗ FAIL：指标越限或匹配到 failure   │
│        signature                  │
└─────────────────────────────────┘
    ↓
生成验证报告
```

### 4.2 容差与判定规则

```python
tolerance = {
    "voltage": {"abs": 0.05, "rel": 0.03},   # 绝对 50mV 或相对 3%
    "delay":   {"abs": "1n", "rel": 0.1},     # 绝对 1ns 或相对 10%
    "current": {"rel": 0.2},                  # 相对 20%（电流波动大）
    "slew":    {"rel": 0.15},                 # 相对 15%
}

def judge(measured, expected, tol):
    """返回 PASS / WARNING / FAIL"""
    if isinstance(expected, dict):
        if "min" in expected and "max" in expected:
            lo, hi = expected["min"], expected["max"]
            if lo * (1 - tol) <= measured <= hi * (1 + tol):
                return "PASS"
            elif lo * 0.8 <= measured <= hi * 1.2:
                return "WARNING"
            else:
                return "FAIL"
    # ... other patterns
```

### 4.3 Failure Signature 匹配

除了数值比对，还需支持模式匹配（因为错误连接的征兆可能是定性特征）：

| Signature | 检测方法 | 诊断 |
|-----------|----------|------|
| constant_mid | V(OUT) 方差 < 阈值 且 均值 ≈ VDD/2 | 方向接反 |
| zero_output | V(OUT) 均值 < 0.1V | 供电缺失或未驱动 |
| large_current | I(VDD) max > 10× 预期 | 输出短路 |
| clipped_signal | V(OUT) 峰值 < VDD - 0.1 且波形顶部平坦 | 模拟信号超限 |
| high_impedance | V(OUT) 随负载变化剧烈 | OE 悬空或高阻态 |

### 4.4 验证报告格式

```json
{
    "test_id": "digital_input_IN0",
    "pad_type": "digital_input",
    "verdict": "PASS",
    "measurements": {
        "V(OUT)_delay": {"value": "3.2n", "expected": "<5n", "status": "PASS"},
        "V(OUT)_voh":   {"value": "1.72", "expected": ">1.62", "status": "PASS"},
    },
    "signatures_matched": [],
    "summary": "All metrics within tolerance. No failure signatures detected."
}
```

---

## Step 5：Skill 封装与端到端测试

### 5.1 Skill API 设计

```python
# Skill 输入
{
    "pad_ring_config": {
        "library": "myLib",
        "cell": "pad_ring_top",
        "pads": [
            {"name": "PAD_IN0",  "type": "digital_input",     "signal": "DATA0"},
            {"name": "PAD_OUT0", "type": "digital_output",    "signal": "DATA_OUT0"},
            {"name": "PAD_IO0",  "type": "digital_bidirectional", "signal": "BIDIR0"},
            {"name": "PAD_ANA0", "type": "analog_signal",     "signal": "ANA0"},
            {"name": "PAD_VDD",  "type": "analog_power",      "signal": "VDD"},
            {"name": "PAD_VSS",  "type": "analog_ground",     "signal": "VSS"},
        ]
    },
    "sim_options": {
        "corner": "tt",
        "temperature": 25,
        "vdd": 1.8,
    }
}

# Skill 输出
{
    "status": "complete",
    "symbol_created": true,
    "testbench_created": true,
    "simulations_run": 6,        # 每个 pad 一组
    "results": {
        "PAD_IN0":  {"verdict": "PASS"},
        "PAD_OUT0": {"verdict": "PASS"},
        "PAD_IO0":  {"verdict": "WARNING", "detail": "slew 15% below spec"},
        "PAD_ANA0": {"verdict": "PASS"},
        "PAD_VDD":  {"verdict": "PASS"},
        "PAD_VSS":  {"verdict": "PASS"},
    },
    "report_path": "/path/to/report.json"
}
```

### 5.2 分段测试策略

#### 阶段 A：单 Pad 验证

- 对每种 pad 类型，单独建 symbol → testbench → 仿真 → 验证
- 目的：验证 skill 对每种 pad 类型的完整链路
- 通过标准：6 种 pad 类型全部 PASS

#### 阶段 B：分段 Pad Ring 验证

- 选取 pad ring 的一个片段（如 4 个 pad：1 input + 1 output + 1 power + 1 ground）
- 建 symbol → testbench → 仿真 → 验证
- 目的：验证多 pad 共存时的连线正确性和供电网络
- 通过标准：分段内所有 pad PASS

#### 阶段 C：完整 Pad Ring 验证

- 对完整 pad ring（可能 50-100+ pad）执行全量验证
- 目的：验证规模化下的稳定性和性能
- 特殊考虑：
  - 仿真规模：全 pad ring 的瞬态仿真可能很慢，需按 pad 组分组仿真
  - 内存：大规模 PSF 文件解析需流式处理
  - 超时：设置仿真超时，防止收敛失败无限挂起

### 5.3 Skill 内部流水线

```
┌─────────────┐    ┌─────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  1. Parse    │───→│  2. Symbol  │───→│  3. Testbench │───→│  4. Simulate │───→│  5. Verify   │
│  Config      │    │  Generate   │    │  Build        │    │  (Spectre)   │    │  & Report    │
└─────────────┘    └─────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
                         │                    │                    │
                         ↓                    ↓                    ↓
                    CDF 校验            连线 DRC 检查         收敛检查
                    (Step 1.3)          (wire on-grid)       (若不收敛 → 报告)
```

每一步都有校验点，失败时报告具体原因而非静默继续。

---

## 跨步骤依赖与风险矩阵

### 依赖关系

```
Step 1 (Symbol)
    │  CDF 必须正确，否则 Step 2 的 instance 参数全部错误
    ↓
Step 2 (Testbench)
    │  netlist 必须正确导出，否则 Step 3 无数据
    ↓
Step 3 (映射表)
    │  golden mapping 必须覆盖所有 pad 类型，否则 Step 4 无法判定
    ↓
Step 4 (验证逻辑)
    │  判定规则必须经过标定，否则 Step 5 误报/漏报
    ↓
Step 5 (Skill 封装)
```

### 风险矩阵

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| CDF 不匹配导致 symbol 参数缺失 | 高 | 致命 | Step 1.3 的 CDF 校验脚本 |
| Skill 连线坐标 off-grid | 中 | 严重 | 基于 mfgGrid 计算坐标，加 DRC 检查 |
| Spectre 不收敛 | 中 | 阻塞 | 设置 maxsteps、gmin 抬高策略、超时兜底 |
| PSF 解析失败 | 低 | 阻塞 | 回退到 raw ASCII 解析 |
| Golden mapping 容差不合理 | 中 | 误报 | 先用已知正确 case 标定容差，再推广 |
| 大规模 pad ring 仿真时间过长 | 高 | 效率 | 分组并行仿真 |
| Netlist 语法错误（路径二） | 高 | 致命 | 优先用路径一；路径二仅用于已验证模板的参数替换 |

---

## 待研究问题清单

以下问题在动手前需要通过实验确认：

1. **CIW 监视的具体 skill code 是什么？** 需要实际操作一遍 symbol 生成，确认抓到的 code 是否足够参数化
2. **CDF 从 schematic 到 symbol 的传播路径是什么？** 是自动的还是要手动 `cdfCopyCDF`？
3. **Virtuoso 中 skill 连线的坐标系统是什么？** origin 在哪？单位是什么？grid 是多少？
4. **Spectre 仿真输出的 PSF 格式是否有 Python 解析库？** 还是需要自己写解析器？
5. **Pad 的 SPICE model 在哪里？** 是否需要额外的 model 文件？PDK 是否提供 behavior model？
6. **双向 pad 的 testbench 如何处理 OE 信号？** 是单独加控制源还是需要时序逻辑？
7. **模拟 pad 的验证标准是什么？** 纯功能验证还是需要性能指标（增益、噪声等）？
8. **分组仿真的分组策略如何确定？** 按信号类型分组还是按物理位置分组？供电 pad 是否每组都要包含？
