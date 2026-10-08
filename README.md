# 款式工场 · 鞋服设计 Agent

从自然语言和参考图开始，与设计助手整理需求、规划不同风格方向、生成并修改鞋服款式。选定满意图片后，可在独立方案页查看设计依据，下载图片与首版打样沟通资料。3D 预览需单独接通服务。

## 本地启动

需要 Python 3.12+、Node.js 和 npm。后端配置项可参考 `.env.example`，在本地 `.env` 中填写；前端本地配置可写入 `frontend/.env.local`。

```bash
uv sync --dev
uv run uvicorn app.main:app --host 127.0.0.1 --port 8020
```

另开终端启动前端：

```bash
cd frontend
npm ci
npm run dev
```

打开 [http://localhost:5180/](http://localhost:5180/)。后端健康检查为 `GET /api/health`。如果采用生产模式运行前端，修改代码后须重新 `npm run build` 并重启 `npm run start`。

把 `.env.example` 复制为 `.env`。出图默认走火山方舟；若中转接口拒绝参考图数组，把 `IMAGE_REFERENCE_FORMAT` 设为 `single`。

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `IMAGE_API_KEY` | 空 | 出图密钥，只留在后端 |
| `IMAGE_BASE_URL` | `https://ark.cn-beijing.volces.com/api/v3` | 出图接口根地址 |
| `IMAGE_MODEL` | 空 | 图像模型 ID |
| `IMAGE_SIZE` | `2K` | 出图尺寸。方舟 seedream 5.0 要求至少 3,686,400 像素 |
| `IMAGE_REFERENCE_FORMAT` | `list` | `list` 把参考图作为 JSON 数组发送；`single` 只发送最相关的一张字符串（正在修改的图或当前选定设计优先，否则第一张用户参考图） |

CMS 相关项只放在后端：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `CMS_BASE_URL` | `http://127.0.0.1:8001` | CMS 根地址 |
| `CMS_API_KEY` | 空 | 设计 Agent 的 CMS 密钥，请求头 `X-API-Key`，不会下发到前端 |
| `CMS_TIMEOUT` | `10` | 调用 CMS 的超时秒数 |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8020` | 拼回传图片的绝对地址 |

前端跳转写在 `frontend/.env.local`（可从 `frontend/.env.example` 复制）：

| 变量 | 本地默认 |
| --- | --- |
| `NEXT_PUBLIC_LINK_OUTFIT` | `http://localhost:3010` |
| `NEXT_PUBLIC_LINK_PRODUCT` | `http://localhost:3000` |
| `NEXT_PUBLIC_LINK_DESIGN` | `http://localhost:5180` |
| `NEXT_PUBLIC_LINK_CMS` | `http://localhost:8001` |

## 当前主流程

1. 对话描述款式，可上传草图、面料照片或参考图；助手整理设计要求。
2. 确认要求，查看并筛选风格方向，再生成候选图片。
3. 比较候选款，对单款提出修改并生成新版本；图片及逐项检查结果保存在项目中。
4. 确认一款后进入方案页，查看设计依据并下载图片与打样沟通资料。未确定的尺寸、面料和工艺会标明待核对，示意结构图不能直接当纸样使用。

顶部“历史对话”可打开现有项目。旧 `/workbench` 地址会转到当前入口。

## 和 CMS 一起演示

链路是：穿搭信号进入 CMS → 产品 Agent 形成证据包 → 负责人在 CMS 批准 → 本设计 Agent 读取已批准的包并出图 → 把设计回写成草稿 → 负责人在 CMS 入档。CMS 页面上应能看到信号、证据包、设计这三段。

1. 启动 CMS（默认 `http://127.0.0.1:8001`），把设计 Agent 的 API Key 写入本仓库 `.env` 的 `CMS_API_KEY`。
2. 在 CMS 中准备一条证据包，并由产品负责人把状态批成 `approved`。未批准时导入会提示“尚未批准”。
3. 按上面的命令启动本仓库后端 `:8020` 和前端 `:5180`。
4. 打开 [http://localhost:5180/](http://localhost:5180/)，在新对话里点「从 CMS 导入证据包」，输入包编号并读取。卡片会显示需求描述、约束、关联信号数和版本；原始 JSON 在折叠块里。
5. 点「用这个需求开始设计」。需求会作为第一条消息进入现有的整理要求流程，项目上会出现证据包编号。
6. 按主流程确认要求、规划方向、生成并确认一款，打开方案页。
7. 在方案页选择回传状态（采纳 / 不采纳 / 需补证），核对可编辑的设计说明，确认写入 CMS。成功后显示 `DSG-YYYYMMDD-` 设计编号，状态为 `draft`（待产品负责人入档）。同一设计再次提交使用同一个编号，不会在 CMS 里重复建档。
8. 回到 CMS，由产品负责人把这条设计草稿入档。

本服务对外的接口是 `GET /api/cms/packages/{package_id}` 和 `POST /api/projects/{id}/cms-response`。前端只调用这两个地址，不接触 CMS 密钥。

## 代码与数据

- `app/main.py`：项目创建、健康检查和 Agent 路由入口。
- `app/agent/`：需求记录、风格规划、生成、检查、版本、素材、打样资料与可选 3D。
- `frontend/components/design/`：对话、方向、版本和方案页界面。
- `frontend/lib/agent-api.ts`：当前前端 API 客户端。
- `data/app.sqlite3`、`data/assets/`：已有项目及生成素材。旧工作台的历史表仍保存在数据库中，清理代码时不会删除或迁移这些数据。

旧“五道门”工作台、示例库和静态验收页已从运行代码中移除。

## 产品文档

- [鞋服智能设计 Agent PRD](<docs/PRD/鞋服设计agent PRD/PRD-鞋服智能设计Agent.md>)

## 验证

```bash
uv run pytest tests -q
uv run ruff check app tests scripts
cd frontend
npm test
npm run typecheck
npm run lint
npm run build
```


## 2026-09-30：检查恢复与统一入口

图片已生成但检查失败时，可在该方案点击“重新检查已有图片”；此操作只核验保存的原图，不重新生成。检查完成后先查看逐项结果，再点击“确认这款”。后台会保留原任务的失败记录，重复提交使用同一请求编号；需求已改变时会拒绝以旧要求确认。

前端可在构建时设置 `NEXT_PUBLIC_BASE_PATH=/design` 部署到同一网站子路径；同平台其余入口为 `/styling/`、`/product/` 和 CMS 首页 `/`。部署、费用和验收范围见相邻 CMS平台/deploy/unified/README.md。当前为本机适配通过，不能视为公网发布完成。


### 2026-09-30 线上 CMS 回传契约

CMS 的 `version` 字段为字符串（如 `v1`），设计端发送时必须转换为 `v{design_index}`。设计应用内部版本编号继续使用数字。验收以真实 CMS 接受回传、审核入档及正反向追溯为准，不能只用放行所有参数的模拟响应证明联通。


### CMS需求自动同步
设计首页自动读取当前空间已批准证据包，无需手填编号；点“用这个需求开始设计”自动关联并保存快照。空列表时先到CMS批准需求，返回设计页会更新。分页、网络重试与审批权限测试：` .venv/bin/python -m pytest tests/test_cms.py -q `；前端执行 `npm test`。接口及验收见 `docs/cms-auto-import.md`。

## 图片服务返回 URL 的配置

部分图片服务会忽略 `response_format=b64_json` 并返回 URL。仅在确认服务图片域名后，设置 `AGENT_IMAGE_DOWNLOAD_HOSTS`（逗号分隔的精确域名）。默认禁用 URL 下载，不发送模型密钥、不跟随重定向，图片仍需通过格式检查并保存到持久化目录。接口契约与验收见 [图片接口兼容说明](docs/image-provider-compatibility.md)。

当前线上入口（2026-10-08 恢复）：https://s357brv8j8f9gdhvbu9a9.apigateway-cn-beijing.volceapi.com/design/ 。免注册直接体验，记录按浏览器访客空间隔离。旧域名已停用。
