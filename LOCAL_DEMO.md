# 本机免登录演示与恢复

契约 design-demo-access/1.0.1。需要 Python 3.12+、uv、Node.js 与 npm。干净克隆后：

```bash
cp .env.example .env
uv sync --dev
uv run uvicorn app.main:app --host 127.0.0.1 --port 8092
```

另一终端：

```bash
cd frontend
npm ci
npm run build:demo-access
npm run start:demo-access
```

打开 [员工入口](http://localhost:3092/demo/designer) 或 [经理入口](http://localhost:3092/demo/manager)。默认首页恢复当前演示角色，首次为员工。页面显示合成演示身份。入口/刷新不会创建业务、审批或模型请求。

默认 managed、instance `design-local-demo`、scope `local-demo-scope`；空人员库自动初始化固定 `design-employee-a` 与 `design-manager`，随机不可知密码，不需账号操作。仅空库初始化；现有库必须已有配置对应的 active 角色，错误返回 `DEMO_CONFIGURATION_INVALID`，不得修改既有用户来适配。现有本机主库使用自己的 instance/scope/origin/manager 配置，不能用此模板覆盖。

本地自动生成 `data/.design-demo-secret`（或 ASSETS_DIR 父目录下同名文件），权限 0600。演示恢复依赖它与原数据库，应一并保留，不提交、不分享、不删除以重置额度。独立 HttpOnly/SameSite=strict cookies 为普通 cookie 名加 `_demo` 与 `_demo_chain`，普通登录 cookie 保留。链持久记录角色、scope/instance 与可信用途，角色会话稳定派生；切换后旧角色权限即时失效，旧 context 写入409。退出清演示访问 cookie，保留恢复链，不删除普通会话；下次恢复同角色/用途。

GET `/api/design-auth/me` 自动建立/恢复 demo（200，完整 managed 身份、`access_mode=demo`、两角色入口）。POST `/api/design-auth/demo-access` 严格接受 `{"role":"designer"}` 或 `{"role":"manager"}`，其余字段422。bootstrap 免旧 context；后续业务写入仍需服务端提供的 X-Design-Context/action/revision。旧 login/register409 `DEMO_LOGIN_FROZEN`；账号改密、成员创建/停用/重置409 `DEMO_ACCOUNT_FROZEN`。员工/经理业务权限、对象归属、图片/SSE和服务凭据继续原守卫。

模板未配置模型密钥或收费能力，可查看、创建空白、保存标题/布局/提示词。创作保存真实 blocked 原因，不伪造回复/图片。需要运营模型时由运行负责人配置服务端 provider 与既有 2.0.3 `DESIGN_INTERACTIVE_POLICY_FILE`（私有0600）：新明确 action 有界预算，讨论 text1/image0，自主明确生成最多 text1/image1，上游人工确认原稿图 text0/image1，manager 无制作额度。策略版本、单主体并发、中央执行 ledger、unknown 守卫继续有效。任何已登记 validation 普通会话及其 demo/角色/退出续期链仍引用原 ledger，耗尽不回退 interactive；不使用新浏览器身份重试验收收费。

恢复正常认证：保留 DB、资产、secret、普通账号/密码与会话，将 `.env` 的 `DESIGN_DEMO_ACCESS_ENABLED=false`，由运行负责人重启后端。demo cookie/context 立即不能读写、取私有图、SSE或发未发送模型阶段；bootstrap404 `DEMO_ACCESS_DISABLED`。有残留 demo cookie 时普通 GET 不静默转用普通身份。显式普通密码登录成功会自动结束并清除当前演示访问 cookie，无需先额外退出；恢复链、用途和 ledger 保留，validation 继承原额度。也可 logout 清演示访问 cookie 后恢复原普通会话。旧 demo action 在新普通 context 下409，不自动重放。正常 me 为 `access_mode=authenticated`、入口 null；managed 仍禁止公开注册，由经理维护团队。空库演示随机密码不可用于正常登录，切换正式使用需由负责人先明确设置自己的正式账号，不能公开演示密码。

只监听 loopback，exact allowed origins 不能是公网。此模式不用于生产或公网部署。GitHub 仅交付源码/占位配置；`.env`、secret、数据库、模型/服务密钥、运行资产、私人照片不得发布。历史 CMS 文档为旧模式参考；当前 managed 实例不恢复 CMS 接口。
