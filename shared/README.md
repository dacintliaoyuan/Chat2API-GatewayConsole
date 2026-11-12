# shared：双端共用的纯协议与纯规则

`shared/` 随本机（local）与云端（cloud）两个应用分别打包，只放双端实际消费、导入时没有运行时副作用的代码：请求/响应映射、SSE 解析与聚合、每请求独立的有界解析器、数据类型与错误类型、纯计算规则（时间、模型标识、计价）。

## 边界

以下归各端自己，不放进 `shared/`：宿主、路由注册、认证与生命周期；可变状态、缓存、全局注册表与服务实例；文件读写、路径解析与配置加载；网络、HTTP 客户端、浏览器与进程；Key 库、凭据读取与撤销缓存；broker、Unix socket、排空与 ACK 链；`OperationManager`、线程池、锁与幂等账本。

## 导入约束

- `shared/__init__.py` 只保留 docstring，不 import 子模块。
- 子模块只导入标准库中的纯计算模块或 `shared` 内部模块。
- 导入 `shared` 不把宿主或业务模块带进 `sys.modules`。

## 两条可注入的纯规则

`shared` 里的规则不接受调用者改写模块常量，也不把“当前时间／随机 id”藏成不可控的副作用：

- **准入字段集合不可变**：`responses_protocol.RESPONSES_FIELDS` 与 `MESSAGES_FIELDS` 是 `frozenset`。需要不同准入面的调用者给 `valid_responses_request(data, allowed_fields=...)` 传自己的集合，模块常量不受影响。
- **响应身份可注入**：`chat_to_responses_response` / `messages_to_responses_response` 接受可选关键字参数 `created_at=None, response_id=None`。默认逐次用 `time`／`secrets` 生成；注入范围仅限响应对象自身的顶层 `id`／`created_at`，`output[].id` 与流式事件内层 id 仍逐次随机。逐字节比对的 fixture/replay 断言必须排除这些字段，或只比对顶层 `id`／`created_at`。

## 跨端公开名

`pricing_rules` 的每个跨模块函数都有公开名；旧私有名保留为同一函数对象的别名（不是包装、不是复制），改名前后行为逐字节一致。本机端 `local/backend/stats_env.py` 与云端 `cloud/backend/summary.py` 使用其中的公开名；完整名单与用途见 `shared/pricing_rules.py` 的导出表。

## 解释器预加载

导入 `shared.pricing_rules` 的运行时纯净性检查断言 `usagesrv`／`chat2api`／`console_adapters`／`share_management`／`model_sync` 不在 `sys.modules`，但不断言 `threading` 缺席：原版 Ubuntu 的 Python 会在用户代码执行前由 `sitecustomize`／`apport_python_hook` 预导入 `threading`。这是解释器启动行为，不是本模块的副作用。源码层面的规则仍然绝对：`shared` 的任何 `.py` 不得出现 `import threading`。
