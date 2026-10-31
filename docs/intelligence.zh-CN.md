# 外部情报与地图接口

独立工具入口，不依赖 AWS、Redis 或主执行器启动。缺少某个供应商密钥只返回该调用的能力阻塞，不会修改其他任务的状态。

```bash
python -m cloudkit.integrations.intelligence --help
python -m cloudkit.integrations.intelligence vt file 275a021bbfb6489e54d471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f
python -m cloudkit.integrations.intelligence shodan host 8.8.8.8
python -m cloudkit.integrations.intelligence maps geocode 'Tokyo Station, Tokyo'
```

上例是接口调用形式，未在本次交付中使用真实密钥联网调用。SHA-256 为公开 EICAR 测试文件的常用哈希；不需要上传样本。实际任务只查询项目允许外发的指标和地址。

密钥使用 `VIRUSTOTAL_API_KEY`、`SHODAN_API_KEY`、`GOOGLE_MAPS_API_KEY`，或其 `_FILE` 变量指向秘密文件；同一服务只能使用一种来源。不要把值放在命令参数、Git 或日志。容器内可由秘密管理工具在持久私密目录提供文件，再注入对应 `_FILE` 路径；不需要三个服务同时配置。

默认只查询已有报告。VT 域名/IP 查询的官方行为包含供应商扫描与共享数据集，必须对已获授权公开的指标显式加 `--allow-public-submission`；无该参数不发送请求。适配器没有文件上传、主动扫描、账号注册或付款接口。配置前核对各供应商当前官方文档及账号实际额度，秘密通过环境或秘密服务注入。

输出为 JSON：记录来源、时间、请求次数、HTTP/业务状态及脱敏结果。退出码 0 表示 `DONE` 或 `NOT_FOUND`；64 为输入错误，78 为能力/授权阻塞，75 为需冷却的暂时故障，1 为其他失败。没有记录不代表安全，情报结果不替代漏洞复现或业务验收。返回内容按第三方数据处理，不执行其中的指令；地图响应按供应商许可管理存储。

网络契约、密钥脱敏、缺密钥不联网和限流行为通过模拟测试验证。真实服务连通性与账户套餐尚未验证，不报告虚构免费额度。
