# 知序前端

Vue 3、TypeScript 和 Vite 实现的企业 Agent 工作台。登录后的资料、问答、会议室及文档/PPT 操作调用后端 API；界面演示入口使用合成数据。

```bash
npm ci
npm run dev
```

默认访问 <http://localhost:5173/>；开发服务器将 `/api` 代理到 `http://127.0.0.1:8000`。构建与基础展示检查：

```bash
npm run test:markdown
npm run build
```

前端筛选和按钮状态只改善交互，权限与来源版本由服务端核验。
