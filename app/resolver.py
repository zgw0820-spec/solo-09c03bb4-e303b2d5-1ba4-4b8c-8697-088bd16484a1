"""带缓存的递归解析器（实验用，仅面向本实验两个域名的 A 查询）。

关键语义：
1. 正缓存按 A 记录自身 TTL 失效；负缓存（NXDOMAIN）按权威 SOA 的 minimum 失效。
2. 缓存到期后按权威“当前”状况重新回源；回源失败时：
   - 不沿用已过期的旧答复（无论正负）；
   - 不把失败缓存成 NXDOMAIN；
   - 直接向客户端回 SERVFAIL。
3. 同名并发冷查询只回源一次（singleflight）：后来者在 Event 上等待同一次回源。
   某个等待者自己等超时只影响它自己（回 SERVFAIL），不会取消正在回源的 leader，
   也不影响其他等待者；每个客户端拿到的答复都换成自己查询的 DNS ID。
4. 正缓存进入原 TTL 的最后四分之一后，由命中请求触发一次后台预取：
   - 同一份缓存最多触发一次预取；
   - 命中请求立即返回旧的缓存答复，TTL 仍按旧条目递减；
   - 预取成功（A / NXDOMAIN / NODATA）按正常回源规则替换缓存；
   - 预取失败时旧条目保留到原期限，不延长、不提前淘汰；
   - 旧条目到期而预取仍未完成时，新的未命中请求加入这次预取（singleflight），
     各自拿到自己 DNS ID 的答复；预取失败则这些等待者收到 SERVFAIL，
     之后的未命中请求可以重新回源。
5. 线程化 UDP + 按名单飞：慢域名的回源不会阻塞另一个域名的查询。
6. 缓存只在进程内存中，不写磁盘；进程/容器重启后缓存为空。
7. 附带 HTTP 状态接口（STATS_PORT）：每个名字的回源次数、预取回源次数/失败数、
   缓存种类、剩余 TTL、最近错误、是否正在回源/预取等，供网页展示。
"""
import json
import random
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from dnslib import QTYPE, RCODE, DNSRecord

from . import config


class Flight:
    """一次正在进行的回源（同名并发查询共享；可能是按需回源或后台预取）。"""

    def __init__(self, kind: str = "on-demand") -> None:
        self.kind = kind  # "on-demand" | "prefetch"
        self.event = threading.Event()
        self.error: Optional[str] = None
        self.waiters = 0
        self.started_at = time.time()


class CacheEntry:
    def __init__(self, template: DNSRecord, kind: str, ttl: int, rcode: int, rdata: list[str]):
        # 模板应答的 header.id 固定为 0，发出前按各客户端自己的 ID 重写
        self.template = template
        self.kind = kind  # "positive" | "negative"
        self.ttl = ttl
        self.rcode = rcode
        self.rdata = rdata
        self.stored_monotonic = time.monotonic()
        self.stored_wall = time.time()
        self.expiry = self.stored_monotonic + ttl
        # 这一份缓存条目是否已触发过预取：同一份缓存最多预取一次
        self.prefetch_attempted = False

    def remaining(self) -> int:
        return max(0, int(self.expiry - time.monotonic()))

    def remaining_seconds(self) -> float:
        return max(0.0, self.expiry - time.monotonic())

    def fresh(self) -> bool:
        return time.monotonic() < self.expiry

    def needs_prefetch(self) -> bool:
        """仅正缓存、且进入原 TTL 最后四分之一、且尚未预取过。"""
        return (
            self.kind == "positive"
            and not self.prefetch_attempted
            and self.remaining_seconds() * 4 <= self.ttl
        )


