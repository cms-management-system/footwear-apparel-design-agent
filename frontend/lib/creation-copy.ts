/** These describe eligibility for a new call, never the outcome of an already reserved run. */
export function creationUnavailableMessage(reason?: string | null) {
  const messages: Record<string, string> = {
    CALL_LIMIT_EXHAUSTED: "当前创作额度已预留或用完，暂时不能开始新的创作。",
    BUDGET_EXHAUSTED: "本次创作预算已用完，暂时不能开始新的创作。",
    CREATION_AUTHORIZATION_REQUIRED: "当前项目尚未获得创作授权，消息仍可保存。",
    CAPABILITY_UNAVAILABLE: "创作服务暂不可用，消息仍可保存。",
    GRANT_BINDING_CONFLICT: "本次创作授权已用于其他任务，暂时不能开始新的创作。",
    PROVIDER_BUSY: "创作服务正在处理其他任务，请稍后再试。",
    CALL_OUTCOME_UNKNOWN: "原请求结果尚待核对，当前不会重新发送。",
    UPSTREAM_MANUAL_CONFIRMATION_REQUIRED: "上游设计需先确认执行稿，再明确生成图片。",
  };
  if (reason && messages[reason]) return messages[reason];
  if (reason && /\p{Script=Han}/u.test(reason) && !/[A-Z]{2,}(?:_[A-Z0-9]+)+/.test(reason)) return reason;
  return "暂时无法开始新的创作，已有内容仍保留。";
}
