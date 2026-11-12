# ZCode 运行中同步协议兼容性取证（3.16.2 样本）

2026-10-21 只读读取安装根 `D:\apps\zcode` 的 `resources/app.asar`；不修改安装、真实配置或会话，不重启客户端，不发模型请求，不读取或复制凭据值。维护者要求按协议兼容性判断，版本号只展示，整包摘要不参与准入。本记录的 3.16.2 是取证样本，不是唯一允许的版本；3.15.1 证据表（`zcode-hot-sync-3.15.1-evidence.md`）继续作为该样本的保守证据保留，3.14.5 证据表（`zcode-hot-sync-3.14.5-evidence.md`）继续作为旧布局的保守证据保留。

本轮取证范围：3.16.2 的八个区域先经语义复核判为**全部等价**（`output/shared-refs-inv-20261004/ai-gateway-hot-sync-3162-20261020/semantics-review.md` §12），本文件据此更新 `CURRENT_PROTOCOL_PROFILE` 中六个「等价」区域的摘要，两个「本就 SAME」的区域不动；产品代码只改 `zcode_hot_sync_protocol.py` 的该证据表与其样本标注注释。

样本文件信息（用于追溯，以下整文件长度与摘要均不用于放行）：

| 条目 | 字节数 | SHA256（前 8 字节） |
| --- | ---: | --- |
| out/host/index.js | 2068026 | 0018ac1f40c1b72f |
| out/host/chunk-EQ7EYADI.js | 239170 | 495cb420d9a0b378 |
| out/host/chunk-XLKRUNM2.js | 862171 | c7a6df717256b5f6 |

`package.json` 的 version 为 3.16.2（`python tmp/probe-region-digests-3162.py`、`python tmp/probe-sample-entries-3162.py` 实读）。整包 `app.asar` 为 341334118 字节、`out/host` 有 14 个条目；整包大小、整包摘要与固定 chunk 名都不参与准入，只有被检区域的规范化摘要参与。包布局由现有 `read_asar_header`/`read_asar_entry` 读取；版本缺失、升级或无关代码变化不影响兼容性判断。

## 与 3.15.1 已审闭包的比较

3.16.2 的压缩符号、chunk 划分与文件内位置与 3.15.1 都不同（锁与持久化仍在 `chunk-EQ7EYADI.js`，共享 schema 在 `chunk-XLKRUNM2.js`，配置格式/文件 codec/repository/运行时装配在宿主 `index.js`），逐个区域核对关键实现后语义等价：

