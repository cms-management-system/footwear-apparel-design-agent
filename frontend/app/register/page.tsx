import AuthEntryGate from "@/components/team/AuthEntryGate";
import Link from "next/link";
import s from "../handoffs/handoff.module.css";
export default function RegisterPage() {
  return <AuthEntryGate><main className={s.loginWrap}><section className={s.loginCard}><p className={s.kicker}>团队账号</p><h1>联系负责人开通账号</h1><p className={s.subtle}>当前设计工作区由团队管理者管理成员。账号开通后，你可以查看分派给自己的任务。</p><Link className={s.primaryLink} href="/login">返回登录</Link></section></main></AuthEntryGate>;
}