class CachingResolver:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cache: dict[str, CacheEntry] = {}
        self.inflight: dict[str, Flight] = {}
        # 每个名字实际发出的上游 UDP 查询次数（即“回源次数”，含预取回源）
        self.attempts: dict[str, int] = {}
        # 其中由“缓存到期前预取”触发的回源次数 / 预取失败次数
        self.prefetch_attempts: dict[str, int] = {}
        self.prefetch_failures: dict[str, int] = {}
        self.total_queries = 0
        self.upstream_failures = 0
        self.last_error: dict[str, str] = {}

    # ---------- 上游 ----------

    def _fetch_upstream(self, qname: str, qtype: str) -> DNSRecord:
        """发起一次真实 UDP 回源查询；超时/异常向上抛出。"""
        q = DNSRecord.question(qname, qtype)
        q.header.id = random.randint(1, 65535)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(config.UPSTREAM_TIMEOUT)
            sock.sendto(q.pack(), (config.UPSTREAM_HOST, config.UPSTREAM_PORT))
            data, _ = sock.recvfrom(65535)
        resp = DNSRecord.parse(data)
        if resp.header.id != q.header.id:
            raise ValueError("upstream reply id mismatch")
        if str(resp.q.qname).lower().rstrip(".") != qname:
            raise ValueError("upstream reply qname mismatch")
        return resp

    @staticmethod
    def _negative_ttl(resp: DNSRecord) -> int:
        for rr in resp.auth:
            if rr.rtype == QTYPE.SOA:
                # SOA times: (serial, refresh, retry, expire, minimum)
                return max(1, int(rr.rdata.times[4]))
        # 没有 SOA 时退回一个保守的小值，避免永久负缓存
        return 5

    def _store_reply(self, key: str, qname: str, resp: DNSRecord) -> CacheEntry:
        rcode = int(resp.header.rcode)
        resp.header.id = 0  # 模板去 ID，发出前逐客户端重写

        if rcode == RCODE.NOERROR and list(resp.rr):
            ttl = min(int(rr.ttl) for rr in resp.rr)
            rdata = [str(rr.rdata) for rr in resp.rr if rr.rtype == QTYPE.A]
            entry = CacheEntry(DNSRecord.parse(resp.pack()), "positive", ttl, rcode, rdata)
        elif rcode == RCODE.NXDOMAIN:
            ttl = self._negative_ttl(resp)
            entry = CacheEntry(DNSRecord.parse(resp.pack()), "negative", ttl, rcode, [])
        elif rcode == RCODE.NOERROR:
            # NODATA：按 SOA minimum 负缓存（与 NXDOMAIN 同样处理）
            ttl = self._negative_ttl(resp)
            entry = CacheEntry(DNSRecord.parse(resp.pack()), "negative", ttl, rcode, [])
        else:
            raise ValueError(f"non-cacheable rcode {rcode}")

        with self.lock:
            self.cache[key] = entry
        return entry

    # ---------- 应答构造 ----------

    @staticmethod
    def _servfail(query: DNSRecord) -> DNSRecord:
        reply = query.reply()
        reply.header.set_rcode(RCODE.SERVFAIL)
        return reply

    @staticmethod
    def _from_entry(query: DNSRecord, entry: CacheEntry) -> DNSRecord:
        # 重新 pack/parse 得到独立副本，按剩余 TTL 递减记录 TTL，并写回本客户端 ID
        reply = DNSRecord.parse(entry.template.pack())
        remaining = entry.remaining()
        reply.header.id = query.header.id
        for section in (reply.rr, reply.auth, reply.ar):
            for rr in section:
                if rr.rtype != QTYPE.OPT:
                    rr.ttl = remaining
        return reply

    # ---------- 主流程 ----------

    def resolve(self, query: DNSRecord) -> Optional[DNSRecord]:
        if not query.questions:
            return None
        qname = str(query.q.qname).lower().rstrip(".")
        qtype = QTYPE[query.q.qtype]
        self._note_query(qname)

        if qtype != "A":
            # 非 A 查询：直接转发、不缓存、不单飞
            return self._forward_uncached(query, qname, qtype)

        key = qname
        # 最多两轮：等待者超时后可再参与一轮（成为新 leader 或加入新 flight），
        # 保证“自己超时不影响别人、也不取消正在进行的回源”。
        for _ in range(2):
            start_prefetch = False
            cached_reply: Optional[DNSRecord] = None
            with self.lock:
                entry = self.cache.get(key)
                if entry is not None and entry.fresh():
                    cached_reply = self._from_entry(query, entry)
                    # 正缓存进入最后四分之一：标记这一份缓存已预取，并起后台预取。
                    # 同一份缓存只触发一次；已有回源在进行时不重复触发。
                    if entry.needs_prefetch() and key not in self.inflight:
                        entry.prefetch_attempted = True
                        flight = Flight("prefetch")
                        self.inflight[key] = flight
                        start_prefetch = True
                else:
                    flight = self.inflight.get(key)
                    if flight is None:
                        flight = Flight("on-demand")
                        self.inflight[key] = flight
                        leader = True
                    else:
                        flight.waiters += 1
                        leader = False

            if cached_reply is not None:
                if start_prefetch:
                    # 命中请求立即返回旧答复；预取在后台自己回源、自己收尾
                    threading.Thread(
                        target=self._run_flight,
                        args=(None, key, qname, qtype, flight),
                        daemon=True,
                        name=f"prefetch-{key}",
                    ).start()
                return cached_reply

            if leader:
                return self._run_flight(query, key, qname, qtype, flight)

            # 等待者：只等这一次回源；超时仅自己回 SERVFAIL，flight 仍由 leader 收尾
            signaled = flight.event.wait(timeout=config.UPSTREAM_TIMEOUT + 2.0)
            with self.lock:
                entry = self.cache.get(key)
                if entry is not None and entry.fresh():
                    return self._from_entry(query, entry)
                if flight.error is not None:
                    # 预取/回源失败：等待者收到 SERVFAIL；旧条目（若仍有）不被沿用，
                    # 之后的未命中请求会重新发起回源。
                    return self._servfail(query)
                if signaled:
                    # leader 正常结束却没有可缓存内容：按失败处理
                    return self._servfail(query)
                # 否则本轮等待超时，进入下一轮（不会取消任何其他等待者）
        return self._servfail(query)

    def _run_flight(
        self,
        query: Optional[DNSRecord],
        key: str,
        qname: str,
        qtype: str,
        flight: Flight,
    ) -> Optional[DNSRecord]:
        """执行一次回源（按需或预取）。

        query 为 None 表示后台预取：没有客户端在等这次调用的直接返回，
        预取结果只写缓存；失败时保留旧缓存（不删、不延长）。
        """
        is_prefetch = flight.kind == "prefetch"
        try:
            with self.lock:
                self.attempts[key] = self.attempts.get(key, 0) + 1
                if is_prefetch:
                    self.prefetch_attempts[key] = self.prefetch_attempts.get(key, 0) + 1
            resp = self._fetch_upstream(qname, qtype)
            try:
                entry = self._store_reply(key, qname, resp)
            except ValueError as exc:
                flight.error = str(exc)
                with self.lock:
                    self.last_error[key] = str(exc)
                    if is_prefetch:
                        self.prefetch_failures[key] = self.prefetch_failures.get(key, 0) + 1
                return self._servfail(query) if query is not None else None
            return self._from_entry(query, entry) if query is not None else None
        except Exception as exc:  # 超时、ICMP 不可达、解析错误等
            # 关键：失败不写缓存 -> 不会沿用过期答复，也不会缓存成 NXDOMAIN；
            # 预取失败时旧缓存条目原样保留到原期限。
            flight.error = repr(exc)
            with self.lock:
                self.upstream_failures += 1
                self.last_error[key] = repr(exc)
                if is_prefetch:
                    self.prefetch_failures[key] = self.prefetch_failures.get(key, 0) + 1
            return self._servfail(query) if query is not None else None
        finally:
            with self.lock:
                if self.inflight.get(key) is flight:
                    self.inflight.pop(key, None)
            flight.event.set()

    def _forward_uncached(self, query: DNSRecord, qname: str, qtype: str) -> DNSRecord:
        try:
            with self.lock:
                self.attempts[qname] = self.attempts.get(qname, 0) + 1
            resp = self._fetch_upstream(qname, qtype)
            resp.header.id = query.header.id
            return resp
        except Exception as exc:
            with self.lock:
                self.upstream_failures += 1
                self.last_error[qname] = repr(exc)
            return self._servfail(query)

    def _note_query(self, qname: str) -> None:
        with self.lock:
            self.total_queries += 1

    # ---------- 状态快照 ----------

    def snapshot(self, name: Optional[str] = None) -> dict:
        with self.lock:
            names = set(self.cache) | set(self.attempts) | set(self.inflight) | set(self.last_error)
            if name:
                names = {name}
            per_name = {}
            for n in sorted(names):
                entry = self.cache.get(n)
                flight = self.inflight.get(n)
                per_name[n] = {
                    "name": n,
                    "upstream_attempts": self.attempts.get(n, 0),
                    "prefetch_attempts": self.prefetch_attempts.get(n, 0),
                    "prefetch_failures": self.prefetch_failures.get(n, 0),
                    "inflight": flight is not None,
                    "inflight_kind": flight.kind if flight else None,
                    "prefetching": flight is not None and flight.kind == "prefetch",
                    "inflight_waiters": flight.waiters if flight else 0,
                    "inflight_age_ms": int((time.time() - flight.started_at) * 1000) if flight else 0,
                    "cached": entry is not None and entry.fresh(),
                    "expired": entry is not None and not entry.fresh(),
                    "prefetch_attempted": entry.prefetch_attempted if entry else False,
                    "cache_kind": entry.kind if entry else None,
                    "rcode": entry.rcode if entry else None,
                    "rdata": entry.rdata if entry else [],
                    "ttl_original": entry.ttl if entry else None,
                    "ttl_remaining": entry.remaining() if entry else None,
                    "prefetch_due": entry.needs_prefetch() if entry else False,
                    "cached_at": entry.stored_wall if entry else None,
                    "last_error": self.last_error.get(n),
                }
            return {
                "total_queries": self.total_queries,
                "upstream_failures": self.upstream_failures,
                "upstream": f"{config.UPSTREAM_HOST}:{config.UPSTREAM_PORT}",
                "upstream_timeout_s": config.UPSTREAM_TIMEOUT,
                "prefetch_window": "last quarter of positive TTL",
                "names": per_name,
            }


