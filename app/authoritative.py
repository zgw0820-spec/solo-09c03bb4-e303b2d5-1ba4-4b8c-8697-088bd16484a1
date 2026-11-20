"""本地权威 DNS 服务器。

特性：
- 线程化 UDP，每个查询一个线程：慢域名（配置了回包延迟）不会阻塞另一域名。
- 数据来自 SQLite，每个查询开短连接，网页写入后下一次查询即可见。
- A 记录存在 -> AA 应答，TTL 取记录自身的 ttl。
- 记录不存在 -> NXDOMAIN，authority 段带 SOA，SOA TTL 与 minimum 均为负缓存期限。
- 可对某域名配置延迟（毫秒）或“静默丢包”（模拟权威不可达，用于验证
  解析器在回源失败时不得沿用过期答复 / 不得缓存 NXDOMAIN）。
"""
import socketserver
import time

from dnslib import A, QTYPE, RCODE, RR, SOA, DNSRecord

from . import config, db


def _soa_record(zone: str, neg_ttl: int) -> RR:
    return RR(
        rname=zone,
        rtype=QTYPE.SOA,
        rclass=1,
        ttl=neg_ttl,
        rdata=SOA(
            config.SOA_MNAME,
            config.SOA_RNAME,
            (
                config.SOA_SERIAL,
                config.SOA_REFRESH,
                config.SOA_RETRY,
                config.SOA_EXPIRE,
                neg_ttl,  # minimum = 负缓存期限
            ),
        ),
    )


def build_reply(query: DNSRecord) -> DNSRecord:
    qname = str(query.q.qname).lower().rstrip(".")
    qtype = QTYPE[query.q.qtype]

    conn = db.connect()
    try:
        # 先应用该域名的回包延迟；dropped=1 时由调用方直接不回包
        delay_ms = db.get_int_setting(conn, f"delay_{qname}", config.DEFAULT_DELAY_MS)
        dropped = db.get_bool_setting(conn, f"dropped_{qname}", False)
        neg_ttl = db.get_int_setting(conn, "neg_ttl", config.DEFAULT_NEG_TTL)
        row = db.get_record(conn, qname)
    finally:
        conn.close()

    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)
    if dropped:
        # 模拟权威故障：吞掉查询，不发送任何 UDP 应答
        return None  # type: ignore[return-value]

    reply = query.reply()

    if qtype == "A":
        if row is not None:
            reply.add_answer(
                RR(rname=qname, rtype=QTYPE.A, rclass=1, ttl=int(row["ttl"]), rdata=A(row["ip"]))
            )
        else:
            # NXDOMAIN + SOA：解析器按 SOA minimum 做负缓存
            reply.header.set_rcode(RCODE.NXDOMAIN)
            reply.add_auth(_soa_record(qname, neg_ttl))
    else:
        # 本实验只管理 A 记录；其他类型一律 NXDOMAIN（同样带负缓存期限）
        reply.header.set_rcode(RCODE.NXDOMAIN)
        reply.add_auth(_soa_record(qname, neg_ttl))

    reply.header.aa = 1
    return reply


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
        if not query.questions:
            return
        try:
            reply = build_reply(query)
        except Exception:
            # 数据库异常等：回 SERVFAIL，不让线程死掉
            reply = query.reply()
            reply.header.set_rcode(RCODE.SERVFAIL)
        if reply is not None:
            sock.sendto(reply.pack(), self.client_address)


def main() -> None:
    server = ThreadingUDPServer((config.LISTEN_ADDR, config.LISTEN_PORT), DNSHandler)
    print(
        f"[authoritative] listening on udp://{config.LISTEN_ADDR}:{config.LISTEN_PORT}, "
        f"db={config.DB_PATH}, domains={config.DOMAINS}",
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
