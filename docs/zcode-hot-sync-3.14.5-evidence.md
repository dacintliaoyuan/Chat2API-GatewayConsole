# ZCode 运行中同步协议兼容性取证（3.14.5 样本）

2026-10-08重新读取运行中客户端镜像对应安装根的resources/app.asar；不修改安装、真实配置或会话，不重启客户端，不发模型请求。维护者要求改为协议兼容性判断，版本号只展示，整包摘要不再参与准入。本记录的3.14.5是取证样本，不是唯一允许的版本。

样本文件信息（用于追溯实验，以下整文件长度和摘要均不用于放行）：

| 条目 | 字节数 | SHA256 |
| --- | ---: | --- |
| out/host/index.js | 1498022 | a3473b147101ee29f53394bcbe90f4a121c570e2bdf1a5e0a19b938f19b174e5 |
| out/host/chunk-LNGNUNBX.js | 113415 | 870b2b85f75b6afada5c9361f7c9eec2b7d988532f5741dd80a62ee4bb28ac16 |
| out/host/chunk-4RHDQOPX.js | 775783 | 0c6c5a962078e3217c95639e020c885500ca6c0c33eef208e32960da90ea9f5d |

package.json的version为3.14.5。包布局由现有read_asar_header/read_asar_entry读取；版本缺失、升级或无关代码变化不影响兼容性判断。进程枚举不完整、安装来源有歧义或协议无法确认仍延期。

## 当前准入实现

zcode_hot_sync_protocol.inspect_installed_protocol只比较已审查的关键协议实现区域：文件锁、原子持久化、provider/model配置格式、文件codec、Repository监听、运行时装配，以及实际导入的共享model/selection schema。PROTOCOL_PROFILE保存这八个区域的摘要，不保存整包、整个bundle或固定chunk文件名。schema通过宿主实际导入关系定位，并核对导出绑定、原生fs/path/timer/crypto依赖及锁超时错误码；目录里的同名关键词或无关schema副本不能代替依赖证据。

这是保守的已知兼容实现识别：版本、无关代码和chunk名称变化会放行；关键协议区域变化（包括尚未核验的等价重写或压缩符号变化）视为兼容性未知，继续待应用。它不是任意JavaScript语义等价判定器，也不凭关键词或一份模拟读写结果宣称新实现兼容。新增兼容实现应先重新核对协议闭包与隔离行为，再更新对应区域的证据；不得靠删除检查放行。

安装版代码在日常准入时只读，不执行；宿主和共享依赖各有8 MiB读取上界，协议片段有100,000字符上界。兼容时仍走原有锁内重读、受管范围合并、版本冲突检测、原子替换和写后读回；不兼容时保持待应用，不自动退出或重启ZCode。

## 核对的契约

- 宿主index.js约29426字符处的acquireFileLock仍使用同目录.lock、唯一owner文件、pid/createdAt/token，锁后复查目录身份与唯一所有者；权限拒绝不视为进程死亡，只有ESRCH可回收对应失活所有者。
- 约32924处的持久化默认重试为25/50/100/200/400ms，ownerless宽限100ms，最大等待8000ms；withFileLock释放队列和文件锁，atomicWritePrivateTextFile使用同目录临时文件与rename，失败清理临时文件。
- decodeProviderConfigFile仍先校验schemaVersion和逐版本迁移，再走strict schema；provider/model规则、顺序、默认选择通过既有codec读写。实际共享schema声明及绑定纳入协议区域核对，不能凭目录中有文件或关键词存在判断兼容。
- NodePersonalProviderConfigRepository约111294处：更新在文件锁内重读再合并；写入后以内容摘要生成revision。默认pollingIntervalMs仍为1000，变更发poll-changed；同revision不重复发布，销毁后停止轮询与事件通知。运行装配仍传入personalFilePath和personalPollingIntervalMs，未发现默认禁用监听。
- RPC bundle将新chunk名称替换为旧名称后，完整摘要为旧证据154169b320e8db602bc7cc27be55717ef6735845a66e2285a9764e4b817047de；该RPC桥内容与已取证版一致。宿主整体摘要不同，未宣称宿主全文件只变了导入名。

## 隔离实跑与结论限制

从安装版宿主提取已检查的锁、原子写入和Repository片段到临时目录执行：6次并发更新保留全部结果，锁与临时文件无残留；仍存活的owner被明确拒绝；Repository检测外部原子更新、发poll-changed，读回新revision，dispose后不再通知。Repository实验的codec边界使用恒等替身，只证明安装版监听与持久化片段的行为；strict codec属于上述静态核对，不能把该实验称为完整客户端验收。

三个片段摘要依次为：

- 锁：cc486d61b6c2709107f9a21101fe95f98f62471b61d0fdac42136c4b0250f2b3
- 原子持久化：0b2e11de2ccc79aad42f9cab5b16093ed9cc021e54e55040f9bba547ec1ee4ef
- Repository：39c0f66c7910b8cfd13f9b17a8f41f42ff86afa06b8c26767913fe80ba130d28

回归覆盖版本升级加无关bundle变化放行、chunk改名放行、八个关键区域改写拒绝、原生依赖/schema绑定改写拒绝、伪装schema副本无效、危险路径/证据缺失/片段歧义/读取超限拒绝。真实run_sync入口在临时安装与客户端目录中验证：兼容升级真正发布，不兼容保持pending且双配置文件字节不变；任何客户端重启调用都会让该测试失败。合成源/证据替身只证明入口和核对逻辑，安装片段实跑证据如上。

真实GUI模型选择器、同会话下一次发送消息采用新参数、移除当前模型提示仍须另做业务验收；片段验证与真实安装协议兼容均不代替该闭环。不得宣称“任意新版本自动兼容”或“文件已写即会话已采用”。