| 区域 | 3.16.2 位置 | 与 3.15.1 契约的对照 |
| --- | --- | --- |
| 文件锁 | chunk-EQ7EYADI.js（`acquireFileLock` 及辅助函数，10 元素） | 同目录 `.lock` + 唯一 owner 文件 `owner-<pid>-<ts>-<rand>.json`{pid,createdAt,token}；锁后复查目录 dev/ino 身份与唯一所有者；只有 EEXIST/ENOENT 重试；EACCES/EPERM 不是进程死亡；只有 ESRCH 判死；超时错误码 `ZCODE_FILE_LOCK_TIMEOUT`；时间戳容差 5 分钟。逐项与 3.15.1 记录一致。 |
| 原子持久化 | chunk-EQ7EYADI.js（`atomicWritePrivateTextFile`/`withFileLock`，6 元素） | 锁默认重试 `[25,50,100,200,400]`、ownerless 宽限 100ms、最大等待 8000ms；`withFileLock` 释放队列与文件锁；原子写使用同目录临时文件 + rename（重试 `[50,100,200,400,800]`，EPERM/EBUSY/EACCES 可重试），失败清理临时文件。与 3.15.1 记录一致。 |
| 配置格式 | 宿主 index.js（`decodeProviderConfigFile`/`encodeProviderConfigFile`/`createProviderRules` 等，15 元素） | 先校验 schemaVersion（非负整数，高于当前支持版本报错）再逐版本迁移，最后 strict schema `{schemaVersion:1, config:{providerOrder?, providerConfigRules, modelConfigRules, defaultModelSelection?}}`；写入时 `schemaVersion:1` + providerRules/modelConfigRules/defaultModelSelection。与 3.15.1 记录一致；区域另有附加的迁移器冗余校验与遗留手工规则迁移，方向与「逐版本迁移」一致。 |
| 文件 codec | 宿主 index.js（`readJsonFileIfExists`/`emptyUpdate`，2 元素） | 缺失文件返回 null、ENOENT 不抛；空更新为冻结的 `{providers,models,providerOrder}`；记录判定 `typeof==="object" && !==null && !Array.isArray`（`isRecord` 落在配置格式区域闭包，函数体与契约逐字同形）。与 3.15.1 记录一致。 |
| Repository 监听 | 宿主 index.js（`NodePersonalProviderConfigRepository`/`snapshotFromUpdate`，2 元素） | 更新在文件锁内重读再合并；revision 由编码内容 sha256 生成；默认 `pollingIntervalMs` 1000；变更发 `poll-changed`；同 revision 不重复发布；dispose 后停止轮询与事件。与 3.15.1 记录一致。 |
| 运行时装配 | 宿主 index.js（`NodeProviderConfigRuntime`，1 元素） | 构造 `NodePersonalProviderConfigRepository({filePath: personalFilePath, pollingIntervalMs: personalPollingIntervalMs, ...})`，未发现默认禁用监听（无显式 `pollingIntervalMs: false`，默认 `??1e3`）。与 3.15.1 记录一致。 |
| 共享 model/selection schema | chunk-XLKRUNM2.js（selection 4 元素 / model 1 元素） | selection schema `{providerId:string.trim().min(1), modelId:string.trim().min(1), options:{reasoningLevel?}.strict().optional()}.strict()` + 分隔符 `"$"` + format/parse 函数；model 规则 schema 仍为 strict 且 `properties/optionSpecs` 可空。与 3.15.1 记录一致，且这两个区域的摘要本就与记录值逐字节相同。 |

六个区域的摘要与 3.15.1 记录值不同（lock、persistence、model_provider_format、file_codec、repository、runtime_wiring），两个相同。语义复核（同轮，`semantics-review.md` §11）对 lock 与 persistence 做过「差异是否只是改名」的证伪尝试：`_normalize` 把「标识符后紧跟 `:`」当属性名保留，三元真值分支与两处 `_` 不会被重编号，对这两个盲点做单/双标识符全量替换搜索**没有任何替换能复现记录摘要**，故摘要差异不能归因于已识别的改名盲点，而属函数体改写；该轮结论是契约枚举的协议事实逐条未变（上表各行），本文件据该结论更新证据表。

## 原生依赖绑定（按真实导入解析，不按固定压缩别名）

锁与持久化闭包实际导入并使用的原生/共享绑定，按宿主文件自身的 `import{...}from"..."` 解析（`python tmp/probe-region-bindings-3162.py` 实读）：

- `fs/promises`（3.16.2 实际模块说明符为 `node:fs/promises`）：锁区域 `readFile`、`rm`、`writeFile`、`readdir`、`rmdir`、`stat`、`mkdir`；持久化区域 `writeFile`、`rename`、`rm`、`mkdir`；
- `path`（`node:path`）：锁区域 `join`；持久化区域 `dirname`、`basename`、`join`；
- `timers/promises`（`node:timers/promises`）：锁与持久化区域各 `setTimeout`；
- `crypto`（`node:crypto`）：repository 区域 `createHash`；
- 超时错误码：锁区域从共享 chunk 导入的常量解析为字符串常量 `const:"ZCODE_FILE_LOCK_TIMEOUT"`（值与 3.14.5/3.15.1 记录一致）。

规范化时，原生导入按 `native:<module>:<export>` 参与摘要、跨 chunk 字符串常量解析成其值（`const:"ZCODE_FILE_LOCK_TIMEOUT"`）、其余绑定标识符按出现顺序重编号，属性名/字符串/数字保持字面量。因此等价改名与换 chunk 不改变摘要，任何原生依赖错误绑定、常量取值变化或函数体变化都会改变摘要并保持待应用。3.15.1 文档按 `fs/promises`/`path` 记名，3.16.2 实测说明符带 `node:` 前缀（`native:node:fs/promises:readFile` 等），导出名逐项一致。

