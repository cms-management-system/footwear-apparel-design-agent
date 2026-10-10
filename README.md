# 款式工场 · 鞋服设计 Agent

面向鞋服团队的设计工作台。设计人员可以从一句话或空白项目开始，在项目画布中持续讨论、整理设计要求、生成效果图或设计稿；也可以承接产品负责人已批准的需求。设计负责人负责接收、分派和人工审核，上游批准版本、设计过程与回传结果保留关联。

当前版本包含创作画布、自然语言对话、项目重命名与归档、临时免登录双角色入口，以及公网部署适配。源码由本机工作副本独立快照交付；不包含真实密钥、私人照片、运行数据库、生成资产、依赖安装目录或构建输出。

## 在线入口

- [设计人员](https://s357brv8j8f9gdhvbu9a9.apigateway-cn-beijing.volceapi.com/v2/design/demo/designer)：自主创作和已分派项目。
- [设计负责人](https://s357brv8j8f9gdhvbu9a9.apigateway-cn-beijing.volceapi.com/v2/design/demo/manager)：需求收件、分派与设计审查。
- [三产品导航与业务流程](https://s357brv8j8f9gdhvbu9a9.apigateway-cn-beijing.volceapi.com/v2/portal)：穿搭、产品管理、设计的六个角色入口。

这些入口使用服务端合成演示身份。免登录不代替角色授权、项目归属、人工审批或服务间凭据。公开实例的数据与本机私有工作数据分离；演示资料不能用来证明真实市场需求或经营收益。

## 本地启动

需要 Python 3.12+、uv、Node.js 与 npm；本轮 Linux 构建使用 Node.js 22。在新克隆或独立源码目录操作，保留已有实例的配置和数据。

```sh
cp -n .env.example .env
uv sync --dev
uv run uvicorn app.main:app --host 127.0.0.1 --port 8092
```

另开终端启动前端：

```sh
cd frontend
npm ci
npm run build:demo-access
npm run start:demo-access
```

打开 [本地设计人员](http://localhost:3092/demo/designer) 或 [本地设计负责人](http://localhost:3092/demo/manager)。模板在空库建立两个合成角色；不覆盖既有用户。模型密钥和收费执行默认关闭，仍可创建空白项目、保存标题及画布布局；模型不可用时显示实际受限原因。

本地角色、持久化、关闭演示访问及恢复普通认证的完整说明见 [LOCAL_DEMO.md](LOCAL_DEMO.md)。本地模式只监听回环地址，公网部署使用下述独立配置。

## 公网部署

这是 Next.js 前端与 Python API 的双进程应用。当前实例使用前端 `127.0.0.1:3192`、API `127.0.0.1:8192`，外部路径为 `/v2/design`；产品服务内部适配入口为 `127.0.0.1:8291`。Python 采用一个 Uvicorn 进程及嵌入队列 worker。

后端初始化、私有配置白名单、用途凭据、固定代理字段、SQLite/资产持久化和恢复步骤见 [公网部署说明](deployment/DESIGN_PUBLIC_DEPLOY.md)。依赖锁位于 `pyproject.toml`、`uv.lock` 和 `deployment/requirements-public.lock.txt`；初始化器为 `scripts/public_design_runtime.py`。该初始化器绑定当前部署的实例、域名和目录，迁移到其他环境需由部署负责人适配这些配置。

前端公开配置见 [frontend/.env.public.example](frontend/.env.public.example)，只含公开地址和构建设置。完成后端配置后，在独立的前端发布目录执行：

```sh
cd frontend
npm ci
cp -n .env.public.example .env.production.local
npm run build
npx next start --hostname 127.0.0.1 --port 3192
```

网关保留 `/v2/design` 前缀到 Next，Next 将 API 转发到 Python；静态文件位于 `/v2/design/_next`。修改公开前端配置后需重新构建并重启。若采用 standalone 打包，须同时保留对应构建的静态目录及 public 目录（如有）。

模型与机器凭据仅进入私有服务端配置。后端模板关闭真实供应商调用；运行负责人配置供应商与执行策略后，才可执行明确的创作动作。公网保护包含每次动作预算、累计 20 次文本/4 次图片调用、单并发和未知请求不退款；这些是调用上限，不是作品数。重启、换角色或会话不能清零累计账本，原 validation 用途耗尽后也不能转为普通创作额度。

## 协作和能力边界

- 产品负责人批准具体需求版本，设计负责人接收并分派；设计结果由负责人审核后回传产品记录。机器接口使用不同用途凭据，人员会话不能替代。
- 自主项目保留自己的来源和权限，不伪造上游批准。画布布局和对话可持续保存，生成结果仍是待审核设计候选。
- 效果图不宣称是真实试穿、尺码结果、可生产纸样或已投产方案。历史 CMS、3D 与其他兼容代码不作为本轮已验证交付能力。
- 本轮公网验证覆盖入口、角色、持久化与服务用途鉴权，未重新验证付费模型效果；最终视觉质量由使用者审核。

## 验证与目录

`app/` 为 API、权限、设计流程和执行账本；`migrations/` 含数据库迁移；`frontend/` 为界面；`deployment/` 为公网说明、占位模板与锁定依赖；`tests/` 为隔离测试。当前快照不携带运行数据或个人素材。

```sh
uv run pytest tests -q
cd frontend
npm test
npm run typecheck
npm run lint
npm run build
```

历史 `/design/`、8020/5180、CMS 中枢和旧 3D 文档属于早期模式，不是当前公网入口或上线验收依据。本文件以 `/v2/design` 的独立设计工作台为当前说明。
