"""Start the local console independently, then open a verified session URL."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit, parse_qs
import webbrowser

from .experiment_lab import protect


def live_session(root):
    try:
        url = protect((root / "session.dpapi").read_bytes(), decrypt=True).decode()
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.username or parsed.password or parsed.path != "/" or parsed.query:
            return None
        token = parse_qs(parsed.fragment).get("session", [""])[0]
        request = urllib.request.Request(f"http://127.0.0.1:{parsed.port}/api/v1/lab/automation",
                                         headers={"X-CostWeave-Session": token})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=1) as response:
            value = json.loads(response.read(2_000_000))
        return url if isinstance(value.get("rounds"), list) and "policy" in value else None
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path(__file__).resolve().parents[1] / "work/manual-lab")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    root = args.data.resolve()
    root.mkdir(parents=True, exist_ok=True)
    url = live_session(root)
    if not url:
        env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        with (root / "launcher.log").open("ab") as log:
            child = subprocess.Popen([sys.executable, "-B", "-m", "costweave.experiment_lab", "--no-browser", "--data", str(root)],
                                     cwd=Path(__file__).resolve().parents[1], env=env, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=log, close_fds=True,
                                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            url = live_session(root)
            if url:
                break
            if child.poll() not in (None, 0):
                break
            time.sleep(0.25)
    if not url:
        print(f"实验台未能启动。诊断日志：{root / 'launcher.log'}", flush=True)
        return 1
    print("实验台已就绪，关闭启动窗口不会停止服务。\n" + url, flush=True)
    if not args.no_browser and not webbrowser.open(url):
        print("浏览器未自动打开，请复制上方完整地址。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
