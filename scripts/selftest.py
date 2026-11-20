"""端到端自测：对运行中的 web + authoritative + resolver 发起真实 UDP 查询。

用法（容器内或本地等价进程均可）：
    python -m scripts.selftest

覆盖：
1. 冷查询回源 + 缓存命中（回源次数不增）
2. 正 TTL 到期后按权威现状重查（改址生效）
3. 删除记录 -> NXDOMAIN 按 SOA 负缓存期限失效
4. 负缓存期间恢复记录，TTL 内仍 NXDOMAIN；到期后拿到新 A
5. 回源失败（权威静默丢包）：不沿用过期答复、不缓存 NXDOMAIN、回 SERVFAIL
6. 同名并发冷查询只回源一次（singleflight），且各自 DNS ID 正确
7. 单个等待者超时不取消回源、不影响其他等待者
8. 慢域名不阻塞另一域名
9. 预取正常路径：命中触发一次、立即回旧答复、旧缓存先到期则等待者共用预取
10. 预取拿到 NXDOMAIN：按负缓存规则替换
11. 预取失败路径：旧缓存照原期限、等待者 SERVFAIL、之后未命中可重新回源
12. 解析器重启后缓存为空（由 scripts/selftest.sh 控制重启并调用 --restart-check）
"""
import json
import os
import random
import socket
import sys
import time
import urllib.request

WEB = os.environ.get("WEB_BASE", "http://127.0.0.1:8000")
RESOLVER = (os.environ.get("RESOLVER_UDP_HOST", "127.0.0.1"),
            int(os.environ.get("RESOLVER_UDP_PORT", "5354")))

# dnslib 只在被测服务的镜像里是硬依赖；自测脚本本身也用它编解码报文
from dnslib import QTYPE, RCODE, DNSRecord  # noqa: E402

