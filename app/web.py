"""FastAPI 操作网页后端。

- 管理两个固定域名的 A 记录（设置 / 删除）
- 配置负缓存 TTL、每个域名的权威回包延迟、丢包故障开关
- 发起真实 UDP 查询（经缓存解析器），并汇总解析器侧的剩余 TTL / 回源次数
"""
import json
import os
import random
import socket
import urllib.error
import urllib.request
from contextlib import asynccontextmanager

from dnslib import QTYPE, RCODE, DNSRecord
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db

RCODE_TEXT = {
    RCODE.NOERROR: "NOERROR",
    RCODE.FORMERR: "FORMAT-ERROR",
    RCODE.SERVFAIL: "SERVFAIL",
    RCODE.NXDOMAIN: "NXDOMAIN",
    RCODE.NOTIMP: "NOT-IMPLEMENTED",
    RCODE.REFUSED: "REFUSED",
}


class RecordIn(BaseModel):
    ip: str = Field(pattern=r"^(\d{1,3}\.){3}\d{1,3}$")
    ttl: int = Field(ge=1, le=86400)


class NegTtlIn(BaseModel):
    neg_ttl: int = Field(ge=0, le=86400)


class DelayIn(BaseModel):
    delay_ms: int = Field(ge=0, le=30000)


class DropIn(BaseModel):
    dropped: bool


class QueryIn(BaseModel):
    name: str
    timeout_ms: int = Field(ge=100, le=10000)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="机房 DNS 改址/撤销演示", lifespan=lifespan)


def _normalized(name: str) -> str:
    n = (name or "").strip().lower().rstrip(".")
    if n not in config.DOMAINS:
        raise HTTPException(status_code=400, detail=f"只允许管理这两个域名：{', '.join(config.DOMAINS)}")
    return n


def _valid_ipv4(ip: str) -> None:
    try:
        socket.inet_aton(ip)
    except OSError:
        raise HTTPException(status_code=400, detail="非法 IPv4 地址")


def _resolver_stats(name: str | None = None) -> dict:
    url = config.RESOLVER_STATS.rstrip("/") + "/stats"
    if name:
        url += "?name=" + urllib.request.quote(name)
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {"error": f"解析器状态接口不可用：{exc!r}", "names": {}}


@app.get("/api/state")
def get_state():
    conn = db.connect()
    try:
        domains = []
        for name in config.DOMAINS:
            row = db.get_record(conn, name)
            domains.append(
                {
                    "name": name,
                    "record": None
                    if row is None
                    else {
                        "ip": row["ip"],
                        "ttl": row["ttl"],
                        "updated_at": row["updated_at"],
                    },
                    "delay_ms": db.get_int_setting(conn, f"delay_{name}", config.DEFAULT_DELAY_MS),
                    "dropped": db.get_bool_setting(conn, f"dropped_{name}", False),
                }
            )
        settings = {
            "neg_ttl": db.get_int_setting(conn, "neg_ttl", config.DEFAULT_NEG_TTL),
        }
    finally:
        conn.close()
    return {
        "domains": domains,
        "settings": settings,
        "resolver": _resolver_stats(),
        "resolver_endpoint": f"{config.RESOLVER_HOST}:{config.RESOLVER_PORT}",
    }


@app.post("/api/domains/{name}/record")
def set_record(name: str, body: RecordIn):
    n = _normalized(name)
    _valid_ipv4(body.ip)
    db.upsert_record(n, body.ip, body.ttl)
    return {"ok": True, "name": n, "ip": body.ip, "ttl": body.ttl}


@app.delete("/api/domains/{name}/record")
def remove_record(name: str):
    n = _normalized(name)
    db.delete_record(n)
    return {"ok": True, "name": n}


@app.put("/api/settings/neg-ttl")
def set_neg_ttl(body: NegTtlIn):
    db.set_setting("neg_ttl", str(body.neg_ttl))
    return {"ok": True, "neg_ttl": body.neg_ttl}


@app.put("/api/domains/{name}/delay")
def set_delay(name: str, body: DelayIn):
    n = _normalized(name)
    db.set_setting(f"delay_{n}", str(body.delay_ms))
    return {"ok": True, "name": n, "delay_ms": body.delay_ms}


@app.put("/api/domains/{name}/drop")
def set_drop(name: str, body: DropIn):
    n = _normalized(name)
    db.set_setting(f"dropped_{n}", "1" if body.dropped else "0")
    return {"ok": True, "name": n, "dropped": body.dropped}


@app.post("/api/query")
def real_query(body: QueryIn):
    """发起真实 UDP DNS 查询（不是模拟），并附上解析器侧统计。"""
    n = _normalized(body.name)
    timeout = body.timeout_ms / 1000.0
    q = DNSRecord.question(n, qtype="A")
    q.header.id = random.randint(1, 65535)
    result: dict = {
        "name": n,
        "dns_id": int(q.header.id),
        "timeout_ms": body.timeout_ms,
        "server": f"{config.RESOLVER_HOST}:{config.RESOLVER_PORT}",
    }
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(q.pack(), (config.RESOLVER_HOST, config.RESOLVER_PORT))
            data, addr = sock.recvfrom(65535)
        resp = DNSRecord.parse(data)
        answers = [
            {"name": str(rr.rname), "type": QTYPE[rr.rtype], "ttl": int(rr.ttl), "rdata": str(rr.rdata)}
            for rr in resp.rr
        ]
        authority = [
            {"name": str(rr.rname), "type": QTYPE[rr.rtype], "ttl": int(rr.ttl), "rdata": str(rr.rdata)}
            for rr in resp.auth
        ]
        rcode = int(resp.header.rcode)
        result.update(
            {
                "status": "ok",
                "timed_out": False,
                "rcode": rcode,
                "rcode_text": RCODE_TEXT.get(rcode, f"RCODE-{rcode}"),
                "answer_id_matches": int(resp.header.id) == int(q.header.id),
                "aa": bool(resp.header.aa),
                "answers": answers,
                "authority": authority,
            }
        )
    except socket.timeout:
        result.update({"status": "timeout", "timed_out": True})
    except OSError as exc:
        result.update({"status": "error", "error": repr(exc)})

    stats = _resolver_stats(n)
    result["resolver_stats"] = stats.get("names", {}).get(n)
    result["resolver_global"] = {
        "total_queries": stats.get("total_queries"),
        "upstream_failures": stats.get("upstream_failures"),
    }
    return JSONResponse(result)


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT)


if __name__ == "__main__":
    main()
