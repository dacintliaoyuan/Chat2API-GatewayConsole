import { actionLabel } from "../console/operation-feedback";

/** Cloud names for the operations this console submits (target-aware). */
export function opLabel(action: string, target: string): string {
  if (action === "service.start") return "启动 API 服务";
  if (action === "service.stop") return "停止 API 服务";
  if (action === "service.restart") return "重启 API 服务";
  if (action === "account.login") return "登录 UM";
  if (action === "share.status") return "分层检查 API 服务";
  if (action === "share.diagnostics") return "分层诊断";
  if (action === "share.key_create") return "新建 API Key";
  if (action === "share.key_update") return "更新 API Key";
  if (action === "share.key_rotate") return "轮换 API Key";
  if (action === "share.key_revoke") return "撤销 API Key";
  if (action === "share.key_delete") return "删除 API Key";
  if (action === "share.key_test") return "测试 API Key";
  if (action === "share.key_reveal") return "查看 API Key 明文";
  if (action === "share.display") return "私有登录窗口";
  return actionLabel(action);
}