RESOLVER = CachingResolver()


import socketserver


class ThreadingUDPServer(socketserver.ThreadingMixIn, socketserver.UDPServer):
    daemon_threads = True
    allow_reuse_address = True


class DNSHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data, sock = self.request
        try:
            query = DNSRecord.parse(data)
        except Exception:
            return
        try:
            reply = RESOLVER.resolve(query)
        except Exception:
            reply = query.reply()
            reply.header.set_rcode(RCODE.SERVFAIL)
        if reply is not None:
            sock.sendto(reply.pack(), self.client_address)


class StatsHandler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        if path in ("/healthz", "/"):
            self._send(200, {"status": "ok"})
            return
        if path == "/stats":
            name = None
            for part in query.split("&"):
                if part.startswith("name="):
                    name = part.split("=", 1)[1].lower().rstrip(".")
            self._send(200, RESOLVER.snapshot(name))
            return
        self._send(404, {"error": "not found"})

    def log_message(self, *args) -> None:  # 静默默认访问日志
        pass


def _serve_stats() -> None:
    httpd = ThreadingHTTPServer((config.LISTEN_ADDR, config.STATS_PORT), StatsHandler)
    httpd.serve_forever()


def main() -> None:
    threading.Thread(target=_serve_stats, daemon=True).start()
    server = ThreadingUDPServer((config.LISTEN_ADDR, config.LISTEN_PORT), DNSHandler)
    print(
        f"[resolver] dns udp://{config.LISTEN_ADDR}:{config.LISTEN_PORT}, "
        f"stats http://{config.LISTEN_ADDR}:{config.STATS_PORT}, "
        f"upstream {config.UPSTREAM_HOST}:{config.UPSTREAM_PORT} (timeout {config.UPSTREAM_TIMEOUT}s)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
