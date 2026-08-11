# 生产环境部署指南

本文档面向生产部署，覆盖 API Key 配置、运行模式、监控对接与备份策略。
如只需本地体验，请参考 [README.md](../README.md) 的快速开始。

---

## 1. 环境要求

| 项 | 要求 |
|----|------|
| Python | 3.11+ |
| 内存 | ≥ 512MB（并发抓取建议 1GB） |
| 磁盘 | ≥ 1GB（checkpoints / logs / backups 持续增长） |
| 网络 | 可访问搜索引擎 API / 抓取目标站点 |

---

## 2. API Key 配置

复制 `.env.example` 为 `.env` 并填入所需 Key（`.env` 已被 gitignore，**切勿提交**）。

### 2.1 搜索引擎 Key（按推荐优先级）

| 环境变量 | 引擎 | 说明 | 获取地址 |
|----------|------|------|----------|
| `BOCHA_API_KEY` | Bocha | 国内首选，结构化返回 | bochaai.com |
| `TAVILY_API_KEY` | Tavily | AI Agent 专用搜索 | tavily.com |
| `SERPER_API_KEY` | Serper | Google 搜索代理 | serper.dev |
| `METASO_API_KEY` | Metaso | 秘塔结构化搜索 | metaso.cn |

**无需 Key 的兜底引擎**：Bing / Baidu / Sogou / 360 / DuckDuckGo（HTML 抓取，稳定性较低但零成本）。

> ⚠️ **降级行为**：未配置任何 API Key 时，系统自动使用 HTML 兜底引擎；配置了 `search_engines: [mock]` 的
> `config.json` 为测试专用，生产请务必使用 `config.production.json`。启动日志会输出实际生效的引擎列表，
> 请留意确认。

### 2.2 LLM Key（用于 Writer 润色 / 质量增强）

在 `.env` 中配置 `OPENAI_API_KEY`、`DEEPSEEK_API_KEY` 等；`llm_router.py` 支持 12 家供应商自动 fallback，
未配置时自动降级为规则模式（骨架 + TF-IDF 匹配，不做 LLM 润色），不影响流水线完成。

### 2.3 Admin API 鉴权

```bash
export ADMIN_API_KEY="your-strong-secret"
```

设置后所有 API 端点需携带 `Authorization: Bearer <key>`（`/health` 与静态资源免鉴权）。
**生产环境暴露到公网时必须设置。**

---

## 3. 运行模式

### 3.1 单次任务（CLI）

```bash
python run.py input.md -c config.production.json -o output/doc.md
```

### 3.2 常驻服务（Admin API + Dashboard）

```bash
python run.py --daemon --admin --dashboard -c config.production.json
# API 与仪表盘: http://<host>:8910
```

外部系统通过 `POST /tasks` 提交任务（支持同步/异步），详见 README 的管理 API 表格与
`GET /api/openapi.json` 在线规范。

### 3.3 MCP Server（供 Claude Desktop 等外部 Agent 调度）

```bash
python run.py --mcp
```

### 3.4 Docker

```bash
docker build -t doc-pipeline .
docker run -d --name doc-pipeline \
  -p 8910:8910 \
  -v $(pwd)/.env:/app/.env:ro \
  -v doc-pipeline-checkpoints:/app/checkpoints \
  -v doc-pipeline-logs:/app/logs \
  --env-file .env \
  doc-pipeline input.md -c config.production.json -o output/doc.md
```

镜像特性：两阶段构建、非 root 用户（uid 1001）、内置 `/health` 健康检查、checkpoints 与 logs 声明为持久化卷。

---

## 4. 监控与可观测

| 能力 | 入口 | 说明 |
|------|------|------|
| 健康检查 | `GET /health` | 总线 + Registry 状态，供 LB / k8s liveness 探针（Docker 已内置） |
| 指标 | `GET /metrics` | Prometheus 格式，直接接入 Prometheus → Grafana |
| 日志 | `logs/doc-pipeline_*.jsonl` | 结构化 JSONL，自动轮转；`GET /api/logs` 支持在线过滤查询 |
| 告警 | `GET /api/alerts` | 熔断 / 死信 / 限流 / 预算超限事件 |
| 成本 | `GET /api/cost` | LLM 调用成本按供应商 / Agent / 时间维度统计 |
| 预算熔断 | `POST /api/cost/budget` | 设置上限，超限自动熔断 LLM 调用 |

推荐 Grafana 面板维度：任务成功率、各 Agent 耗时 P95、LLM 成本/日、熔断器状态。

---

## 5. 弹性与恢复

- **断点续传**：任务中断后 `python run.py input.md --resume --task-id <id>` 从最近 checkpoint 恢复；
  服务启动时加 `--recover` 自动恢复 SQLite 任务队列中的中断任务。
- **熔断 / 限流**：内置 CLOSED/OPEN/HALF_OPEN 熔断器与令牌桶限流器，无需外部组件。
- **死信队列**：`GET /dlq` 查看失败消息，`POST /dlq/<id>/replay` 重放。

---

## 6. 备份策略

| 数据 | 位置 | 建议 |
|------|------|------|
| 断点与报告 | `checkpoints/` | 每日增量备份，保留 30 天 |
| 文档版本 | `versions/` | 含 diff 和回滚能力，建议与代码库同级备份 |
| 总线 / 任务数据库 | `bus_data/*.db` | SQLite（WAL 模式），停机后直接拷贝文件即可 |
| 产出文档 | `output/` + `backups/` | SafeWriter 原子写入 + 自动备份（默认 7 天 / 20 份，可在 config 调整） |

`config.production.json` 中可调项：`safe_writer.backup_ttl_days`、`safe_writer.max_backups`。

---

## 7. 上线前检查清单

- [ ] `.env` 已配置所需搜索 / LLM Key，且未提交到 git
- [ ] `ADMIN_API_KEY` 已设置（对外暴露时）
- [ ] 使用 `config.production.json` 启动（`fail_fast: false`，多引擎）
- [ ] 启动日志确认生效的搜索引擎列表
- [ ] `GET /health` 返回健康、`GET /metrics` 可被抓取
- [ ] 备份策略已覆盖 checkpoints / versions / bus_data
