# 设计后端公网发布操作

目标仅新实例 design-public-20261010 / public-three-agent-20261010，Python loopback8192，Next3192，外部/v2/design。原8092、3092、本机私有库/图片/unknown任务保持。公开初始化不复制原运行数据，也不生成演示图片；两角色初始库为空业务库，真实服务交接按原人工批准链操作。

## Linux依赖和私有配置

源码新release目录中的`pyproject.toml`/`uv.lock`为Linux依赖契约，Python>=3.12，使用`uv sync --frozen --no-dev`。若服务器没有uv，根先安装/选择已有uv，再在该独立release创建venv；不能复用macOS .venv或node_modules。也可在Linux独立venv执行`.venv/bin/python -m pip install --require-hashes -r deployment/requirements-public.lock.txt`，此requirements从uv.lock frozen导出（包含平台markers与完整包hash）。Python wheel由Linux环境解析锁文件，不需GPU。后端一进程一Uvicorn worker，嵌入既有持久队列worker，不另启动第二个执行worker。

根已创建`/srv/contest/secrets/three-agent-public-20261010/pairing-input.json`（0600 contest属主）。初始化读取其中三项design_package_reader/design_event_writer/design_asset_reader；完整七项须唯一，不生成另一组token。配对主体与产品初始化一致：design-public-package-reader、design-public-event-writer、product-public-asset-reader。内部产品root http://127.0.0.1:8291，产品实例product-public-20261010；机器鉴权purpose/subject/scope/instance严格绑定。

根写`/srv/contest/secrets/three-agent-public-20261010/design-runtime-env.json`（0600 contest属主），参照`design-public.providers.example.json`。不输出/提交值。此JSON可只含provider字段；冻结的Origin/instance/scope/路径/角色由runtime脚本注入，不能改为旧库。显式写同值也允许。脚本丢弃ambient DESIGN_/AGENT_/MODEL_/IMAGE_/CMS_环境，不读取源码.env，旧单次grant/validation文件不携入。私有配置目录700、库/密钥600，服务User=contest。

provider白名单：MODEL_BASE_URL,MODEL_ID,MODEL_API_KEY；IMAGE_BASE_URL,IMAGE_MODEL,IMAGE_API_KEY,IMAGE_SIZE,IMAGE_REFERENCE_FORMAT；AGENT_VISION_BASE_URL,AGENT_VISION_MODEL,AGENT_VISION_API_KEY,AGENT_VISION_THINKING；AGENT_IMAGE_DOWNLOAD_HOSTS。执行开关/保护：AGENT_ENABLED,DESIGN_PAID_PROVIDERS_ENABLED,DESIGN_IMAGE_ONLY_ENABLED,AGENT_REASONING_CALL_MAX_FEN,AGENT_IMAGE_CALL_MAX_FEN,DESIGN_PUBLIC_TEXT_CALL_LIMIT,DESIGN_PUBLIC_IMAGE_CALL_LIMIT。运行负责人还可提供DESIGN_DEMO_ACCESS_ENABLED=true/false（首发true、恢复普通登录false）；其他供应商/费用字段拒绝，字段值均为字符串。示例默认供应商关闭；实际可生成部署须根受控复制现有合法provider值并显式启用三个执行开关=true。每阶段fee cap默认50分为保护性估算上限，不声称实际价格；每action单text/单image、累计20/4、单并发同时生效。reference/3D默认关闭。

## 一次初始化与启动

在根冻结新release的design目录，以contest运行（根替换RELEASE路径）：

```sh
.venv/bin/python scripts/public_design_runtime.py --env-json /srv/contest/secrets/three-agent-public-20261010/design-runtime-env.json --initialize --pairing-input /srv/contest/secrets/three-agent-public-20261010/pairing-input.json
.venv/bin/python scripts/public_design_runtime.py --env-json /srv/contest/secrets/three-agent-public-20261010/design-runtime-env.json
```

