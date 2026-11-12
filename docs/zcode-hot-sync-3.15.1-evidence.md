# ZCode 运行中同步协议兼容性取证（3.15.1 样本）

2026-10-11 只读读取运行中客户端镜像对应安装根的 `resources/app.asar`；不修改安装、真实配置或会话，不重启客户端，不发模型请求。维护者要求按协议兼容性判断，版本号只展示，整包摘要不参与准入。本记录的 3.15.1 是取证样本，不是唯一允许的版本；3.14.5 证据表（`zcode-hot-sync-3.14.5-evidence.md`）继续作为旧布局的保守证据保留。

样本文件信息（用于追溯，以下整文件长度与摘要均不用于放行）：

| 条目 | 字节数 | SHA256 |
| --- | ---: | --- |
| out/host/index.js | 1678223 | 3fc3c5b286d7871e87f257eb09ab8e7165e8705307c2ee6cb66b687cd4c7f4bc |
| out/host/chunk-WNGDTTYX.js | 191832 | 33816ee6b3428812（前 8 字节） |
| out/host/chunk-MR7X55B4.js | 823893 | b052a71d50d597ae（前 8 字节） |

package.json 的 version 为 3.15.1。包布局由现有 `read_asar_header`/`read_asar_entry` 读取；版本缺失、升级或无关代码变化不影响兼容性判断。进程枚举不完整、安装来源有歧义或协议无法确认仍延期。

## 与 3.14.5 已审闭包的比较

3.15.1 的压缩符号、chunk 划分与文件内位置与 3.14.5 都不同（锁与持久化从宿主 `index.js` 移入 `chunk-WNGDTTYX.js`，共享 schema 常量在 `chunk-MR7X55B4.js`），但逐个区域核对关键实现后语义等价：

| 区域 | 3.15.1 位置 | 与 3.14.5 契约的对照 |
| --- | --- | --- |
| 文件锁 | chunk-WNGDTTYX.js（`acquireFileLock` 及辅助函数） | 同目录 `.lock` + 唯一 owner 文件 `owner-<pid>-<ts>-<rand>.json`{pid,createdAt,token}；锁后复查目录 dev/ino 身份与唯一所有者；只有 EEXIST/ENOENT 重试；EACCES/EPERM 不是进程死亡；只有 ESRCH 判死；超时错误码 `ZCODE_FILE_LOCK_TIMEOUT`；时间戳容差 5 分钟。逐项与 3.14.5 记录一致。 |
| 原子持久化 | chunk-WNGDTTYX.js（`atomicWritePrivateTextFile`/`withFileLock`） | 锁默认重试 `[25,50,100,200,400]`、ownerless 宽限 100ms、最大等待 8000ms；`withFileLock` 释放队列与文件锁；原子写使用同目录临时文件 + rename（重试 `[50,100,200,400,800]`，EPERM/EBUSY/EACCES 可重试），失败清理临时文件。与 3.14.5 记录一致。 |
| 配置格式 | 宿主 index.js（`decodeProviderConfigFile`/`encodeProviderConfigFile`/`createProviderRules` 等） | 先校验 schemaVersion（非负整数，高于当前支持版本报错）再逐版本迁移，最后 strict schema `{schemaVersion:1, config:{providerOrder?, providerConfigRules, modelConfigRules, defaultModelSelection?}}`；写入时 `schemaVersion:1` + providerRules/modelConfigRules/defaultModelSelection。与 3.14.5 记录一致。 |
| 文件 codec | 宿主 index.js（`readJsonFileIfExists`/`emptyUpdate`/`isRecord`） | 缺失文件返回 null、ENOENT 不抛；空更新为冻结的 `{providers,models,providerOrder}`；记录判定 `typeof==="object" && !==null && !Array.isArray`。与 3.14.5 记录一致。 |
| Repository 监听 | 宿主 index.js（`NodePersonalProviderConfigRepository`/`snapshotFromUpdate`） | 更新在文件锁内重读再合并；revision 由编码内容 sha256 生成；默认 `pollingIntervalMs` 1000；变更发 `poll-changed`；同 revision 不重复发布；dispose 后停止轮询与事件。与 3.14.5 记录一致。 |
| 运行时装配 | 宿主 index.js（`NodeProviderConfigRuntime`） | 构造 `NodePersonalProviderConfigRepository({filePath: personalFilePath, pollingIntervalMs: personalPollingIntervalMs, ...})`，未发现默认禁用监听。与 3.14.5 记录一致。 |
| 共享 model/selection schema | chunk-MR7X55B4.js | selection schema `{providerId:string.trim().min(1), modelId:string.trim().min(1), options:{reasoningLevel?}.strict().optional()}.strict()` + 分隔符 `"$"` + format/parse 函数；model 规则 schema 仍为 strict 且 `properties/optionSpecs` 可空。与 3.14.5 记录一致。 |

## 原生依赖绑定（按真实导入解析，不按固定压缩别名）

锁与持久化闭包实际导入并使用的原生/共享绑定，按宿主文件自身的 `import{...}from"..."` 解析：