## 当前准入实现

`zcode_hot_sync_protocol.inspect_installed_protocol`（`zcode_hot_sync_protocol.py:1067`）先按固定锚点核对旧布局（3.14.5，`PROTOCOL_PROFILE`），再按运行时命名核对当前布局（3.16.2，`CURRENT_PROTOCOL_PROFILE`）：

- 当前布局的每个区域按客户端自己的运行时命名字符串（`i(fn,"acquireFileLock")`、`i(this,"NodeProviderConfigRuntime")` 等）与稳定字面量定位（`_naming_calls`，`:616`；`_element_patterns`，`:605`），函数体取平衡大括号，闭包按被引用的同文件声明展开一层（`_closure`，`:771`）；区域必须在同一个 bundle 内（跨 bundle 拆分按无法确认拒绝）。
- **本轮新增的同名消歧规则**（`_region_elements`，`:686`）：一个运行时名字可能被同一 bundle 内的多个声明携带。此时只接受「从本区域**已经解析**的锚点（其余 roots/classes/constants）出发的引用链唯一命中一个候选」的消歧，并把依据写进证据（`notes`，实测为「同名消歧：renameWithRetry 在 chunk-EQ7EYADI.js 内命名 2 次（hc、Zc）；仅 hc 被区域锚点 root:atomicWritePrivateTextFile 引用」）；引用判定用 `_references`（`:647`）与 `_shadowed_names`（`:631`，故意取超集，被遮蔽的名字不算引用）。
- 仍 fail-closed 的情形：区域元素缺失或命名歧义（`_element_reason`，`:655`）、常量缺失或歧义、**同名声明无法由本区域锚点证明唯一**（两个候选都无本区域锚点引用，或都被本区域锚点引用，或候选无法提取）、区域所在 bundle 无法唯一确认、bundle 集合为空、跨 chunk 依赖的目标模块缺失/导出未找到/实现无法确认（`_import_token`，`:818`）；以上全部按「证据无法解析或不支持，继续待应用」拒绝。区域元素存在而摘要不符按「协议兼容性待确认」延期（`_inspect_current_layout_bundles`，`:961`）。候选绝不按位置、长度或任何其他猜测选取。
- 旧布局仍在场时优先按旧证据表核对，旧布局锚点完全不在场才尝试当前布局；旧布局的依赖绑定/证据表失败继续给出具体原因而不是静默落到当前布局。
- 版本号只展示；不把版本 allowlist、整包 SHA256 或固定 chunk 名当作准入条件。

安装版代码在日常准入时只读，不执行；宿主与共享依赖各有 8 MiB 读取上界（`_MAX_HOST_BYTES`，`:38`），bundle 集合有 64 个条目上界（`_MAX_BUNDLES`，`:40`），区域片段有 100,000 字符上界（`_MAX_REGION_CHARS`，`:39`）。兼容时仍走原有锁内重读、受管范围合并、版本冲突检测、原子替换和写后读回；不兼容时保持待应用，不自动退出或重启 ZCode。

## 真实安装只读核对

2026-10-21 只读读取安装根 `D:\apps\zcode` 的 `resources/app.asar`（`python tmp/probe-region-digests-3162.py`）：`package.json` version=3.16.2；八个区域全部定位到唯一 bundle 并提取全部元素，摘要与更新后的 `CURRENT_PROTOCOL_PROFILE` 逐字节一致（下表 `matches=True`），准入返回兼容。

