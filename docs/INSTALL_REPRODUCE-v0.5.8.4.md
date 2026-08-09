# CostWeave v0.5.8.4 安装、迁移、回滚与复现

## 环境

- Python 3.11 或更高版本；
- Windows、macOS 或 Linux；
- 默认只绑定 `127.0.0.1`；
- 不需要 API Key，不会默认调用外部模型。

## Wheel 安装

```bash
python -m venv .venv
.venv/bin/python -m pip install costweave-0.5.8.4-py3-none-any.whl
.venv/bin/costweave-release --pretty
.venv/bin/costweave --host 127.0.0.1 --port 8765
```

Windows 将 `.venv/bin/` 换成 `.venv\\Scripts\\`。

`costweave-release --pretty` 会在临时目录创建全新数据库，执行一个离线任务，逐个重放全部运行，创建并验证逻辑备份，并确认外部调用尝试为 0。任何一步失败都会返回非零退出码。

## 源码运行

```bash
python -B tests/run_isolated.py
python -B -m costweave.release_cli --pretty
python -B -m costweave.server --host 127.0.0.1 --port 8765
```

## 从 v0.5.8.3 升级

1. 停止服务并复制现有 SQLite 数据库。
2. 安装 v0.5.8.4。
3. 使用原来的 `COSTWEAVE_RUNTIME_DB` 启动。
4. 确认 `/api/health` 返回 Runtime Schema `v5.8.4`、migration 29。
5. 确认 `/api/v1/release/status` 的全部运行时硬门通过。
6. 在副本环境执行备份与恢复演练。

migration 029 只新增不可变发布证明表和触发器，不改写既有运行、事件或 Artifact。

## 回滚

应用代码可以停止并回滚，但 migration 029 已应用的数据库不应交给旧版本继续写入。需要完整回滚时，恢复升级前数据库备份。不要手工删除 `schema_migrations` 或发布证明行。

## 可复现证据

发布包同时提供 wheel、源码包、运行包、SHA-256 校验和、Benchmark、模型卡、策略卡、数据卡、安全报告和验收报告。先校验文件哈希，再执行全新安装验证。

## 明确边界

复现成功只证明离线工程合同。它不会触发真实 Provider，不证明真实答案质量、成本比例、P95、生产可用性或商业领先。
