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

`photometry` 接收离线曝光的目标星和单颗比较星孔径计数、天空背景及孔径像素数，保存逐帧差分通量与光变候选状态。它使用既有 `archive_case` 的可归档裁定；时隙或标定变化撤回曝光后，光变结果随之重算并保留事件链。当前仅有单比较星、前两张可用帧作基线的初筛方法，不处理变星比较星、缺测、颜色/大气消光或拥挤星场。
当夜跟进单和正式测光表分别落在 `photometry_product`，各自有版本与事件链。目前两项都直接沿用单比较星的初筛结果，正式表尚未实现独立参照审核。

```powershell
python -m nightledger slot --db photometry.sqlite --input fixtures/slot-ok.json
python -m nightledger calibration --db photometry.sqlite --input fixtures/calibration-ok.json
python -m nightledger exposure --db photometry.sqlite --input fixtures/exposure-photometry.json
python -m nightledger photometry --db photometry.sqlite --input fixtures/photometry-dip.json
python -m nightledger report --db photometry.sqlite
```
