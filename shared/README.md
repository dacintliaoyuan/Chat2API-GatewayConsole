# shared：双端共用的纯协议与纯规则

本目录随本机（local）与云端（cloud）两个应用分别打包，只放双端实际消费、且导入时没有运行时副作用的代码：

- 纯协议与参数转换（例如 Responses／Messages 请求与响应映射）；
- SSE 解析与聚合；
- 每请求独立创建的有界解析器；
- 必要的数据类型与错误类型；
- 纯规则（时间、模型标识、计价等计算）。

## 不放什么

以下内容归各端自己，不能放进 `shared`：

- 宿主、路由注册、认证与生命周期；
- 可变状态、缓存、全局注册表与服务实例；
- 文件读写、路径解析与配置加载；
- 网络、HTTP 客户端、浏览器与进程；
- Key 库、凭据读取与撤销缓存；
- broker、Unix socket、排空与 ACK 链；
- `OperationManager`、线程池、锁与幂等账本。

## 导入约束

- `shared/__init__.py` 只保留 docstring，不 import 子模块；
- 子模块只允许导入标准库中的纯计算模块或 `shared` 内部模块；
- 导入 `shared` 不得把宿主或业务模块带进 `sys.modules`。

## 两条可注入的纯规则

`shared` 里的规则不接受调用者改写模块常量，也不把"当前时间／随机 id"藏成不可控的副作用：

- **准入字段集合不可变**：`responses_protocol.RESPONSES_FIELDS` 与 `MESSAGES_FIELDS` 是 `frozenset`。它们决定请求是否被接受，可变的 `set` 会让任意导入方放宽全进程的准入面。需要不同准入面的调用者给 `valid_responses_request(data, allowed_fields=...)` 传自己的集合，模块常量不受影响。
- **响应身份可注入**：`chat_to_responses_response` / `messages_to_responses_response` 接受可选关键字参数 `created_at=None, response_id=None`。默认仍逐次用 `time`／`secrets` 生成，行为不变；显式传入时使用调用者值，便于 fixture、回放与断言。**注入范围仅限顶层**：只有响应对象自身的 `id`／`created_at` 被覆盖；`output[].id` 的 `msg_*`／`fc_*`（`responses_protocol.py` 的流式翻译在 `:729`／`:942`／`:1041` 一带逐次生成）与流式事件内层 id 仍逐次随机。任何"注入后逐字节比对"的 fixture／replay 断言必须排除这些字段，或改为只比对顶层 `id`／`created_at`。

## 跨端公开名

`pricing_rules` 原先以私有拼写（前导下划线）暴露十个函数，跨模块消费等于伸手进另一个包的实现细节；现在每个都有公开名，旧私有名保留为**同一函数对象**的别名（不是包装、不是复制），因此改名前后行为逐字节一致。

**实测的双端消费面（2026-11-10 复核）**：本地端 `local/backend/stats_env.py` 只调用其中 6 个公开名（`beijing_day`、`day_start_ms`、`estimate_record`、`normalize_price_table`、`parse_ts`、`match_model_id_uncached`）；云端 `cloud/backend/summary.py` 只用 `DEFAULT_EXCHANGE_RATE` 与 `version_at` 两个公开名。因此「十个函数被两个应用同时调用」的说法高估了实际复用：`beijing_weekday`／`strip_id_decor`／`tokens_of`／`version_id`／`price_version`／`peak_of`／`rate_of`／`tiers_of`／`current_version`／`checked_age_days` 中的多数只在 `shared` 内部或测试里出现。

| 公开名 | 私有别名 | 做什么 |
|---|---|---|
| `beijing_weekday` | `_beijing_weekday` | 北京时间日序 → 星期（0=周日） |
| `checked_age_days` | `_checked_age_days` | `checkedAt` 距今天数，不可解析返回 `None` |
| `current_version` | `_current_version` | 当前生效的价目表版本段 |
| `peak_of` | `_peak_of` | 峰谷规则归一化（`enabled`／`effectiveAtMs`／`windows`） |
| `price_version` | `_price_version` | 一个显式版本段 → 内部查找结构 |
| `rate_of` | `_rate_of` | 汇率兜底到 `DEFAULT_EXCHANGE_RATE` |
| `strip_id_decor` | `_strip_id_decor` | 去模型 id 的日期快照与版本后缀 |
| `tiers_of` | `_tiers_of` | 价表扁平成 `[(models, billingMode, source)]` |
| `tokens_of` | `_tokens_of` | 模型 id 按分隔符切词 |
| `version_id` | `_version_id` | 版本标识（显式 `versionId` 优先） |

改名进度：`usagesrv.py` 全部十个由 E19-1-02 改、`local/backend/stats_env.py` 六个由 E19-1-04 改；`cloud/backend/summary.py` 本来只用公开名。别名在这两处改名完成后才能删，且删除时必须先确认无其他消费者。

## 为什么 `threading` 不在运行时禁止列表

导入 `shared.pricing_rules` 的运行时纯净性检查断言 `usagesrv`／`chat2api`／`console_adapters`／`share_management`／`model_sync` 不在 `sys.modules`，但**不**断言 `threading` 缺席：原版 Ubuntu 的 Python 会在任何用户代码执行前由 `sitecustomize`／`apport_python_hook` 预导入 `threading`（在 UM-Cloud-P1 上用 `-I` 子进程实测，只 import `shared.pricing_rules` 时 `apport_python_hook`、`sitecustomize`、`threading` 都已在模块表）。这是解释器启动行为，不是本模块的副作用。

源码层面的规则仍然绝对：`shared` 的任何 `.py` 不得出现 `import threading`（静态 AST 用例检查 import 语句，与解释器预加载无关）。运行时例外只放宽"解释器替我导入了什么"，不放宽"我导入了什么"。


