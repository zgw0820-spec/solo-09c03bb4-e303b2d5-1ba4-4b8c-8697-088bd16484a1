# 机房 DNS 改址 / 撤销演示台

一个面向机房值班场景的最小 DNS 实验平台：在网页上给两个内网域名设置 / 删除 A 记录、
调整 TTL 与权威回包延迟，然后发起**真实 UDP DNS 查询**，观察客户端何时拿到新答复、
剩余 TTL 如何递减、解析器向权威回源了几次。

## 架构

```
浏览器 ──HTTP──> web (FastAPI, 8000)
                  │  写记录/配置                读记录/配置（每查询短连接）
                  ▼                            ▲
               SQLite (共享卷 dns-data)        │
                                               │
客户端/网页后端 ──UDP 53──> resolver (缓存解析器, dnslib)
                               │  缓存未命中/过期才回源（UDP 53）
                               ▼
                          authoritative (权威 DNS, dnslib, AA)
                               │
                               └─ 每域名可配回包延迟 / 静默丢包（模拟故障）
```

- **authoritative**：线程化 UDP 权威服务。A 记录存在则回 AA 应答（TTL 取记录值）；
  不存在则回 `NXDOMAIN` 并在 authority 段带 SOA，SOA 的 `minimum` 即负缓存期限。
- **resolver**：线程化 UDP 缓存解析器，另带一个 HTTP 状态口（8081）暴露
  每域名的回源次数、缓存种类、剩余 TTL、单飞状态、最近错误。缓存**只在内存**，
  容器不挂卷，重启即空。
- **web**：FastAPI + 单文件原生 JS 页面，SQLite 存记录与设置，启动时自动建表播种。

## 启动（Docker Compose）

```bash
docker compose up -d --build
# 网页：     http://localhost:8000
# 解析器：   127.0.0.1:5354/udp（dig @127.0.0.1 -p 5354 app1.internal）
# 状态接口： http://localhost:8081/stats
```

无 Docker 的环境可用本地等价进程（需要一个含 fastapi/uvicorn/dnslib 的 Python）：

```bash
scripts/selftest-local.sh        # 自动起三进程、跑自测、重启验证、清理
```

## 网页功能

1. 两个固定域名（默认 `app1.internal`、`app2.internal`，可用环境变量 `DOMAINS` 改）
   的 A 记录**设置 / 改址 / 删除（撤销）**，记录 TTL 可配。
2. 全局**负缓存 TTL**（写入权威 SOA minimum）。
3. 每域名**权威回包延迟（毫秒）**与**静默丢包开关**（模拟权威不可达）。
4. 输入**查询等待上限（毫秒）**后发起真实 UDP 查询，展示结果码、应答、
   答复 DNS ID 是否与查询一致、剩余 TTL、缓存种类、该域名累计回源次数。
5. 解析器实时状态表，可 2 秒自动刷新。

## 关键语义（与需求逐条对应）

| 需求 | 实现位置与行为 |
| --- | --- |
| A 按记录 TTL 失效 | `resolver.CacheEntry.expiry = now + 应答最小 TTL`；命中时按剩余秒数重写 RR TTL |
| NXDOMAIN 按 SOA 负缓存期限失效 | 权威 NXDOMAIN 带 SOA；解析器取 `SOA.minimum` 作为负缓存 TTL |
| 到期后按权威现况重查 | 缓存仅在 `fresh()` 时命中；过期即重新回源，不预先刷新 |
| 回源失败不沿用过期答复 | 失败路径不写/不读缓存，直接 `SERVFAIL`；过期条目不会被返回 |
| 回源失败不缓存为 NXDOMAIN | 仅缓存真实 `NOERROR`/`NXDOMAIN`/`NODATA`；超时/异常/SERVFAIL 一律不缓存 |
| 同名并发冷查询只回源一次 | `inflight` 字典 + `threading.Event` 单飞；后来者在同一 Flight 上等待 |
| 单个请求超时不取消其他等待者 | 等待者超时只让自己回 SERVFAIL；leader 的回源 socket 与 Event 不受影响 |
| 答复对应各自 DNS ID | 缓存模板 `id=0`，发出前用每个客户端自己的查询 ID 重写（自测逐包校验） |
| 慢域名不阻塞另一域名 | 权威与解析器均为线程化 UDP，单飞按域名分键 |
| 重启后不沿用旧缓存 | 缓存纯内存、容器无数据卷；`--restart-check` 自测验证计数与缓存归零 |

## 自测

```bash
scripts/selftest.sh           # Docker Compose 环境：33 项端到端断言 + 重启验证
scripts/selftest-local.sh     # 本地三进程等价跑法（本仓库即以此验证）
```

覆盖 9 组场景：冷查询/缓存命中、正 TTL 到期改址、NXDOMAIN 负缓存、
负缓存期内恢复记录、回源失败（SERVFAIL、不缓存、不沿用）、8 路同名单飞、
等待者超时隔离、慢域名互不阻塞、重启缓存清空。

## 目录

```
app/
  config.py          环境变量集中配置
  db.py              SQLite 建表 / 记录与设置读写（WAL）
  authoritative.py   权威 DNS（dnslib，线程化 UDP）
  resolver.py        缓存解析器 + 单飞 + HTTP 状态口
  web.py             FastAPI 接口（含真实 UDP 查询代理）
  static/index.html  操作网页
scripts/
  selftest.py        端到端断言（真实 UDP）
  selftest.sh        compose 跑法
  selftest-local.sh  本地进程跑法
docker-compose.yml   authoritative / resolver / web 三服务
Dockerfile
```

## 说明与边界

- 实验聚焦 A 记录：解析器对非 A 查询直接转发、不缓存。
- 解析器只服务本实验域名的观察需求，没有做递归/转发链路、DNSSEC、TCP/53。
- “静默丢包”是测试夹具，用于确定性复现权威不可达；生产环境请勿保留。