- `fs/promises`：`mkdir`、`readFile`、`readdir`、`rmdir`、`rm`、`stat`、`writeFile`（锁与持久化），另有 `rename`、`chmod`、`open`、`lstat`、`realpath`、`appendFile` 在闭包内；
- `path`：`join`（以及 `basename`/`dirname` 等闭包内使用）；
- `timers/promises`：`setTimeout`；
- `crypto`：`createHash`；
- 超时错误码：锁 chunk 从共享 chunk 导入常量，该导出解析为字符串常量 `"ZCODE_FILE_LOCK_TIMEOUT"`（值与 3.14.5 一致）。

规范化时，原生导入按 `native:<module>:<export>` 参与摘要、跨 chunk 字符串常量解析成其值（`const:"ZCODE_FILE_LOCK_TIMEOUT"`）、其余绑定标识符按出现顺序重编号，属性名/字符串/数字保持字面量。因此等价改名与换 chunk 不改变摘要，任何原生依赖错误绑定、常量取值变化或函数体变化都会改变摘要并保持待应用。

## 当前准入实现

`zcode_hot_sync_protocol.inspect_installed_protocol` 先按固定锚点核对旧布局（3.14.5，`PROTOCOL_PROFILE`），再按运行时命名核对当前布局（3.15.1，`CURRENT_PROTOCOL_PROFILE`）：

- 当前布局的每个区域按客户端自己的运行时命名字符串（`i(fn,"acquireFileLock")`、`i(this,"NodeProviderConfigRuntime")` 等）与稳定字面量定位，函数体取平衡大括号，闭包按被引用的同文件声明展开一层；区域必须在同一个 bundle 内（跨 bundle 拆分按无法确认拒绝）。
- 区域元素缺失、命名歧义、常量缺失、区域所在 bundle 无法唯一确认、bundle 集合为空：全部按「证据无法解析或不支持，继续待应用」拒绝；区域元素存在而摘要不符按「协议兼容性待确认」延期。
- 旧布局仍在场时优先按旧证据表核对，旧布局锚点完全不在场才尝试当前布局；旧布局的依赖绑定/证据表失败继续给出具体原因而不是静默落到当前布局。
- 版本号只展示；不把版本 allowlist、整包 SHA256 或固定 chunk 名当作准入条件。

安装版代码在日常准入时只读，不执行；宿主与共享依赖各有 8 MiB 读取上界，bundle 集合有 64 个条目上界，区域片段有 100,000 字符上界。兼容时仍走原有锁内重读、受管范围合并、版本冲突检测、原子替换和写后读回；不兼容时保持待应用，不自动退出或重启 ZCode。

## 更新到模型选择与下一次发送的读路径

`provider_config.json` 写入后，读路径为：宿主 `NodePersonalProviderConfigRepository` 默认 1000ms 轮询 → 内容摘要 revision 变化 → 发 `poll-changed` → `ProviderConfigService` 发 `"personal:updated"` → `onProviderProvisioningSourceChanged("personal-config")` → 注册表/模型选择刷新（`onDidCheckZCodeBuiltin`、`modelSelection`）。下一次发送消息使用刷新后的模型选择；这一步是运行期行为，静态核对只确认接线，不证明某次会话已采用。3.14.5 证据记录的「真实 GUI 同会话采用」缺口同样保留。

## 隔离实跑与结论限制

合成安装包（私有临时目录、合成 asar）覆盖：等价改名与换 chunk 后仍准入；原生依赖错误绑定（如 `mkdir`→`rmdir`）、常量取值变化、函数体变化、元素缺失、bundle 缺失、asar 损坏、进程枚举未知一律拒绝或延期；真实 `run_sync` 入口在运行中按真实准入发布 `provider_config.json`，不兼容时保持 pending 且双配置文件字节不变，且准入/发布路径都不重启客户端。测试与夹具全部使用私有临时目录，不读写真实客户端配置。

`tests/test_zcode_hot_sync.py` 的 `ZcodeCurrentHotSyncAdmissionTests` 覆盖上述准入边界，`tests/zcode_current_protocol_fixture.py` 提供当前布局的合成安装包；真实安装只读核对由 `test_real_installation_matches_the_evidence_table` 与 `tests/test_model_sync.py` 的 `test_running_publication_uses_the_current_protocol_admission` 承担。

## 真实安装只读核对

2026-10-11 只读读取运行中安装根 `D:\apps\zcode` 的 `resources/app.asar`：`package.json` version=3.15.1；八个区域摘要与本文件对应的 `CURRENT_PROTOCOL_PROFILE` 一致，准入返回兼容。本次没有执行安装版 JavaScript、没有写真实配置、没有重启或启动任何进程。

真实 GUI 模型选择器、同会话下一次发送消息采用新参数、移除当前模型提示仍须另做业务验收；区域核对与真实安装协议兼容都不代替该闭环。不得宣称「任意新版本自动兼容」或「文件已写即会话已采用」。