| 区域 | 所在条目 | 元素数 | 摘要 | 证据表值 | 匹配 |
| --- | --- | ---: | --- | --- | --- |
| lock | chunk-EQ7EYADI.js | 10 | 45ae03a45a811373d1d4bed32c3f763a682ced71b8e014986adfe70a5721915d | 同左 | True |
| persistence | chunk-EQ7EYADI.js | 6 | 06a71a226682bda8e07e3353fdb9e03193342bc08f6353d50ffe00a748191ffe | 同左 | True |
| model_provider_format | index.js | 15 | 513cefbe1661c8ecf85e360f086ae56943fe38ac49aac23e1586cff993048d58 | 同左 | True |
| file_codec | index.js | 2 | 75855d0d0f288a329cc9e66d1e004d146ed455244fd7685403ede68ad506e289 | 同左 | True |
| repository | index.js | 2 | 6ef8244493147a948f314215007b81a2a8bfc1e2257c01c0506598777be23512 | 同左 | True |
| runtime_wiring | index.js | 1 | a8e11814396c607e033f929a45c6eaac7f4beb0cf989b4bf56446ce177766d3b | 同左 | True |
| shared_selection_schema | chunk-XLKRUNM2.js | 4 | e742a3e3aadaab1d9f8705299e6b81fb780a0690b3c419977c9ed7ab1307908d | 同左（本就相同） | True |
| shared_model_schema | chunk-XLKRUNM2.js | 1 | d31fb5d84c403bf0a5d858cfbe5379cbdc96ae1b188e49462257fe1695912692 | 同左（本就相同） | True |

更新证据表前的同一次只读读取为：六区域摘要与记录值（3.15.1）不同，准入返回 `包版本 3.16.2（仅展示）：协议兼容性待确认：lock`；更新后同一读取路径返回 `包版本 3.16.2（仅展示）：文件锁、配置格式和变更监听协议兼容`。本次没有执行安装版 JavaScript、没有写真实配置、没有重启或启动任何进程。

## 隔离实跑与结论限制

合成安装包（私有临时目录、合成 asar）覆盖：等价改名与换 chunk 后仍准入；原生依赖错误绑定（如 `mkdir`→`rmdir`）、常量取值变化、函数体变化、元素缺失、bundle 缺失、asar 损坏、进程枚举未知、跨 chunk 依赖缺失/同名诱饵/导出名错误一律拒绝或延期；同名声明的消歧只有可证明时才解析（其余仍 fail-closed）。`tests/test_zcode_hot_sync.py` 的 `ZcodeCurrentHotSyncAdmissionTests` 覆盖上述边界，`tests/zcode_current_protocol_fixture.py` 提供当前布局的合成安装包；真实安装只读核对由 `test_real_installation_regions_are_extractable`、`test_real_installation_repointed_import_is_rejected` 与 `tests/test_model_sync.py` 的 `test_running_publication_uses_the_current_protocol_admission` 承担。

更新证据表后，读路径仍为：宿主 `NodePersonalProviderConfigRepository` 默认 1000ms 轮询 → 内容摘要 revision 变化 → 发 `poll-changed` → `ProviderConfigService` 发 `"personal:updated"` → `onProviderProvisioningSourceChanged("personal-config")` → 注册表/模型选择刷新。该接线以 3.16.2 宿主的字面量实读为准（`poll-changed`/`poll-error`/`personal:updated`/`personal-config` 各 1 处、`pollingIntervalMs` 3 处、`onRecovery`/`onPollingError` 各 2 处）；下一次发送消息使用刷新后的模型选择是运行期行为，静态核对只确认接线。

**结论限制**：本记录只证明 3.16.2 样本的八个区域与 3.15.1 已审契约在枚举的协议事实上逐条一致，**不宣称任意新版本自动兼容**——任何新版本仍须重新取证，摘要不符一律保持待应用。区域核对与真实安装协议兼容都**不代替**真实 GUI 模型选择器、同会话下一次发送采用新参数的业务验收，**不得宣称「文件已写即会话已采用」**。此外，3.15.1 的 `app.asar` 已不在本机（现为 3.16.2），无法做新旧字节级 diff，故摘要差异的具体成因不能确定；`_normalize` 对等价改名敏感的既有缺陷本轮未修，因此本记录不把「摘要相同」当等价证明、也不把「摘要不同」当变更证据。