首次建立`/srv/contest/data/three-agent-public-20261010/design/design.sqlite3`、assets/、48字节`.design-demo-secret`，仅两个合成角色design-employee-a/design-manager、随机不可知密码；不创建项目、会话、图或供应商任务。生成私有design-pairing.json、design-interactive-policy.json（0600），现有不同配置拒绝覆盖/轮换。init可对已归属新实例的库幂等运行，调用账本/秘密保持；非本实例或未初始化的现有库拒绝迁移。run必须先完成init，拒绝秘密丢失/权限不符/陌生库。初次中断留下未完成库会拒绝盲目继续：根先核实其确为空新库再修复，不覆盖旧数据。

示例systemd见`design-public.service.example`，根替换release绝对路径。启动固定127.0.0.1:8192，proxy_headers=False，access_log=False，workers=1。原队列recover只处理当前新库，unknown不重试、不因TTL释放slot。升级仅换源码release/venv，仍指同data+secrets；不要删除数据库、demo-secret、policy、累计表或slot来“恢复额度”。正常重启会继续读取已保存会话、历史/人工审批和账本。停止切release回退时先停新进程，私有持久目录保持；原云应用与本机原实例均不覆盖。回到不含累计额度的旧源码可能绕过费用保护，不作为公开版本回退目标。

## 固定代理与验收

边缘代理送Next3192时必须固定`Host: s357brv8j8f9gdhvbu9a9.apigateway-cn-beijing.volceapi.com`及`X-Forwarded-Proto: https`，覆盖浏览器伪造值。Next现有/api rewrite去prefix→8192；其http-proxy会将原Host放入X-Forwarded-Host。Python禁用proxy_headers，保留实际loopback peer。人员请求要求实际HTTPS exact origin，或loopback+固定forwarded-host+https；所有写入（bootstrap/login/logout亦同）必须精确Origin。服务/api/design-integration仍走用途Bearer，不能拿人员Cookie替代机器token。GET /api/health无身份。

公网角色普通/演示Cookie各自instance派生、Secure/HttpOnly/SameSiteStrict、Path=/v2/design，退出删除同Path；前端负责basePath与相对角色链接。Cookie/actor/context/revision/action与owner/分派/经理审查约束不变。DESIGN_DEMO_ACCESS_ENABLED=false保留普通登录恢复能力（仍精确HTTPS与累计账本）；公开人员界面无额度增额入口。运行脚本首发默认true；新库初始化必须true，运行负责人在同一私有JSON改false并重启可恢复普通登录，原demo Cookie不再认可且累计账本继续保护。新角色初始随机不可知密码，恢复普通认证后的密码安排须用既有受控账号管理流程，不能引入旧私有账号或在公开网页调整开关。

后端capabilities新增public_execution_budget={remaining:{text_calls,image_calls},reset:manual_server_only,concurrency:1,unknown_refunded:false}；direct_creation原每actionremaining继续受总额度截断。新reason=PUBLIC_BUDGET_EXHAUSTED，run.error.message是“公开演示累计额度已用完，暂不可生成；已有项目、历史与人工审批仍可使用”。FE应将此reason映射为同类中文；无映射的已有fallback仍显示不可开始且保持内容，后端并非错误地ready。JSON body不接受purpose/grant/quota。

计数在实际供应商派发前的BEGIN IMMEDIATE事务中与ProviderSlot保留唯一attempt，提交后调用。未知、超时及提交后无法确认出网均保守计入且不退还；提交前回滚不计。20文本/4image是累计调用数（非日重置、非20作品），角色/会话/action/policy版本/重启不清零；root validation单独耗尽用途仍不会获运营grant。只有运行负责人改server limits并重启可增额，已有次数完整保持，禁止manager自助。mock/合成测试不证明真实模型效果或市场/经营结果。

总控云验收不出图、不用新身份规避原validation：页面/静态/API/六角色、foreign Origin403、对象隔离、空白项目/布局保存刷新与进程重启、人工合成交接原体摘要+受控Bearer图读取（如另有许可合成资产）即可。根记录Linux部署/实际HTTP，本文不宣称云已上线或真实provider已验。
