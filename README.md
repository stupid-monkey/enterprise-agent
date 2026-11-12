# Enterprise Agent / 知序

企业知识与文档工作台的开发项目。前端使用 Vue 3、TypeScript 和 Vite；API 使用 FastAPI。代码提供多角色开发登录、资料上传与审核、授权问答、部门会议室、DOCX/PPTX 草稿、长期记忆实验及评测脚本。

## 功能与边界

- 资料读取由服务端按组织、部门、密级、发布状态和显式拒绝规则判定；问答和生成文档时重新核验来源。
- 企业问答以授权证据和引用为基础；普通对话走独立模型路径，不读取企业资料。
- 文档生成采用 Supervisor、Knowledge、Document 和 Validator 工作流；失败可按已保存节点继续，产物需要人工核对。
- 长期记忆、PostgreSQL 迁移、混合检索及 Redis 缓存有本地实验或可选配置。部分产品数据仍使用 SQLite，不能视为生产级多机部署。

本仓库只发布源码、测试和合成评测输入。内部开发计划、架构/面试文档、评测结果、用户资料、数据库及运行凭据均不在此仓库中。不要向第三方模型端点发送真实企业资料，除非已完成相应的数据合规审批。

## Docker 本地启动

需要 Docker Desktop 和 Compose。项目根目录执行：

```powershell
Copy-Item .env.example .env
docker compose up -d --build api web
```

打开 <http://127.0.0.1:8080/>。API 文档位于 <http://127.0.0.1:8001/docs>。默认开发账号仅用于本机演示；不要将 `ENABLE_DEV_ACCOUNTS=true` 的配置部署到共享环境。未配置模型密钥时，授权资料摘录和离线测试仍可使用，普通模型对话与 AI 生成不可用。

模型接入使用 `.env` 中的 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和各模型 ID。`.env` 已被 Git 忽略。默认 Compose 以 SQLite 运行；PostgreSQL 和 Redis 服务是可选实验组件，需按目标环境单独配置。

## 源码开发

需要 Python 3.12 和 Node.js 22。PowerShell 示例：

```powershell
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements-api.txt
Copy-Item .env.example .env
& .\.venv\Scripts\python.exe -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
```

在另一个终端启动前端：

```powershell
Set-Location frontend
npm ci
npm run dev
```

Vite 默认在 <http://localhost:5173/>，将 `/api` 代理到本机 8000 端口。前端也提供不访问受保护 API 的界面演示入口。

## 验证

```powershell
$env:MEMORY_BACKEND='sqlite'
$env:CONVERSATION_BACKEND='sqlite'
& .\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -q
Set-Location frontend
npm ci
npm run test:markdown
npm run build
```

`eval/` 保留可复现的脚本与合成用例，不包含历史运行报告。某些 PostgreSQL 合约测试需要隔离测试库。测试和本地基准并不能证明真实企业数据下的准确率、可用率或生产性能。
