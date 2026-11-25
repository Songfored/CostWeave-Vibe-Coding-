# 从小票照片读取应付总额

这次实验比较同一批印度尼西亚小票上的两代读取器。上一轮发现，有些照片在三条路线中都得不到正确金额。这一轮保留原有调度器、费用和交付标准，改进从照片到候选金额的过程。[上一轮实验](../verification-value/README.md) · [本轮预先登记的协议](protocol.json)

## 读取自己的图片

在仓库根目录安装可选的 OCR 依赖，将 Tesseract 的 `eng.traineddata` 放入本地目录后执行以下命令。拍摄内容在本机处理。[实验使用的字库](https://github.com/tesseract-ocr/tessdata_fast/blob/4.1.0/eng.traineddata)

```sh
python -m pip install ".[receipt-ocr]"
python -m costweave receipt-total receipt.jpg --tessdata ./tessdata --route check
```

若读出 88,000 印尼盾，输出中的 `amount_hundredths` 为 `8800000`，`currency` 为 `IDR`。退出码 0 表示给出候选答案，2 表示证据不足而放弃（金额为 `null`），1 表示图片、字库或运行环境有错误。命令直接运行指定路线。下文的预算、时限和自适应选择在策略评测中生效。

| 路线 | 使用哪些信息 |
|---|---|
| `direct` | 找出票面，校正阴影和倾斜，按词的位置拼回印刷行。一次 OCR 后根据总额标签、金额列和算术关系选择候选。 |
| `check`（默认） | 分别读取增强图和保留原光照的灰度图。综合总额、实收减找零、小计加税费减折扣等证据，最有支持的两个金额过于接近时放弃。 |
| `original` | 对增强图使用两种版面识别方式，并读取保留原光照的灰度图。采用更严格的证据和领先幅度要求。 |

同一张纸上的多次 OCR 可能共同漏掉一位数字。金额之间的关系提供额外的核对线索，但这些路线给出的仍是候选金额。

## 数据和判定

开发阶段只使用上一轮的官方 validation 图片。新实验在官方 train 的第 0、1 分片上分别校准两代策略，分别选出各自的固定对照路线。两套策略冻结后，再用第 2 分片检验。第 3 分片未使用。对官方 validation、test 和本次各分片先后比对原图字节及解码 RGB 像素，排除完全相同的图像。相同商户或版式仍可能跨组出现。

完整照片只交给读取器，标注总额用于事后判分。缺少唯一可解析总额的照片在两版中同时排除。未作答、超时、预算或时限不足均计入可评分小票总数。实验要求至少 70% 的小票按时答对，且错误交付占比至多 10%。报价 1、2、4 均为测试单位。耗时包含预热后的图像预处理、OCR 和金额判定，图像解码、引擎启动与排队另计。

图片和独立标注出自 Park 等人及 NAVER CLOVA 的 CORD。派生记录以 CC BY 4.0 发布，含图片摘要、归一化标注金额、读数及耗时。原图可从固定数据版本获取。[作者项目](https://github.com/clovaai/cord) · [固定分片](https://huggingface.co/datasets/naver-clova-ix/cord-v2/tree/7f0115a4b758a71d6473b8d085751692da2fef98/data) · [许可证](https://creativecommons.org/licenses/by/4.0/)

## 重放与重新采集

从仓库根目录执行。用 Python 3.11+ 的标准库即可重放已发布的逐单测量，命令会检查两代读取器是否使用相同的单据、预算、时限和标注，以及冻结文件是否匹配。

```sh
python -m scripts.compare_receipt_readers --protocol examples/receipt-readers/protocol.json --old-calibration examples/receipt-readers/old-calibration.json --new-calibration examples/receipt-readers/new-calibration.json --old-freeze examples/receipt-readers/old-frozen.json --new-freeze examples/receipt-readers/new-frozen.json --old-holdout examples/receipt-readers/old-holdout.json --new-holdout examples/receipt-readers/new-holdout.json --output comparison-local.json
```

要在自己的硬件上重测，将上述三个 train 分片按原名保存，官方 validation 和 test 文件分别命名为 `validation.parquet` 和 `test.parquet`，与 `eng.traineddata` 放在 `sources/`。本次采集使用 Python 3.11、PyArrow 20.0.0、Pillow 11.3.0、tesserocr 2.13.0 和 Tesseract 5.5.4。设置环境变量 `OMP_THREAD_LIMIT=1`。采集器检查输入摘要，逐张交替安排两版的先后顺序。若更换机器，应使用新采集的校准及留出文件重新比较，耗时与原记录可能不同。

```sh
python -m scripts.measure_receipt_readers calibration sources measured
python -m scripts.evaluate_verification_value measured/old-calibration.json --protocol examples/receipt-readers/protocol.json --freeze-only --frozen-out measured/old-frozen.json
python -m scripts.evaluate_verification_value measured/new-calibration.json --protocol examples/receipt-readers/protocol.json --freeze-only --frozen-out measured/new-frozen.json
python -m scripts.measure_receipt_readers holdout sources measured
```
