# 本机演示前端

依赖 Node.js 与 npm，先启动设计后端。复制 `.env.example` 为 `.env.local`，调整 `BACKEND_BASE_URL` 与公开跳转链接；模型密钥和服务凭据只配置在后端。

通用运行：`npm ci` → `npm run build` → `npm run start`（默认 5180）。其他端口可使用 `npx next start --hostname 127.0.0.1 --port 3092`。构建时确定 API 反代地址，改地址后需重新构建。

本次独立候选：`npm run build:demo-access` 与 `npm run start:demo-access`，使用 `.next-demo-access-20261010`，3092 反代 8092。运行 owner 在确认旧服务安全后切换，本窗口不切服务。

后端开启 `DESIGN_DEMO_ACCESS_ENABLED=true`，保持 managed 与 AUTH_REQUIRED=true，配置精确 allowed origins、现有 active designer/manager 稳定主体。可用 `/demo/designer`、`/demo/manager`；默认首页自动恢复服务器身份。首次进入由服务端初始化合成身份，不建立项目、run、审批或预算。登录、注册书签自动转演示入口。关闭开关并重新加载后端，原账号登录恢复，demo 上下文撤权；前端不依赖公开开关。

页面标明合成演示身份；设置页冻结密码与人员账号操作，保留角色切换。草稿、未确认请求在 demo 模式按实例/范围/主体/角色/上下文隔离，普通登录原草稿保持。请求未知先读取原结果，不自动重发；角色切换会取消旧读取并隐藏原投影。切换不批准业务、不分配模型预算，能力仍以服务器 capabilities 为准。

发布只包含源代码和合法公开素材。不要上传 `.env.local`、密钥、运行数据库、私人照片、构建缓存或真实业务数据。此文件无凭据与本机目录依赖。
