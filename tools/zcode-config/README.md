# ZCode 规则库恢复工具

`keepset_apply.py` 是手工恢复工具，复用产品渲染器 `model_sync.render_provider_config_sync` 求投影。日常同步使用控制台；本工具不参与运行中兼容准入。

默认只读 `--check`。`--apply` 与 `--revert` 要求关闭 ZCode，不启动或停止客户端、不发模型请求。显式提供当前推导的保留集：

```powershell
python tools/zcode-config/keepset_apply.py --check --keepset <当前保留集.json>
python tools/zcode-config/keepset_apply.py --apply --keepset <当前保留集.json>
python tools/zcode-config/keepset_apply.py --revert --backup <备份目录>
```

保留集绑定当前代码摘要与受管状态；不一致、不可读或不可解析均拒绝。范围外手工条目保持不动；目录上下文不由本工具查询，投影沿用规则库已有值。

唯一写目标是 `--config` 指定的规则库，默认 `~/.zcode/v2/provider_config.json`，不写主配置或同步状态。应用前逐字节备份并记录 SHA256，写后读回；恢复先核对备份摘要与 JSON。恢复覆盖规则库，若还需要当前内容，先另存备份。

备份可能包含 API Key，须落在不入库的受控目录。退出码 0 表示动作完成或无差异；2 表示差异、拒绝或不可判定。写回文件不证明客户端已采用。

回归：`python -m unittest tests.test_zcode_config_tools -q`。
