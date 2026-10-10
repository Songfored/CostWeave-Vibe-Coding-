# CostWeave v0.6.9 安装与复现

本文用于发布工程和冻结产物复现。首次在 Windows 本机运行项目，请先阅读[本地环境与真实模型评测指南](REAL_MODEL_EVALUATION.md)。

本机模型渠道使用 `.\scripts\configure-models.ps1` 自动配置。该脚本只进行模型发现，不属于发布复现，也不会启用付费推理。

## 从源码运行

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -m costweave.release_readiness
python -m costweave.release_packaging --output dist/v0.6.9
```

## 安装发布 wheel

```powershell
python -m venv work/v069-clean-venv
work\v069-clean-venv\Scripts\python.exe -m pip install --no-deps dist\v0.6.9\costweave-0.6.9-py3-none-any.whl
work\v069-clean-venv\Scripts\costweave.exe --help
```

发布目录应包含 wheel、源码 tar、源码 zip、`release-manifest.json` 与 `SHA256SUMS.txt`。应使用清单核对精确哈希。

预期边界：版本 `0.6.9`，发布门禁通过，真实外部调用为 0，`production_ready=false`。
