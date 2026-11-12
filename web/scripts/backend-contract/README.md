# 后端数据模块与前端接线

本目录由后端与接线任务维护。`src/console/backend/` 仅负责契约、请求和状态，不包含 DOM、CSS、图表或布局。前端任务负责页面设计与渲染，复用这些数据和操作接口。既有页面尚未全部改用此模块；本次唯一现有页面代码增量是同步范围的编码与移除旧组合动作参数。

## 模块入口

从 `src/console/backend/index.ts` 导入：

- `BackendClient`：同源 API 的能力查询、操作提交／轮询／协作取消，模型、上下文、日志和分享用量读取。提交结果不确定时不重发，保留操作 ID，不能显示为已失败或已成功。`readGatewayConnection` 是选中网关连接信息的窄直读：不进操作账本，明文调用 Key 只在读取结果对象里。
- `GatewayConnectionBinding`：当前选中网关连接信息的单槽所有者。切换网关、取消或离开页面立即丢弃上一个明文 Key；迟到的响应不会写进新网关（同一代际守卫），响应身份与服务不符时拒绝安装。
- `BackendSession`：模型、日志、分享数据的状态所有者。刷新失败保留上次值并标陈旧；换来源、取消及销毁后阻止迟到结果覆盖。
- `createFetchTransport({fetch})`：显式真实网络适配器，构造时不发请求。管理操作沿用 HttpOnly 会话及 `X-Requested-With`，不接收、持久化或输出任何管理令牌。
- `FixtureTransport`：完全离线场景。模拟请求只访问所传 JSON，不能连接真实网关或改客户端。
- `copyGatewayConnectionField`：网关连接读面的窄复制决策。只接受 `baseUrl`/`keyPlaintext`；无明文 Key 或剪贴板写入失败返回明确失败结果与原因，绝不提示成功；不写日志、localStorage 或任何持久化。
- `normalize*`、`joinModelDetails`、`encodeSyncScope`：严格数字证据、真实来源＋模型 ID 合并、全部／选中范围编码；空选拒绝，不隐式同步全部。

真实读取由页面显式创建 transport 后发起；生产代码接线与生产构建发布分开。不能因存在模块或候选包就写“正式页面全部已接入”。

## 数据与动作约定

1. 模型目录、设置读面、上下文证据分别读取后按 `source|model_id` 合并。声明值、实测 Token 下界、字符证据和历史各自展示；`compat_default` 输出上限只表示兼容默认。
2. 设置先 `saveSettings` 保存草稿，再 `previewSettings(selection)`，最后用同一选择 `applySettings(preview, selection)`。恢复字段用 `null`，局部保存不发送其他字段。预览 token、草稿版本或客户端快照变化时重新预览。`applied`、`deferred` 互斥。
3. `applySettingsAndReadback` 把应用结果和再次实读分开；延期不冒充读回成功，读回失败不抹去已经完成的应用。`clientReadbacks` 是每个消费方文件中读出的值，不能当运行中应用的加载证据。
4. `readLogs` 的 `request_id` 是精确结构化 ID；`q` 是文本搜索，二者独立。`requestScope=read_window` 只覆盖本页读取窗口，不表示全文件请求链已齐。结构化字段未知保持 null，原始脱敏文本保留。
5. 每 Key 使用稳定 `keyId` 及 `keyName`；名称不是归因标识。轮换仍沿同一 ID，删除历史独立保留。模型／日期／无日期明细使用各自的覆盖与价格版本；`tokenUsage` 将完整、下界、估算、未知分开，金额不能因为有数值就被称为完整费用。
6. 不由读取、同步、保存或构造客户端自动发起计费探测。`runAction` 要求明确动作意图；任何真实探测仍遵守维护者给出的窗口和预算。
7. 网关连接信息读面（Rev15）只返回快照端口与描述符常量拼出的 Base URL、协议、鉴权方式与调用 Key 读取状态；`keyState` 与 Python 读面逐一对应，`unknown` 不降级成「没有」。`keyInUse` 如实区分「配置文件值」与「实例已确认使用值」：已知状态必须给出已知机制（`unknown` 只与真实文件来源核对失败等诚实组合并存），`absent` 只来自 chat2api 实例记录，按 Python 读面保持 `startup_env`。明文 Key 只在 `present` 时出现在读取结果对象里，不进日志、错误文本或任何持久化；夹具里的明文是合成哨兵值，绝不是真实 Key。

## 真实处理器生成的 Demo 夹具

输入为 `ai-gateway/tests/fixtures/frontend_contract_rev25.json`，来源为 `ai-gateway/tests/generate_frontend_contract_fixture.py`。生成器调用真实 registry／处理器／事务／用量投影和日志 reader，但客户端、账本、价格、Key、模型、状态与凭据边界全部使用私有合成 fixture。`metadata.fixture=true`，并带后端源码 SHA256。

包含目录已知／未知、保存／真实预览／应用、实际客户端读回／不可读、整组延期、快照冲突、探测历史、Key 名称／删除历史、单侧／估算／真实零／无价／无日期／未归属记录、运行事件。冲突样例是处理器抛出的 `OperationError` 字段，不是凭空编造的 HTTP 响应。源码哈希按 UTF-8、LF 归一计算；检查器会拒绝与当前处理器不一致的过期夹具。

在 `SelfUseAI` 根重新生成：

```powershell
python -X utf8 ai-gateway/tests/generate_frontend_contract_fixture.py
```

在 `ai-gateway/web` 验证：

```powershell
node --experimental-strip-types --test scripts/backend-contract/check.mjs
node --experimental-strip-types scripts/backend-contract/check-generated.mjs
npx tsc --noEmit
```

检查不访问生产服务。正式样式、页面布局、各类图表及生产发布仍由前端 handoff 安排；本模块不替换前端 AI 的源码。
