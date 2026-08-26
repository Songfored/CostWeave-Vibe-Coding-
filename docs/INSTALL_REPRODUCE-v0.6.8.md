# CostWeave v0.6.8 安装与复现

## 环境

- Python 3.11 或更高版本；
- 构建环境需要已安装 `pip`、`setuptools` 和 `wheel`；
- 无必需运行时第三方依赖；
- 不需要 API Key，不允许真实 Provider 调用。

## 从源码运行

```bash
python -B -m costweave.server
```

打开服务打印的完整本地地址。当前版本由 `costweave/version.py` 提供；不要分别修改网页、CLI 或 `pyproject.toml`。

## 生成干净发布物

在仓库根目录执行：

```bash
python -B -m costweave.release_packaging --clean --pretty
```

构建器把白名单源码复制到 `work/release-v0.6.8/staging/`，并在 `dist/v0.6.8/` 生成：

- `costweave-0.6.8-py3-none-any.whl`；
- `costweave-0.6.8.tar.gz`；
- `CostWeave-v0.6.8.zip`；
- `release-manifest.json`；
- `SHA256SUMS.txt`。

wheel 由 `pip wheel --no-build-isolation` 在 staging 的父目录构建，源码 tar 和 zip 由标准库从同一白名单文件集生成，因此根目录遗留的 `build/` 不会被导入或进入归档。构建器只会在带有自身标记文件时清理既有 staging 和输出目录，不会删除未标记的用户目录。

## 干净安装 wheel

Windows：

```powershell
python -m venv work/v068-clean-venv
work\v068-clean-venv\Scripts\python.exe -m pip install --no-deps dist\v0.6.8\costweave-0.6.8-py3-none-any.whl
work\v068-clean-venv\Scripts\python.exe -B -m costweave.release_cli --pretty
```

Linux / macOS：

```bash
python -m venv work/v068-clean-venv
work/v068-clean-venv/bin/python -m pip install --no-deps dist/v0.6.8/costweave-0.6.8-py3-none-any.whl
work/v068-clean-venv/bin/python -B -m costweave.release_cli --pretty
```

期望：版本为 `0.6.8`，发布门禁全部通过，`engineering_ready=true`，`production_ready=false`，真实外部调用为 0。

## 校验哈希

在 `dist/v0.6.8/` 中逐项计算 SHA256，并与 `SHA256SUMS.txt` 对比。`release-manifest.json` 同时记录归档成员检查结果、文件大小和构建边界。

## 证据边界

本流程只证明 E0 本地工程合同、干净打包和可安装性；不证明真实模型质量、真实账单、生产容量、长期稳定性或固定降本比例。
