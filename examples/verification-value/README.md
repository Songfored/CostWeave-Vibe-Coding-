# 真实小票核验实验

任务是从一张印度尼西亚小票的原始图片读出最终应付金额，统一记为百分之一印尼盾。三条完整路线分别使用低分辨率直接读取、较高分辨率读取并核对现金和找零、两次读取原图后检查两者是否一致。后两条路线仍可能犯相同的错误。原图尺寸可提前获得，用高宽比划分两组。每条路线都可以明确放弃作答。

原始图片和独立人工标注来自 Park 等人和 NAVER CLOVA 的 *CORD: A Consolidated Receipt Dataset for Post-OCR Parsing*（2019），按 CC BY 4.0 发布。本目录发布的是派生测量记录，包含图片哈希、独立标注金额、各路线的输出和耗时。派生数据沿用 CC BY 4.0；未收录原图或原始全文。本研究对金额进行了格式归一化，并另行生成策略输出。[原作者项目](https://github.com/clovaai/cord) · [固定数据版本](https://huggingface.co/datasets/naver-clova-ix/cord-v2/tree/7f0115a4b758a71d6473b8d085751692da2fef98) · [数据许可](https://creativecommons.org/licenses/by/4.0/)

## 如何复现

在仓库根目录执行以下命令。重放已发布的测量只需要 Python 标准库。第一条命令仅读取官方 validation 对应的校准集，固定模型和对照路线。第二条命令在官方 test 对应的留出集打分，并核对校准集、协议与冻结模型的摘要。

```sh
python -m scripts.evaluate_verification_value examples/verification-value/calibration.json --protocol examples/verification-value/protocol.json --freeze-only --frozen-out frozen-local.json
python -m scripts.evaluate_verification_value examples/verification-value/calibration.json --holdout examples/verification-value/holdout.json --protocol examples/verification-value/protocol.json --frozen-in frozen-local.json --output report-local.json
```

重新采集耗时需要 Python 3.11+、pyarrow 20.0.0、Pillow 11.3.0、tesserocr 2.13.0、上面固定版本的原始 Parquet 文件以及 Tesseract 英语字库。设置 `OMP_THREAD_LIMIT=1`，用 `python -m scripts.measure_cord_verification calibration VALIDATION.parquet TESSDATA_DIR calibration.json` 重测。留出集使用参数 `holdout` 及官方 test 文件。采集器检查 Parquet 和字库 SHA-256。耗时包含预热引擎的图像缩放、OCR 和金额解析；共享的图像解码、启动引擎和服务排队不计入。不同硬件或负载会改变耗时。费用 1、2、4 是假设报价，未调用付费服务。[固定英语字库](https://github.com/tesseract-ocr/tessdata_fast/blob/4.1.0/eng.traineddata)

## 判定口径

协议在读取留出标签前固定。单据按来源文件及原图哈希检查重复；没有商户 ID 可用于保证商户不重叠。标注缺少明确、唯一、可解析的总额时排除，并记录编号。识别未作答、预算拒绝和超时均保留在分母中。费用包含所有已派发的尝试。每单时限由图片摘要决定，与答案无关。留出集用于一次性评价，此版本的后续研究应使用新数据做独立确认。

校准过程保留逐单配对，分别统计核验纠正和破坏了多少个结果，按时正确率随剩余时间变化。策略用 DKW 同时下界与多组校正控制抽样不确定性；这些界限依赖组内独立且有代表性的校准样本。先筛除预算、时间或质量下界达不到要求的路线，再按预计每份按时正确交付的费用排序。估计不足时仅在配置允许时执行明确记录的固定后备路线。已有足量证据但没有合格路线时停止。真实业务还需单独验证数据分布、实际账单与排队延迟。

## 冻结后的结果

提交 `b637023` 固定了 OCR 提取器、选择规则、门槛、校准数据、对照路线及模型。校准集 100 张中有 98 张可评分，留出集 100 张中有 95 张可评分。其余 7 张缺少可按预设方式解析的唯一标注总额。校准阶段选定的固定对照为直接读取。

| 路线 | 按时正确 / 95 | 错误交付 | 未交付 | 总测试费用 |
|---|---:|---:|---:|---:|
| 直接读取 | 37 | 33 | 25 | 95 |
| 读取并核对 | 16 | 5 | 74 | 190 |
| 双次读取原图 | 4 | 0 | 91 | 200 |
| 自适应选择 | 0 | 0 | 95 | 0 |

留出集的门槛是至少 67 张按时正确且至多 9 张错误交付。所有策略均未达标。自适应策略的每份按时正确交付成本无法计算。校准集两组分别有 95 和 3 张，留出集两组分别有 94 和 1 张。常规组的质量下界不足，唯一的高窄票据缺少足够校准样本，且固定后备路线的预计时长超过其剩余时限。

对逐单结果的事后诊断显示，仅有 **40/95** 张小票在至少一条准入的固定路线上按时且正确。因此，在本实验限定的三条路线、预算、时限和每单一次选择的条件下，即使事先知道正确答案，正确挑选路线也达不到 67 张。这一结论适用于这批小票；更换识别器、增加新证据或扩展多步核验后需要重新评测。[冻结模型](frozen.json) · [逐单结果](report.json)
