# 编码标准（review 用）

本文件只写 review agent 需要执行的判断规则；能用 `scripts/check.sh` 机械检查的，不在这里重复。

## 分享链

- 改分享链名字前先跑 `python scripts/check_share_names.py`；它失败说明旧链/新链混了。
- 生产 8100 只运行 `chat2api/chat2api.py`；纯 helper 在 `chat2api/share_auth.py`。
- `share_gateway` 是读面组件键，不要改成 `share_auth`；模块名是 `share_auth`。
- `chat2api.py` 只在 `--share-config` 分支内延迟 import `share_auth`，顶层 import 视为违规。
- 旧链/新链的完整映射见 `docs/share-chain-map.md`；改名前先读它。

## Python

- 新代码不得出现 `except ...: pass`；必须写明补偿的缺陷，或记录后继续。
- 环境变量解析必须捕获 `TypeError`/`ValueError` 并给默认值；不要让配置错误在 import 时炸掉进程。
- 成功、合法空结果、用户取消、暂时失败、配置错误、数据损坏要可区分；不用一个布尔压平。
- 失败关闭的路径必须返回可读原因；不要用「看起来没问题」补齐未知。

## 文档与证据

- 运行过测试、构建或脚本时，记录命令、工作目录、版本或状态、实际结果与未验证范围。
- 本次回归、既有失败和环境缺失分别报告；不把未跑过的检查写成交付证据。
- 生成物走生成器；契约夹具用 `python tests/generate_frontend_contract_fixture.py` 重新生成，不手改。