PASS, FAIL = 0, 0
FAILURE_DETAILS = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32m[PASS]\033[0m {label}")
    else:
        FAIL += 1
        FAILURE_DETAILS.append(f"{label} :: {detail}")
        print(f"  \033[31m[FAIL]\033[0m {label} {detail}")


def http(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        WEB + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def stats(name: str) -> dict:
    with urllib.request.urlopen(
        f"http://{os.environ.get('RESOLVER_STATS_HOST', '127.0.0.1')}:"
        f"{os.environ.get('RESOLVER_STATS_PORT', '8081')}/stats?name={name}",
        timeout=5,
    ) as resp:
        return json.loads(resp.read().decode())["names"].get(name, {})


def dns_query(name: str, timeout: float):
    """真实 UDP 查询，返回 (reply_or_None, query_id)；超时返回 (None, id)。"""
    q = DNSRecord.question(name, qtype="A")
    q.header.id = random.randint(1, 65535)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(q.pack(), RESOLVER)
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            return None, int(q.header.id)
    return DNSRecord.parse(data), int(q.header.id)


def wait_until(deadline_s, desc, fn):
    end = time.time() + deadline_s
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f"等待超时：{desc}（最后状态={last}）")


def set_record(name, ip, ttl):
    return http("POST", f"/api/domains/{name}/record", {"ip": ip, "ttl": ttl})


def del_record(name):
    return http("DELETE", f"/api/domains/{name}/record")


def set_neg_ttl(ttl):
    return http("PUT", "/api/settings/neg-ttl", {"neg_ttl": ttl})


def set_delay(name, ms):
    return http("PUT", f"/api/domains/{name}/delay", {"delay_ms": ms})


def set_drop(name, dropped):
    return http("PUT", f"/api/domains/{name}/drop", {"dropped": dropped})


def answers_of(reply):
    return [str(rr.rdata) for rr in reply.rr if rr.rtype == QTYPE.A]


def restart_check():
    """阶段 12：重启后的解析器必须没有任何旧缓存。"""
    print("\n== 12. 解析器重启后不得沿用旧缓存 ==")
    snap = stats("")  # name='' 时返回全量
    with urllib.request.urlopen(
        f"http://{os.environ.get('RESOLVER_STATS_HOST', '127.0.0.1')}:"
        f"{os.environ.get('RESOLVER_STATS_PORT', '8081')}/stats", timeout=5
    ) as resp:
        full = json.loads(resp.read().decode())
    check("重启后缓存表为空", all(not v.get("cached") for v in full["names"].values()),
          detail=str(full["names"]))
    check("重启后回源计数归零", all(v.get("upstream_attempts") == 0 for v in full["names"].values()),
          detail=str(full["names"]))
    summarize()


def summarize():
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    if FAIL:
        print("失败项：")
        for d in FAILURE_DETAILS:
            print(" -", d)
        sys.exit(1)
    sys.exit(0)


def main():
    state = http("GET", "/api/state")
    d1, d2 = state["domains"][0]["name"], state["domains"][1]["name"]
    print(f"被测域名：{d1} / {d2}；解析器：{RESOLVER}；网页：{WEB}")

    # 统一短 TTL，缩短测试时长
    POS_TTL, NEG_TTL = 2, 2
    set_neg_ttl(NEG_TTL)
    set_delay(d1, 0); set_delay(d2, 0)
    set_drop(d1, False); set_drop(d2, False)
    set_record(d1, "10.0.0.10", POS_TTL)
    set_record(d2, "10.0.0.20", POS_TTL)

    print("\n== 1. 冷查询回源 + 缓存命中 ==")
    reply, qid = dns_query(d1, 3.0)
    check("冷查询拿到 NOERROR", reply is not None and reply.header.rcode == RCODE.NOERROR,
          detail=str(reply.header if reply else None))
    check("答复 DNS ID 与查询一致", reply is not None and int(reply.header.id) == qid)
    check("应答内容正确", reply is not None and answers_of(reply) == ["10.0.0.10"])
    s = wait_until(3, "stats 可见", lambda: stats(d1))
    check("回源次数=1", s.get("upstream_attempts") == 1, detail=str(s))
    attempts_after_first = s["upstream_attempts"]
    reply2, qid2 = dns_query(d1, 3.0)
    check("第二次查询命中缓存且 ID 仍正确",
          reply2 is not None and int(reply2.header.id) == qid2 and answers_of(reply2) == ["10.0.0.10"])
    s = stats(d1)
    check("缓存命中不增加回源次数", s.get("upstream_attempts") == attempts_after_first, detail=str(s))
    check("剩余 TTL 递减且不超过原始 TTL",
          s.get("ttl_remaining") is not None and 0 <= s["ttl_remaining"] <= POS_TTL, detail=str(s))

    print("\n== 2. 正 TTL 到期后按权威现状重查（改址） ==")
    set_record(d1, "10.0.0.11", POS_TTL)
    reply3, _ = dns_query(d1, 3.0)
    check("TTL 未到期仍返回旧地址（缓存有效）",
          reply3 is not None and answers_of(reply3) == ["10.0.0.10"], detail=str(answers_of(reply3)))
    wait_until(POS_TTL + 3, "正缓存到期", lambda: not stats(d1).get("cached"))
    reply4, _ = dns_query(d1, 3.0)
    check("到期重查返回新地址", reply4 is not None and answers_of(reply4) == ["10.0.0.11"],
          detail=str(answers_of(reply4)))
    check("到期重查增加了回源次数", stats(d1).get("upstream_attempts") == attempts_after_first + 1,
          detail=str(stats(d1)))

    print("\n== 3. 删除记录：NXDOMAIN 按 SOA 负缓存期限失效 ==")
    del_record(d1)
    wait_until(POS_TTL + 3, "旧正缓存到期", lambda: not stats(d1).get("cached"))
    reply5, _ = dns_query(d1, 3.0)
    check("回源得到 NXDOMAIN", reply5 is not None and reply5.header.rcode == RCODE.NXDOMAIN)
    soa = [rr for rr in reply5.auth if rr.rtype == QTYPE.SOA]
    check("NXDOMAIN 带 SOA", bool(soa))
    check("SOA minimum 等于配置的负 TTL", bool(soa) and int(soa[0].rdata.times[4]) == NEG_TTL,
          detail=str(soa[0].rdata if soa else None))
    s = stats(d1)
    check("解析器记录为负缓存", s.get("cache_kind") == "negative" and s.get("cached") is True, detail=str(s))
    neg_attempts = s["upstream_attempts"]
    reply6, _ = dns_query(d1, 3.0)
    check("负缓存期内重复查询仍是 NXDOMAIN 且不回源",
          reply6 is not None and reply6.header.rcode == RCODE.NXDOMAIN
          and stats(d1).get("upstream_attempts") == neg_attempts)

    print("\n== 4. 负缓存期内恢复记录，到期后才生效 ==")
    set_record(d1, "10.0.0.12", POS_TTL)
    reply7, _ = dns_query(d1, 3.0)
    check("负 TTL 未到期：恢复记录不立即生效",
          reply7 is not None and reply7.header.rcode == RCODE.NXDOMAIN, detail=str(reply7.header))
    wait_until(NEG_TTL + 3, "负缓存到期", lambda: not stats(d1).get("cached"))
    reply8, _ = dns_query(d1, 3.0)
    check("负缓存到期重查得到恢复后的 A 记录",
          reply8 is not None and reply8.header.rcode == RCODE.NOERROR
          and answers_of(reply8) == ["10.0.0.12"], detail=str(answers_of(reply8) if reply8 else None))

    print("\n== 5. 回源失败：不沿用过期答复 / 不缓存 NXDOMAIN / 回 SERVFAIL ==")
    wait_until(POS_TTL + 3, "正缓存再次到期", lambda: not stats(d1).get("cached"))
    set_drop(d1, True)  # 权威对该域名静默丢包
    reply9, _ = dns_query(d1, 6.0)
    check("回源失败回 SERVFAIL", reply9 is not None and reply9.header.rcode == RCODE.SERVFAIL,
          detail=str(reply9.header if reply9 else None))
    check("失败后没有把 NXDOMAIN 缓存起来",
          stats(d1).get("cache_kind") != "negative" and not stats(d1).get("cached"), detail=str(stats(d1)))
    check("失败被计入 upstream_failures", stats(d1).get("last_error"))
    failed_attempts = stats(d1).get("upstream_attempts")
    reply10, _ = dns_query(d1, 6.0)
    check("再次查询仍重新回源并 SERVFAIL（不沿用任何过期答复）",
          reply10 is not None and reply10.header.rcode == RCODE.SERVFAIL
          and stats(d1).get("upstream_attempts") == failed_attempts + 1,
          detail=str(stats(d1)))
    set_drop(d1, False)
    reply11, _ = dns_query(d1, 6.0)
    check("权威恢复后立即重新拿到真实答复",
          reply11 is not None and reply11.header.rcode == RCODE.NOERROR
          and answers_of(reply11) == ["10.0.0.12"], detail=str(answers_of(reply11) if reply11 else None))

    print("\n== 6. 同名并发冷查询只回源一次，各自 DNS ID 正确 ==")
    set_delay(d2, 1500)
    wait_until(POS_TTL + 3, "d2 缓存到期", lambda: not stats(d2).get("cached"))
    base = stats(d2).get("upstream_attempts", 0)
    results = []
    threads = []

    def worker():
        r, i = dns_query(d2, 5.0)
        results.append((r, i))

    import threading
    first = threading.Thread(target=worker)
    first.start()
    # 显式等待 leader 进入“回源中”，避免其余线程因调度抖动错过同一次 flight
    wait_until(3, "leader 进入 inflight", lambda: stats(d2).get("inflight"))
    for _ in range(7):
        t = threading.Thread(target=worker)
        threads.append(t)
        t.start()
    first.join()
    for t in threads:
        t.join()
    check("8 个并发查询全部拿到应答", len(results) == 8, detail=f"got {len(results)}")
    check("全部 NOERROR 且应答各自匹配自己的 DNS ID",
          all(r is not None and r.header.rcode == RCODE.NOERROR and int(r.header.id) == i
              for r, i in results), detail=str([(int(r.header.id) if r else None, i) for r, i in results]))
    check("应答内容一致", all(answers_of(r) == ["10.0.0.20"] for r, _ in results))
    check("同名冷查询只回源一次", stats(d2).get("upstream_attempts") == base + 1,
          detail=str(stats(d2)))

    print("\n== 7. 单个等待者超时不取消回源、不影响其他等待者 ==")
    set_delay(d2, 1200)
    wait_until(POS_TTL + 3, "d2 缓存到期", lambda: not stats(d2).get("cached"))
    base = stats(d2).get("upstream_attempts", 0)
    slow, fast = [], []

    def slow_client():
        r, i = dns_query(d2, 5.0)
        slow.append((r, i))

    def fast_client():
        r, i = dns_query(d2, 0.35)  # 权威延迟 1.2s，此客户端必然先超时
        fast.append((r, i))

    t_slow = threading.Thread(target=slow_client)
    t_fast = threading.Thread(target=fast_client)
    t_slow.start(); time.sleep(0.05); t_fast.start()
    t_fast.join(); t_slow.join()
    check("短超时客户端收到超时（None）", fast and fast[0][0] is None, detail=str(fast))
    check("长超时客户端仍拿到回源应答且 ID 正确",
          slow and slow[0][0] is not None and int(slow[0][0].header.id) == slow[0][1]
          and answers_of(slow[0][0]) == ["10.0.0.20"], detail=str(slow))
    check("回源只发生一次，未被超时者取消", stats(d2).get("upstream_attempts") == base + 1,
          detail=str(stats(d2)))
    set_delay(d2, 0)

    print("\n== 8. 慢域名不阻塞另一域名 ==")
    set_delay(d2, 2500)
    wait_until(POS_TTL + 3, "d2 缓存到期", lambda: not stats(d2).get("cached"))
    t0 = time.time()
    r_d1, _ = dns_query(d1, 3.0)  # d1 无延迟；若被 d2 阻塞会等到 2.5s 以上
    elapsed = time.time() - t0
    check("d1 在 d2 慢回源期间快速得到答复",
          r_d1 is not None and r_d1.header.rcode == RCODE.NOERROR and elapsed < 1.0,
          detail=f"elapsed={elapsed:.2f}s")
    set_delay(d2, 0)

    print("\n== 9. 预取正常路径：命中触发一次，旧缓存先到期则等待者共用预取 ==")
    # 预取窗口 = 原 TTL 的最后 1/4。令窗口(≈TTL/4) < 权威延迟 < 回源超时，
    # 就能确定性地得到“旧缓存先到期、预取仍在回源”的真实窗口。
    with urllib.request.urlopen(
        f"http://{os.environ.get('RESOLVER_STATS_HOST', '127.0.0.1')}:"
        f"{os.environ.get('RESOLVER_STATS_PORT', '8081')}/stats", timeout=5
    ) as resp:
        upstream_timeout = float(json.loads(resp.read().decode())["upstream_timeout_s"])
    delay_ms = int(upstream_timeout * 1000 * 0.6)
    # 预取窗口 = TTL/4；令窗口明显小于权威延迟（TTL ≈ 2*延迟），
    # 就能确定性地得到“旧缓存先于预取回包到期”的窗口。
    PREFETCH_TTL = max(4, int(2 * delay_ms / 1000))
    print(f"   （upstream timeout={upstream_timeout}s -> 延迟={delay_ms}ms, TTL={PREFETCH_TTL}s）")
    set_delay(d2, delay_ms)
    set_record(d2, "10.0.0.20", PREFETCH_TTL)
    wait_until(POS_TTL + 3, "d2 旧缓存到期", lambda: not stats(d2).get("cached"))
    cold, cold_id = dns_query(d2, 6.0)
    check("预取场景冷查询拿到 A", cold is not None and answers_of(cold) == ["10.0.0.20"],
          detail=str(answers_of(cold) if cold else None))
    base_attempts = stats(d2).get("upstream_attempts")
    base_prefetch = stats(d2).get("prefetch_attempts", 0)

    wait_until(PREFETCH_TTL + 3, "进入预取窗口",
               lambda: stats(d2) if stats(d2).get("cached") and stats(d2).get("prefetch_due") else None)
    t_hit = time.time()
    hit, hit_id = dns_query(d2, 6.0)  # 命中请求：应立即拿到旧答复，同时触发后台预取
    hit_elapsed = time.time() - t_hit
    check("预取触发查询立即收到旧答复（未被 2s 权威延迟拖住）",
          hit is not None and hit_elapsed < 0.5, detail=f"elapsed={hit_elapsed:.2f}s")
    check("预取触发查询的 DNS ID 正确", hit is not None and int(hit.header.id) == hit_id)
    check("预取触发查询仍是原 A 答复且 TTL 未重置",
          hit is not None and answers_of(hit) == ["10.0.0.20"]
          and all(int(rr.ttl) <= 1 for rr in hit.rr if rr.rtype == QTYPE.A),
          detail=str([(str(rr.rdata), int(rr.ttl)) for rr in hit.rr]))
    s = wait_until(3, "预取回源可见", lambda: stats(d2) if stats(d2).get("prefetching") else None)
    check("同一份缓存只触发一次预取", s.get("prefetch_attempts") == base_prefetch + 1
          and s.get("upstream_attempts") == base_attempts + 1, detail=str(s))
    check("预取期间命中请求不计入等待者", s.get("inflight_waiters") == 0, detail=str(s))

    hit2, hit2_id = dns_query(d2, 6.0)  # 预取窗口内再次命中：不得再触发一次预取
    attempts_now = stats(d2).get("upstream_attempts")
    check("同份缓存第二次命中不再触发预取", attempts_now == base_attempts + 1,
          detail=str(stats(d2)))
    check("第二次命中仍立即回旧答复且 ID 正确",
          hit2 is not None and int(hit2.header.id) == hit2_id and answers_of(hit2) == ["10.0.0.20"],
          detail=str(answers_of(hit2) if hit2 else None))

    # 等旧条目先于预取到期：此时缓存为空、预取仍在回源
    wait_until(3, "旧缓存先到期且预取未完成",
               lambda: stats(d2) if (not stats(d2).get("cached") and stats(d2).get("prefetching")) else None)
    t_join = time.time()
    joined, joined_id = dns_query(d2, 6.0)  # 新请求加入尚未完成的预取
    joined_elapsed = time.time() - t_join
    check("旧缓存到期后的新请求共用预取、拿到 NOERROR",
          joined is not None and joined.header.rcode == RCODE.NOERROR
          and answers_of(joined) == ["10.0.0.20"], detail=str(joined.header if joined else None))
    check("共用预取的答复带自己的 DNS ID", joined is not None and int(joined.header.id) == joined_id)
    check("共用预取不会额外回源", stats(d2).get("upstream_attempts") == base_attempts + 1,
          detail=str(stats(d2)))
    check("共用预取需等待已有预取回包（没有另起一次完整回源）",
          joined_elapsed < delay_ms / 1000.0,
          detail=f"elapsed={joined_elapsed:.2f}s delay={delay_ms}ms")
    s = wait_until(3, "预取写入新缓存",
                   lambda: stats(d2) if stats(d2).get("cached") and stats(d2).get("ttl_remaining", 0) >= PREFETCH_TTL - 1 else None)
    check("预取成功后缓存被新条目替换（TTL 回到接近原值）",
          s.get("cache_kind") == "positive" and 1 < s.get("ttl_remaining", 0) <= PREFETCH_TTL,
          detail=str(s))
    check("新条目可以再次触发预取（预取标记随缓存替换重置）",
          s.get("prefetch_attempted") is False, detail=str(s))
    after, _ = dns_query(d2, 6.0)
    check("预取后的命中返回新缓存且不再回源",
          after is not None and answers_of(after) == ["10.0.0.20"]
          and stats(d2).get("upstream_attempts") == base_attempts + 1,
          detail=str(stats(d2)))

    print("\n== 10. 预取拿到 NXDOMAIN 也按正常回源规则更新缓存 ==")
    set_delay(d2, 0)
    set_record(d2, "10.0.0.20", PREFETCH_TTL)
    wait_until(PREFETCH_TTL + 3, "d2 旧缓存到期", lambda: not stats(d2).get("cached"))
    cold, _ = dns_query(d2, 6.0)
    check("NXDOMAIN 预取场景冷查询拿到 A", cold is not None and answers_of(cold) == ["10.0.0.20"],
          detail=str(answers_of(cold) if cold else None))
    base_attempts = stats(d2).get("upstream_attempts")
    wait_until(PREFETCH_TTL + 3, "进入预取窗口",
               lambda: stats(d2) if stats(d2).get("cached") and stats(d2).get("prefetch_due") else None)
    del_record(d2)  # 预取时权威将回 NXDOMAIN
    hit, hit_id = dns_query(d2, 6.0)
    check("触发预取的命中请求仍立即拿到旧 A",
          hit is not None and answers_of(hit) == ["10.0.0.20"] and int(hit.header.id) == hit_id,
          detail=str(answers_of(hit) if hit else None))
    neg = wait_until(5, "预取写入负缓存",
                     lambda: stats(d2) if stats(d2).get("cache_kind") == "negative" else None)
    check("预取得到 NXDOMAIN 后按负缓存规则替换缓存",
          neg.get("rcode") == int(RCODE.NXDOMAIN) and neg.get("cached") is True, detail=str(neg))
    check("预取的 NXDOMAIN 按 SOA minimum 计 TTL",
          0 < neg.get("ttl_remaining", 0) <= NEG_TTL, detail=str(neg))
    nx, nx_id = dns_query(d2, 3.0)
    check("替换后的负缓存命中返回 NXDOMAIN 且 ID 正确",
          nx is not None and nx.header.rcode == RCODE.NXDOMAIN and int(nx.header.id) == nx_id,
          detail=str(nx.header if nx else None))
    check("负缓存命中不增加回源次数", stats(d2).get("upstream_attempts") == base_attempts + 1,
          detail=str(stats(d2)))
    set_record(d2, "10.0.0.20", PREFETCH_TTL)

    print("\n== 11. 预取失败：旧缓存照原期限、等待者 SERVFAIL、之后可重新回源 ==")
    # 预取失败靠权威静默丢包（等待 UPSTREAM_TIMEOUT）模拟；为了让失败发生在
    # 旧缓存仍有效、且预取窗口（TTL/4）内，TTL 取 4*(超时+2) 秒。
    upstream_timeout = None
    with urllib.request.urlopen(
        f"http://{os.environ.get('RESOLVER_STATS_HOST', '127.0.0.1')}:"
        f"{os.environ.get('RESOLVER_STATS_PORT', '8081')}/stats", timeout=5
    ) as resp:
        upstream_timeout = float(json.loads(resp.read().decode())["upstream_timeout_s"])
    PF_FAIL_TTL = int(4 * (upstream_timeout + 2))
    print(f"   （upstream timeout={upstream_timeout}s -> 失败场景 TTL={PF_FAIL_TTL}s）")
    set_delay(d2, 0)
    set_delay(d1, 0)
    set_drop(d1, False)
    set_record(d1, "10.0.0.10", PF_FAIL_TTL)
    wait_until(PF_FAIL_TTL + 5, "d1 旧缓存到期", lambda: not stats(d1).get("cached"))
    cold, _ = dns_query(d1, 6.0)
    check("失败场景冷查询拿到 A", cold is not None and answers_of(cold) == ["10.0.0.10"],
          detail=str(answers_of(cold) if cold else None))
    base_attempts = stats(d1).get("upstream_attempts")
    base_prefetch = stats(d1).get("prefetch_attempts", 0)
    base_failures = stats(d1).get("prefetch_failures", 0)
    set_drop(d1, True)  # 预取将因权威静默丢包而超时失败

    wait_until(PF_FAIL_TTL + 3, "进入预取窗口",
               lambda: stats(d1) if stats(d1).get("cached") and stats(d1).get("prefetch_due") else None)
    t_hit = time.time()
    hit, hit_id = dns_query(d1, 6.0)
    hit_elapsed = time.time() - t_hit
    check("预取失败场景：触发查询仍立即拿到旧答复",
          hit is not None and hit_elapsed < 0.5 and answers_of(hit) == ["10.0.0.10"],
          detail=f"elapsed={hit_elapsed:.2f}s answers={answers_of(hit) if hit else None}")
    check("预取失败场景：触发查询 ID 正确", hit is not None and int(hit.header.id) == hit_id)
    check("预取回源计数增加", wait_until(3, "预取计数",
          lambda: stats(d1) if stats(d1).get("prefetch_attempts", 0) == base_prefetch + 1 else None).get("prefetch_attempts") == base_prefetch + 1)
    failed = wait_until(upstream_timeout + 6, "预取失败",
                        lambda: stats(d1) if stats(d1).get("prefetch_failures", 0) > base_failures else None)
    check("预取失败被单独计数", failed.get("prefetch_failures") == base_failures + 1, detail=str(failed))
    check("预取失败后旧缓存仍有效（照原期限使用）",
          failed.get("cached") is True and failed.get("ttl_remaining", 999) <= PF_FAIL_TTL,
          detail=str(failed))
    check("预取失败后缓存未被替换成 NXDOMAIN", failed.get("cache_kind") == "positive", detail=str(failed))
    check("失败回源计入 upstream_failures 且记录错误", bool(failed.get("last_error")), detail=str(failed))

    # 旧条目仍有剩余生命时，命中继续拿到旧答复，且不再触发新的预取
    still, still_id = dns_query(d1, 3.0)
    check("预取失败后旧缓存期内仍返回旧答复",
          still is not None and answers_of(still) == ["10.0.0.10"] and int(still.header.id) == still_id,
          detail=str(answers_of(still) if still else None))
    check("同一份缓存失败后不再重复预取",
          stats(d1).get("upstream_attempts") == base_attempts + 1
          and stats(d1).get("prefetch_attempts") == base_prefetch + 1, detail=str(stats(d1)))

    # 旧缓存到期（预取已失败）：新请求重新发起按需回源，权威仍丢包 -> SERVFAIL
    wait_until(PF_FAIL_TTL + 5, "d1 旧缓存到期", lambda: not stats(d1).get("cached"))
    failed_reply, failed_id = dns_query(d1, upstream_timeout + 3)
    check("预取失败且旧缓存到期后，未命中请求重新回源并 SERVFAIL",
          failed_reply is not None and failed_reply.header.rcode == RCODE.SERVFAIL
          and int(failed_reply.header.id) == failed_id, detail=str(failed_reply.header if failed_reply else None))
    check("重新回源计入回源次数", stats(d1).get("upstream_attempts") == base_attempts + 2,
          detail=str(stats(d1)))
    set_drop(d1, False)
    recovered, recovered_id = dns_query(d1, 6.0)
    check("权威恢复后未命中请求重新回源拿到 A",
          recovered is not None and answers_of(recovered) == ["10.0.0.10"]
          and int(recovered.header.id) == recovered_id, detail=str(answers_of(recovered) if recovered else None))
    check("恢复后的回源再次计入次数", stats(d1).get("upstream_attempts") == base_attempts + 3,
          detail=str(stats(d1)))
    set_delay(d1, 0)
    set_drop(d1, False)

    print("\n== 12 预备：制造缓存供重启验证 ==")
    set_record(d1, "10.0.0.10", POS_TTL)
    dns_query(d1, 3.0)
    check("重启前缓存确实存在", bool(stats(d1).get("cached")), detail=str(stats(d1)))
    print("（请用 scripts/selftest.sh 重启解析器并执行 --restart-check）")
    summarize()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--restart-check":
        restart_check()
    else:
        main()
