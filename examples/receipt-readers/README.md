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

## 独立测试结果

提交 `bda1419` 登记了评测方法，`d616472` 固定了读取算法。`9066d59` 在读取留出集之前固定了两套校准策略。校准集的 400 张照片中有 2 张重复、8 张缺少唯一可解析标注，其余 390 张参与校准。独立留出集 200 张中有 9 张缺少唯一可解析标注，其余 **191 张**逐张比较。[旧版冻结文件](old-frozen.json) · [新版冻结文件](new-frozen.json) · [对照结果](comparison.json)

下表将每张图的实测答案、耗时及测试报价送回执行内核，用预先冻结的策略重放交付过程。

| 策略 | 按时正确（旧 → 新） | 错误交付（旧 → 新） | 未交付（旧 → 新） | 总测试费用（旧 → 新） |
|---|---:|---:|---:|---:|
| 直接读取 | 78 → 110 | 73 → 53 | 40 → 28 | 191 → 191 |
| 交叉核对 | 21 → 108 | 15 → 34 | 155 → 49 | 382 → 382 |
| 严格复读原图 | 13 → 49 | 2 → 5 | 176 → 137 | 372 → 372 |
| 自适应选择 | 0 → 0 | 0 → 0 | 191 → 191 | 4 → 4 |

直接读取的按时正确率从 **40.8% 升至 57.6%**，每份按时正确答案的费用从 2.45 降至 1.74 个测试单位。逐张对照，直接读取新增 50 张正确、丢失 18 张正确。新版交叉核对保住旧版该路线全部 21 张正确的结果，又答对 87 张，但错误交付也净增了 19 张。校准时预先选出的对照为旧版直接读取和新版交叉核对。两者在留出集上分别答对 78 和 108 张，错误交付分别为 73 和 34 张。

按协议，191 张中至少要 **134 张按时正确，同时最多只能有 19 张错误交付**。两代读取器的固定策略与自适应策略都未通过。自适应策略在两版中各尝试过一次后备路线，都没有交付金额。严格复读在计时与派发限制之外能读对 106 张，但其预估耗时为 1,800 毫秒，只有 93 张的时限允许派发，最终按时读对 49 张。

逐张事后检查，至少一条可派发路线能按时答对的单据从 **79 张增至 134 张**。这个上限恰好等于门槛，即使事先知道各路线结果，也要把这 134 张全部选对才能达到正确交付目标。还有 57 张在现有路线与时限下均无法正确交付。扩大正确结果的覆盖面和在运行时判断何时值得核对，都仍有必要。留出集只能用于这次独立检验。

命令行实测中，官方 validation 首张照片的标注为 45,500 印尼盾，默认核对路线给出 45,200。空白图片正确返回放弃。对同一张照片复读仍可能给出相同的错误数字。

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
