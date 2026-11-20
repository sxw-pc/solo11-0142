# 天文观测夜次采录台

台站观测协调员用本地合成数据登记望远镜时隙、暗场与平场标定、科学曝光和归档结论。所有输入为虚构标识与元数据，不读取真实望远镜，也不联网。

## 快速运行

Python 3.11+，在仓库根目录执行：

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m nightledger demo --db demo.sqlite
python -m pytest -q
```

子系统入口与报告：

```powershell
python -m nightledger slot --db sample.sqlite --input fixtures/slot-ok.json
python -m nightledger calibration --db sample.sqlite --input fixtures/calibration-ok.json
python -m nightledger exposure --db sample.sqlite --input fixtures/exposure-ok.json
python -m nightledger report --db sample.sqlite
```

`*-conflict.json` 提供重叠时隙、标定帧异摘要和科学帧异摘要反例。`source` 与 `source_conflict` 保留来源和原始 JSON 摘要；`decision.previous` 与 `archive_event.previous` 串接裁定历史。输入在 SQLite 事务内写入，重复来源幂等。

## 三个独立子系统

`slot` 根据同望远镜时隙优先权、停机区间、目标高度和太阳距角决定可观测状态，并冻结滤镜、探测器、温度和曝光时间的配置版本。`calibration` 依据帧摘要、暗场温度与曝光时长、平场滤镜、到期时间裁定标定集合是否可用。`exposure` 核对科学帧目标、指向偏差、标定覆盖和重拍替换关系，形成可撤回的归档结果。

三条跨系统反例：停机使原本可归档科学帧撤回；滤镜版次变化使平场失配并撤回归档；指向失败帧被合格重拍替换后，原失败记录和新归档谱系同时保留。测试分别验证。

本版不实现真实 FITS 图像处理、遥测接入、跨夜次标定继承、科研计划配额、协同曝光或外部归档副本。

## 增强采录起点：逐星测光

`photometry` 接收离线曝光的目标星孔径计数、天空背景及孔径像素数，保存逐帧差分通量与光变状态。它使用既有 `archive_case` 的可归档裁定；时隙或标定变化撤回曝光后，两项接收结果都会从仍可用帧重算，前后结论分别留在 `photometry_product_event` 事件链上。单比较星旧格式（如 `fixtures/photometry-dip.json`）行为不变。

多比较星采录在会话级 `comparisons` 注册比较星身份与星表锚点（星等及误差），逐帧 `comparisons` 给出各星孔径计数、缺测与饱和标记，同一星在各帧标识一致（未注册的星名直接拒收）。调和时用整夜重叠帧求解零点：成对恒定链（重叠链）筛查比较星自身变化并排除变星，星表锚点提供绝对参照；目标星不参与零点拟合，真正的目标暗化不会被吸收进零点。只有重叠链可辨识、且检验帧拥有至少两颗独立的锚定稳定参照时，正式测光表（`catalogue`）才给出可发布结论；缺少锚点时明确报 `indeterminate` 并说明结论仍取决于哪些比较星假设，不补造确定结果。当夜跟进单（`followup`）始终只是以观测员指定比较星稳定为条件的快览，附补拍建议；两份结果互不替代，各自记录所用参照、误差与适用帧（`photometry_product_detail`）。

```powershell
python -m nightledger slot --db photometry.sqlite --input fixtures/slot-ok.json
python -m nightledger slot --db photometry.sqlite --input fixtures/slot-night-2.json
python -m nightledger calibration --db photometry.sqlite --input fixtures/calibration-ok.json
python -m nightledger calibration --db photometry.sqlite --input fixtures/calibration-ok-2.json
python -m nightledger exposure --db photometry.sqlite --input fixtures/exposure-ensemble.json
python -m nightledger photometry --db photometry.sqlite --input fixtures/photometry-ensemble.json
python -m nightledger calibration --db photometry.sqlite --input fixtures/calibration-conflict-2.json
python -m nightledger report --db photometry.sqlite
```

`photometry-ensemble.json` 中 e-3 缺 B、e-4 缺 A，而 C 在后两帧增亮 12%：跟进单以 C 稳定为条件保留有条件暗化线索并要求补拍，正式表经 A/C、B/C 重叠链排除 C 后报无暗化。`photometry-ensemble-blind.json` 的后两帧 A、B 都缺测，两种解释不可区分，正式表报 `indeterminate`。`calibration-conflict-2.json` 撤回 night-2 的两张关键帧后，两项结果都从仍可用帧重算为 `pending`。`photometry-ensemble-dip.json` 演示目标真暗化时正式表确认 `candidate`。
