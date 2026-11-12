# 依赖锁定说明（UM 分享云端）

本文件是 `dependencies.lock.json` 的人类可读说明；机器可读版本以 JSON 为准。

## 目标

- OS：Ubuntu 26.04 LTS（resolute），amd64。
- Python：CPython 3.14（`python3.14`）。
- 不假设 WSL 的 Ubuntu 24.04 / Python 3.12；`bootstrap-wsl.sh` 只用于 WSL 隔离验证，
  不是云机安装器。

## Python 直接依赖

| 包 | 版本 | 用途 |
| --- | --- | --- |
| requests | 2.34.2 | 8110 上游 HTTP、Caddy/管理读面 |
| playwright | 1.64.1 | 8110 的持久 profile 浏览器登录 |
| PyYAML | 6.0.3 | 配置读取 |
| cryptography | 50.0.2 | share secret AES-GCM |

传递依赖：`certifi 2026.10.19`、`charset-normalizer 3.5.2`、`idna 3.20`、
`urllib3 2.8.0`、`greenlet 3.5.6`、`pyee 14.0.0`、`typing-extensions 4.16.0`、
`cffi 2.1.1`、`pycparser 3.12`。

`requirements.lock` 只列直接依赖；离线 wheelhouse 必须包含以上全部。生成命令：

```bash
python3.14 -m pip download --only-binary=:all: \
  --platform manylinux2014_x86_64 --implementation cp --python-version 3.14 \
  --abi cp314 -r requirements.lock -d wheelhouse
```

## Playwright / Chromium

- Playwright 1.64.1 → chromium revision 1248 / Chrome for Testing 156.0.8078.4。
- 浏览器安装目录固定 `/opt/um-share/pw-browsers`，不属于 release。
- Linux 侧必须重新 `playwright install chromium`；不能复制 Windows 的
  `%LOCALAPPDATA%\ms-playwright`。

## Ubuntu 26.04 系统包

`dependencies.lock.json` 的 `apt.packages` 是从 resolute main/universe 索引
（2026-11-01）解析的精确版本。安装脚本按包名安装；严格复现时使用
`apt-get install pkg=version` 或 apt snapshot。

**未验证**：当前开发机不是 Ubuntu 26.04，未在目标机执行 apt 解析、Caddy 2.6.2
配置检查或 Chromium 沙箱验证。
