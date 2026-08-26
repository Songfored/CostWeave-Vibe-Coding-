# CostWeave v0.6.7 安装与复现

## 环境

- Windows、Linux 或 macOS；
- Python 3.11 或更高版本；
- 运行时无必需第三方依赖；
- 不需要 API Key，不需要联网，不产生真实外部调用。

## 从源码验证

在源码根目录执行：

```text
python -B -m compileall -q costweave tests
python -B -m unittest -v tests.test_security_recovery_v067
python -B -m unittest discover -s tests -p "test_*.py"
python -B tests/run_isolated.py --timeout 240
python -B -m costweave.release_cli --pretty
```

期望：1058 项全量和隔离测试均为 0 失败；发布门禁 19/19；版本 v0.6.7；外部调用 0；`engineering_ready=true`；`production_ready=false`。

## 本地启动

```text
python -m costweave.server
```

终端会打印包含 `#session=...` 的完整回环地址。第一次必须打开完整地址；网页会把令牌写入当前标签页的会话存储并从地址栏删除。未携带令牌的 API 请求应返回 401。

不要把服务绑定到非回环地址，也不要通过公网反向代理暴露。

## 构建与安装

仓库当前含有历史 `build/` 目录，它会遮蔽同名 Python 构建包。发布构建应从只包含源码、`pyproject.toml`、README 和许可证的干净 staging 目录执行：

```text
python -B -m pip wheel . --no-deps --no-build-isolation --no-cache-dir --wheel-dir dist
```

将 wheel 安装到新虚拟环境后，再从不含源码目录的工作目录运行：

```text
python -B -m costweave.release_cli --pretty
```

历史候选构建哈希记录在[验收报告](ACCEPTANCE-v0.6.7.md)。修复后安全复扫已经完成，但这些包早于最新源码修复，仍不应作为当前正式版上传 GitHub Release；必须重新构建、干净安装、复跑门禁并生成新哈希。

## 后续复扫前清理

标准扫描目标不应包含 `work/`、`outputs/`、虚拟环境、wheel、sdist 和历史安装副本。先把这些目录移出扫描目标或使用干净源码副本，再在新任务中启动全仓标准安全扫描。
