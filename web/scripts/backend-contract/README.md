# 后端契约夹具与前端接线检查

`src/console/backend/` 只负责契约、请求与状态，不包含 DOM、CSS、图表或布局。本目录的检查把后端真实处理器的输出与前端契约夹具对照，防止两边漂移。

## 入口

```bash
cd web
npm run check:contracts
```

该脚本依次运行本目录的 `check*.mjs`，覆盖：

- 模型目录与上下文读面；
- 保存、预览、应用与读回；
- 整组延期、快照冲突与操作错误；
- 日志、Key 名称与删除历史；
- 分享用量、价格版本与未归属记录；
- 云端用量账本与分享读面截止时间。

单个检查也可以直接运行：

```bash
node --experimental-strip-types --test scripts/backend-contract/check.mjs
```

## 夹具

输入是 `tests/fixtures/frontend_contract_rev25.json`，来源是 `tests/generate_frontend_contract_fixture.py`。生成器调用真实 registry／处理器／事务／用量投影与日志 reader，但客户端、账本、价格、Key、模型、状态与凭据边界全部使用私有合成 fixture；`metadata.fixture=true`，并带后端源码 SHA256。

在仓库根重新生成：

```bash
python -X utf8 tests/generate_frontend_contract_fixture.py
```

检查器会拒绝与当前处理器不一致的过期夹具。检查不访问生产服务。
