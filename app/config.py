"""集中读取环境变量配置，web / authoritative / resolver 三个进程共用。"""
import os


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


# 网页上可管理的两个固定域名
DOMAINS = [d.strip().lower() for d in _env("DOMAINS", "app1.internal,app2.internal").split(",") if d.strip()]

# SQLite 数据库（web 写、authoritative 读，通过共享卷同一个文件）
DB_PATH = _env("DB_PATH", "/data/dns.db")

# DNS 监听
LISTEN_ADDR = _env("LISTEN_ADDR", "0.0.0.0")
LISTEN_PORT = int(_env("LISTEN_PORT", "53"))

# resolver -> authoritative 回源
UPSTREAM_HOST = _env("UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(_env("UPSTREAM_PORT", "5301"))
UPSTREAM_TIMEOUT = float(_env("UPSTREAM_TIMEOUT", "3.0"))

# resolver 自带的 HTTP 状态接口
STATS_HOST = _env("STATS_HOST", "127.0.0.1")
STATS_PORT = int(_env("STATS_PORT", "8081"))

# 网页后端访问解析器的地址
RESOLVER_HOST = _env("RESOLVER_HOST", "127.0.0.1")
RESOLVER_PORT = int(_env("RESOLVER_PORT", "5354"))
RESOLVER_STATS = _env("RESOLVER_STATS", "http://127.0.0.1:8081")

# 网页服务
WEB_HOST = _env("WEB_HOST", "0.0.0.0")
WEB_PORT = int(_env("WEB_PORT", "8000"))

# 默认种子数据
DEFAULT_POS_TTL = int(_env("DEFAULT_POS_TTL", "30"))
DEFAULT_NEG_TTL = int(_env("DEFAULT_NEG_TTL", "15"))
DEFAULT_DELAY_MS = int(_env("DEFAULT_DELAY_MS", "0"))

# SOA 记录内容（NXDOMAIN 负缓存 TTL 取自 SOA 的 minimum 字段）
SOA_MNAME = _env("SOA_MNAME", "ns.internal.")
SOA_RNAME = _env("SOA_RNAME", "hostmaster.internal.")
SOA_SERIAL = 2026010101
SOA_REFRESH = 7200
SOA_RETRY = 3600
SOA_EXPIRE = 1209600
