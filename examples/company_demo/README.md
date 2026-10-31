# TaskDesk：跨部门交付演示

本次真实分工由后端执行者编写 `app.py`，前端执行者编写 `web/`，独立验证者编写 HTTP、数据库重启和浏览器验收。它是可操作的任务清单，用来验证完整交付，不是在线公司管理平台。

从仓库根目录启动（仅监听本机）：

```bash
python examples/company_demo/app.py --host 127.0.0.1 --port 8080 --db /tmp/cloudkit-taskdesk.sqlite3
```

浏览器打开 http://127.0.0.1:8080 。支持添加任务、完成/恢复、计数与错误反馈；重启后保留同一数据库中的任务。没有登录或公网 TLS，不应直接暴露公网。

独立验收：

```bash
python examples/company_demo/verify.py --output verification/company-http.json
npm --prefix examples/company_demo ci
npm --prefix examples/company_demo exec -- playwright install chromium
node examples/company_demo/browser-test.cjs --output verification/company-browser
```

浏览器安装需要访问 Playwright 下载服务。CI 在独立 Chromium 中测试七个真实交互场景，并上传桌面和移动尺寸截图。HTTP 验收不冒充浏览器测试；确定性流程演练不冒充模型调用。详见 [公司协调器](../../docs/company.zh-CN.md)。
