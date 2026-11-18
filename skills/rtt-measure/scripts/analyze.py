#!/usr/bin/env python3
"""패킷 캡처(pcap/pcapng)에서 대상 HTTPS 연결 하나를 골라 RTT 계산에 필요한 값을 뽑는다.

출력(JSON):
  rtt_ms, mss(협상값), payload_per_segment(실제 한 세그먼트에 실리는 데이터), tls_server_segments,
  handshake_rtt(첫 요청까지 걸린 RTT 수), stages[각 요청 단계의 라운드별 세그먼트 수, 대기, 대역폭 제한 여부]

사용: analyze.py run-1.pcap --host example.com
      analyze.py capture.pcap --host example.com --window rtt-runs/run-1.json   # 수동 캡처 파일에서 그 회차 구간만
"""
import argparse, json, socket, struct, sys
import dpkt

REQUEST_MIN_PAYLOAD = 60      # 이보다 작은 클라이언트 데이터는 HTTP/2 제어 프레임(SETTINGS ACK, WINDOW_UPDATE)으로 본다
ROUND_GAP_RATIO = 0.5         # 요청 묶음 구분, timestamp 없는 연결의 라운드 구분에 쓰는 공백 기준
STALL_GAP_RATIO = 3.0         # 보고용 이름 붙이기만: 서버 유휴가 이보다 길면 'CDN MISS 등 긴 대기', 짧으면 '서버 대기'
STALL_GAP_MIN_MS = 20.0
MIN_INIT_CWND = 10            # 초기 cwnd(RFC 6928). cwnd 하한 = 이 값 + 지금까지 ACK된 세그먼트


def ip_packets(path):
    with open(path, 'rb') as f:
        magic = f.read(4); f.seek(0)
        reader = dpkt.pcapng.Reader(f) if magic == b'\x0a\x0d\x0d\x0a' else dpkt.pcap.Reader(f)
        dl = reader.datalink()
        for ts, buf in reader:
            try:
                if dl == dpkt.pcap.DLT_EN10MB:
                    ip = dpkt.ethernet.Ethernet(buf).data
                elif dl == dpkt.pcap.DLT_LINUX_SLL:
                    ip = dpkt.sll.SLL(buf).data
                elif dl == 276:  # LINUX_SLL2
                    ip = dpkt.sll2.SLL2(buf).data
                elif dl in (dpkt.pcap.DLT_NULL, dpkt.pcap.DLT_LOOP):
                    ip = dpkt.loopback.Loopback(buf).data
                elif dl in (dpkt.pcap.DLT_RAW, 101, 12):
                    ip = dpkt.ip.IP(buf) if buf[0] >> 4 == 4 else dpkt.ip6.IP6(buf)
                else:
                    continue
            except Exception:
                continue
            if isinstance(ip, (dpkt.ip.IP, dpkt.ip6.IP6)) and isinstance(ip.data, dpkt.tcp.TCP):
                yield ts, ip


def addr(ip, which):
    raw = ip.src if which == 'src' else ip.dst
    return socket.inet_ntop(socket.AF_INET6 if isinstance(ip, dpkt.ip6.IP6) else socket.AF_INET, raw)


def tcp_options(tcp):
    opts = {}
    for kind, data in dpkt.tcp.parse_opts(tcp.opts):
        if kind == dpkt.tcp.TCP_OPT_MSS and len(data) == 2:
            opts['mss'] = struct.unpack('>H', data)[0]
        elif kind == dpkt.tcp.TCP_OPT_TIMESTAMP:
            opts['timestamp'] = True
            if len(data) == 8:
                opts['tsval'], opts['tsecr'] = struct.unpack('>II', data)
    return opts


def sni_from_client_hello(data):
    """TLS ClientHello에서 SNI 추출. ClientHello가 여러 세그먼트로 나뉘어도 이어 붙인 바이트를 받는다."""
    try:
        if len(data) < 5 or data[0] != 0x16:
            return None
        rec_len = struct.unpack('>H', data[3:5])[0]
        hs = data[5:5 + rec_len]
        if hs[0] != 0x01:
            return None
        p = 4 + 2 + 32                      # handshake header, version, random
        p += 1 + hs[p]                      # session id
        p += 2 + struct.unpack('>H', hs[p:p + 2])[0]   # cipher suites
        p += 1 + hs[p]                      # compression
        end = p + 2 + struct.unpack('>H', hs[p:p + 2])[0]
        p += 2
        while p + 4 <= end:
            etype, elen = struct.unpack('>HH', hs[p:p + 4]); p += 4
            if etype == 0:                  # server_name
                name_len = struct.unpack('>H', hs[p + 3:p + 5])[0]
                return hs[p + 5:p + 5 + name_len].decode('ascii', 'replace')
            p += elen
    except Exception:
        return None
    return None


MIN_RTT_SAMPLES = 3            # 전송 중 RTT 표본이 이보다 적으면 handshake RTT를 쓴다


def rtt_samples(pkts, after):
    """(서버 패킷 도착 시각, RTT 표본) 목록. TSecr 값마다 그 값을 처음 실어 온 서버 패킷만 쓴다."""
    sent = {}
    for ts, d, t in pkts:
        if d == 'c2s':
            o = tcp_options(t)
            if 'tsval' in o and o['tsval'] not in sent:
                sent[o['tsval']] = ts
    seen, out = set(), []
    for ts, d, t in pkts:
        if d != 's2c' or ts <= after or t.plen == 0:
            continue
        echo = tcp_options(t).get('tsecr')
        if echo is None or echo in seen or echo not in sent:
            continue
        seen.add(echo)
        if ts - sent[echo] > 0:
            out.append((ts, ts - sent[echo]))
    return out


def median(values):
    v = sorted(values)
    return v[len(v) // 2] if v else None


def seq_after(a, b):
    """32비트 순번 비교: a가 b보다 뒤인가"""
    return 0 < ((a - b) & 0xFFFFFFFF) < 0x80000000


def ack_rounds(segs, cacks):
    """ACK 인과관계로 라운드를 나눈다. segs: [(ts, n, seq, tsecr)] 시간순, cacks: [(ts, ack, tsval)] 시간순.

    서버 세그먼트의 TSecr는 서버가 그 세그먼트를 보낼 때 받아 둔 가장 최근 클라이언트 패킷의 TSval이다.
    라운드 k의 첫 세그먼트를 확인한 클라이언트 ACK의 TSval을 서버가 되돌려 주기 시작하면, 서버가 그 ACK를 받은 뒤
    보낸 세그먼트 = 라운드 k+1이다. 시간으로 자르지 않으므로 페이싱이나 RTT 흔들림에 영향을 받지 않는다.
    (BBR이 라운드를 세는 방식과 같다: 라운드 시작 패킷이 ACK되면 다음 라운드)"""
    rounds, cur, thr = [], [], None
    for s in segs:
        ts, n, seq, echo = s[:4]
        if cur and thr is not None and echo is not None and (echo == thr or seq_after(echo, thr)):
            rounds.append(cur); cur, thr = [], None
        if not cur:
            v = next((tv for cts, ack, tv in cacks if cts >= ts and tv is not None and seq_after(ack, seq)), None)
            if v is not None:
                # timestamp 해상도(보통 1ms) 때문에 같은 값이면 다음 라운드를 구분할 수 없으므로 한 단계 위로
                thr = v if echo is None or seq_after(v, echo) else (echo + 1) & 0xFFFFFFFF
        cur.append(s)
    if cur:
        rounds.append(cur)
    return rounds


def init_cwnd_lower_bound(stage_raws, delivered):
    """초기 cwnd(IW)의 하한. 어떤 라운드도 그 시점 cwnd를 넘을 수 없고, cwnd ≤ IW + 그때까지 ACK된 세그먼트이므로
    IW ≥ 라운드 크기 − 그 라운드 전까지 받은 세그먼트. 연결 안 모든 라운드에서 최댓값을 쓰고, RFC 6928의 10보다 작게는 두지 않는다.
    (서버가 RFC보다 큰 초기 cwnd를 쓰면 그만큼 올라간다. 예: CloudFront 첫 라운드 30 이상)"""
    best = MIN_INIT_CWND
    for raw in stage_raws:
        for r in raw:
            best = max(best, len(r) - delivered)
            delivered += len(r)
    return best


def merge_cwnd_rounds(raw, delivered, iw=MIN_INIT_CWND):
    """cwnd 때문에 나뉜 라운드만 남긴다. raw: 라운드 목록(각 라운드는 세그먼트 목록), delivered: 이 단계 전까지 받은 세그먼트 수.
    슬로 스타트에서 cwnd ≥ 초기 cwnd + 지금까지 ACK된 세그먼트. 연속한 두 라운드를 합쳐도 앞 라운드 시작 시점의
    이 하한 안에 들어가면 cwnd가 나눈 게 아니므로(페이싱 꼬리, 서버가 데이터를 늦게 준 경우) 합친다."""
    rounds, starts = [], []
    for r in raw:
        if rounds and len(rounds[-1]) + len(r) <= iw + starts[-1]:
            rounds[-1] = rounds[-1] + r
            continue
        if rounds:
            delivered += len(rounds[-1])
        rounds.append(list(r)); starts.append(delivered)
    if rounds:
        delivered += len(rounds[-1])
    return rounds, delivered


TS_TICK = 0.001   # TCP timestamp 해상도(보통 1ms). 이만큼은 측정 오차로 본다


def server_idle(segs, sdata, cacks, rtt, request_ts):
    """서버 유휴 시간. (첫 응답 전 유휴, {세그먼트 i와 i+1 사이: 유휴}).
    첫 응답이 '요청 + 최소 RTT'보다 늦게 온 만큼(CDN MISS 등), 그리고 그때까지 보낸 데이터를 모두 ACK한 시각 + 최소 RTT보다
    다음 세그먼트가 늦게 온 만큼 = 서버에 보낼 데이터가 없던 시간."""
    first = segs[0][0] - (request_ts + rtt) if segs else 0
    gaps, ci = {}, 0
    for i, ((t0, n0), (t1, _)) in enumerate(zip(segs, segs[1:])):
        end_seq = (sdata[t0][0] + n0) & 0xFFFFFFFF
        while ci < len(cacks) and cacks[ci][0] < t0:
            ci += 1
        t_ack = next((cts for cts, ack, _ in cacks[ci:] if ack == end_seq or seq_after(ack, end_seq)), None)
        if t_ack is not None and t1 - (t_ack + rtt) > TS_TICK:
            gaps[i] = t1 - (t_ack + rtt)
    return (first if first > TS_TICK else 0), gaps


def gap_idle(segs, start, rtt):
    """timestamp가 없을 때의 대체: 1 RTT보다 긴 공백에서 RTT를 뺀 만큼"""
    first = segs[0][0] - start - rtt if segs else 0
    gaps = {i: (b[0] - a[0] - rtt) for i, (a, b) in enumerate(zip(segs, segs[1:])) if b[0] - a[0] > rtt}
    return (first if first > 0 else 0), gaps


# BBR의 '파이프가 찼다' 판정 상수 (Linux net/ipv4/tcp_bbr.c: bbr_full_bw_thresh = 5/4, bbr_full_bw_cnt = 3)
FULL_BW_GROWTH = 1.25
FULL_BW_ROUNDS = 3


def bandwidth_round_index(raw, gaps, rtt):
    """대역폭에 막히기 시작한 라운드 번호(없으면 None). BBR STARTUP이 대역폭 한계를 판정하는 방식과 같다.
    라운드별 전송률 = 라운드 크기 ÷ max(라운드 도착에 걸린 시간(서버 유휴 제외), 최소 RTT).
    대역폭에 여유가 있으면 라운드는 RTT 안에 도착하므로 전송률은 '크기 ÷ RTT'로 라운드마다 커진다.
    대역폭에 막히면 라운드가 RTT보다 오래 걸려 전송률이 병목 대역폭에서 평평해진다.
    전송률이 지금까지 최대의 1.25배 이상으로 커지지 않는 라운드가 3번 연속이면, 그 첫 라운드부터 대역폭 제한이다."""
    best, run = 0.0, 0
    for k, r in enumerate(raw):
        a, b = r[0][-1], r[-1][-1]
        busy = (r[-1][0] - r[0][0]) - sum(v for i, v in gaps.items() if a <= i < b)
        rate = len(r) / max(busy, rtt)
        if best and rate < best * FULL_BW_GROWTH:
            run += 1
            if run >= FULL_BW_ROUNDS:
                return k - FULL_BW_ROUNDS + 1
        else:
            run = 0
        best = max(best, rate)
    return None


def time_rounds(segs, rtt):
    """timestamp가 없을 때의 대체: 시간 공백으로 라운드를 나누고, 공백 없이 이어진 구간은 RTT 창으로 자른 뒤
    창 평균의 20% 미만인 조각 창은 앞 창에 합친다(창 경계가 실제 라운드와 어긋나서 생기는 조각)."""
    rounds, cur, prev = [], [], None
    for s in segs:
        if cur and s[0] - prev > rtt * ROUND_GAP_RATIO:
            rounds.append(cur); cur = []
        cur.append(s); prev = s[0]
    if cur:
        rounds.append(cur)
    out = []
    for r in rounds:
        if (r[-1][0] - r[0][0]) < rtt:     # 1 RTT 안에 도착한 묶음은 그대로 한 라운드
            out.append(r); continue
        wins, t0 = [], r[0][0]
        for s in r:
            k = int((s[0] - t0) / rtt)
            while len(wins) <= k:
                wins.append([])
            wins[k].append(s)
        wins = [w for w in wins if w]
        avg = sum(len(w) for w in wins) / len(wins)
        merged = []
        for w in wins:
            if merged and len(w) < avg * 0.2:
                merged[-1] = merged[-1] + w
            else:
                merged.append(w)
        out.extend(merged)
    return out


def payload_len(ip, tcp):
    """캡처가 잘렸어도 정확하도록 IP 헤더의 길이 필드로 TCP 데이터 길이를 계산"""
    if isinstance(ip, dpkt.ip.IP):
        return max(0, ip.len - ip.hl * 4 - tcp.off * 4)
    return max(0, ip.plen - tcp.off * 4)


def collect_flows(path, window=None):
    flows = {}
    for ts, ip in ip_packets(path):
        if window and not (window[0] <= ts <= window[1]):
            continue
        tcp = ip.data
        src, dst = addr(ip, 'src'), addr(ip, 'dst')
        if tcp.dport == 443:
            key, direction = (src, tcp.sport, dst), 'c2s'
        elif tcp.sport == 443:
            key, direction = (dst, tcp.dport, src), 's2c'
        else:
            continue
        f = flows.setdefault(key, {'client': key[0], 'cport': key[1], 'server': key[2], 'pkts': []})
        tcp.plen = payload_len(ip, tcp)
        f['pkts'].append((ts, direction, tcp))
    return flows


def flow_sni(flow):
    buf, seen = b'', set()
    for ts, d, tcp in flow['pkts']:
        if d == 'c2s' and tcp.data and tcp.seq not in seen:
            seen.add(tcp.seq); buf += bytes(tcp.data)
            if len(buf) >= 5 and len(buf) >= 5 + struct.unpack('>H', buf[3:5])[0]:
                break
            if len(buf) > 20000:
                break
    return sni_from_client_hello(buf)


def pick_flow(flows, host, server_ip, client_port=None):
    cands = []
    for f in flows.values():
        has_syn = any(d == 'c2s' and tcp.flags & dpkt.tcp.TH_SYN and not tcp.flags & dpkt.tcp.TH_ACK for _, d, tcp in f['pkts'])
        if not has_syn:
            continue
        if server_ip and f['server'] != server_ip:
            continue
        if client_port and f['cport'] != client_port:
            continue
        if host and flow_sni(f) != host:
            continue
        server_bytes = sum(tcp.plen for _, d, tcp in f['pkts'] if d == 's2c')
        cands.append((f['pkts'][0][0], -server_bytes, f))
    if not cands:
        return None, 0
    # 브라우저는 같은 호스트에 연결을 2개 동시에 열고 한쪽만 쓰기도 한다. 블로그 개선 후 캡처: SYN이 0.3ms 빠른 연결이
    # TLS handshake만 하고 HTTP 요청 없이 닫혔다. 그래서 먼저 열린 연결이 아니라
    # 리소스 대부분을 실어 나른 연결 = 서버가 가장 많이 보낸 연결을 고른다.
    cands.sort(key=lambda x: (x[1], x[0]))
    return cands[0][2], [{'client_port': c[2]['cport'], 'server_bytes': -c[1]} for c in cands]


def analyze(path, host=None, server_ip=None, client_port=None, window=None):
    flows = collect_flows(path, window)
    flow, n_candidates = pick_flow(flows, host, server_ip, client_port)
    if not flow:
        return {'error': f'대상 연결을 찾지 못했습니다 (host={host}, server_ip={server_ip}). 새 연결(SYN)부터 캡처됐는지 확인하세요.'}
    pkts = flow['pkts']
    t_syn = next(ts for ts, d, t in pkts if d == 'c2s' and t.flags & dpkt.tcp.TH_SYN)
    synack = next(((ts, t) for ts, d, t in pkts if d == 's2c' and t.flags & dpkt.tcp.TH_SYN and t.flags & dpkt.tcp.TH_ACK), None)
    if not synack:
        return {'error': 'SYN+ACK가 없습니다.'}
    t_synack, synack_tcp = synack
    opts = tcp_options(synack_tcp)
    client_syn = next(t for ts, d, t in pkts if d == 'c2s' and t.flags & dpkt.tcp.TH_SYN)
    client_opts = tcp_options(client_syn)
    handshake_rtt = t_synack - t_syn
    # 전송 중 RTT: 서버 패킷의 TSecr는 클라이언트가 보낸 TSval을 되돌려 준 값이다.
    # TSecr 값마다 그 값을 처음 실어 온 서버 패킷 도착 시각 − 그 TSval을 처음 보낸 시각 = RTT 표본.
    # 표본은 서버의 페이싱·처리 지연과 timestamp 해상도(보통 1ms) 때문에 실제보다 커질 수는 있어도 작아지지는 않는다.
    # 그래서 최솟값을 경로 RTT로 쓴다. handshake 한 번으로 잰 RTT는 와이파이 절전 해제 등으로 크게 부풀 수 있다.
    samples = rtt_samples(pkts, t_synack)
    data_rtt = min(s for _, s in samples) if len(samples) >= MIN_RTT_SAMPLES else None
    rtt = data_rtt or handshake_rtt
    rtt_ms = rtt * 1000
    ts_opt = opts.get('timestamp') and client_opts.get('timestamp')
    mss = min(opts.get('mss', 1460), client_opts.get('mss', 65535))
    payload = mss - (12 if ts_opt else 0)

    # 재전송 제거: 같은 seq의 데이터는 처음 것만
    def data_of(direction):
        out, seen = [], set()
        for ts, d, t in pkts:
            if d == direction and t.plen > 0:
                if t.seq in seen:
                    continue
                seen.add(t.seq); out.append((ts, t.plen))
        return out
    server = data_of('s2c')
    client = data_of('c2s')
    sdata, seen_seq = {}, set()
    for ts, d, tp in pkts:
        if d == 's2c' and tp.plen > 0 and tp.seq not in seen_seq:
            seen_seq.add(tp.seq); sdata[ts] = (tp.seq, tcp_options(tp).get('tsecr'))
    cacks = [(ts, tp.ack, tcp_options(tp).get('tsval')) for ts, d, tp in pkts
             if d == 'c2s' and tp.flags & dpkt.tcp.TH_ACK and not tp.flags & dpkt.tcp.TH_SYN]
    retrans = sum(1 for ts, d, t in pkts if d == 's2c' and t.plen) - len(server)

    # ClientHello 다음, 서버 handshake 응답 이후 첫 클라이언트 데이터 = Finished
    first_server = server[0][0] if server else None
    finished = next(((ts, n) for ts, n in client if first_server and ts > first_server), None)
    tls_segments = sum(1 for ts, n in server if finished and ts < finished[0])

    # 요청 묶음: 제어 프레임보다 큰 클라이언트 데이터. 브라우저가 한 번에 보내는 요청은 수 ms 안에 몰리고,
    # 다음 단계 요청은 응답을 받은 뒤(약 1 RTT 뒤) 나가므로 0.5 RTT를 기준으로 나눈다.
    reqs = [(ts, n) for ts, n in client if finished and ts >= finished[0] and n >= REQUEST_MIN_PAYLOAD]
    groups = []
    for ts, n in reqs:
        if groups and ts - groups[-1]['last'] <= rtt * ROUND_GAP_RATIO:
            groups[-1]['last'] = ts; groups[-1]['count'] += 1
        else:
            groups.append({'start': ts, 'last': ts, 'count': 1})

    resp = [(ts, n) for ts, n in server if finished and ts > finished[0]]
    # 서버가 아직 보내는 중에 나간 요청은 보통 새 단계로 보지 않는다(큰 응답 중간의 추가 요청).
    # 단, HTML을 받는 도중 나간 요청은 새 단계다: 서버가 <head>를 먼저 보내면(early flush, 스트리밍 SSR)
    # 브라우저가 HTML이 끝나기 전에 하위 리소스를 요청하기 때문이다. 이때 HTML 나머지는 다음 단계와 cwnd를 나눠 쓴다.
    stage_groups = []
    for g in groups:
        in_flight = stage_groups and any(stage_groups[-1]['start'] < ts < g['start'] for ts, _ in resp) and \
            any(0 < ts - g['start'] < rtt * ROUND_GAP_RATIO for ts, _ in resp)
        if in_flight and len(stage_groups) >= 2:
            stage_groups[-1].setdefault('mid_requests', 0)
            stage_groups[-1]['mid_requests'] += g['count']
        elif stage_groups and not any(stage_groups[-1]['start'] < ts < g['start'] for ts, _ in resp):
            # 앞 묶음 이후 서버 응답이 하나도 없었다면 같은 단계의 요청이 조금 늦게 나간 것
            stage_groups[-1]['count'] += g['count']; stage_groups[-1]['last'] = g['last']
        else:
            stage_groups.append(dict(g, overlaps_previous=bool(in_flight)))

    rtt = data_rtt or handshake_rtt
    rtt_ms = rtt * 1000
    # 1차: 단계마다 라운드 후보를 나누고 서버 유휴 시간을 잰다
    pre = []
    for i, g in enumerate(stage_groups):
        end = stage_groups[i + 1]['start'] if i + 1 < len(stage_groups) else float('inf')
        segs = [(ts, n) for ts, n in resp if g['start'] < ts < end]
        use_ack = bool(ts_opt) and bool(segs) and all(sdata.get(ts, (None, None))[1] is not None for ts, _ in segs)
        if use_ack:
            raw = ack_rounds([(ts, n, *sdata[ts], k) for k, (ts, n) in enumerate(segs)], cacks)
            first_idle, gaps = server_idle(segs, sdata, cacks, rtt, g['last'])
        else:
            raw = time_rounds([(ts, n, k) for k, (ts, n) in enumerate(segs)], rtt)
            first_idle, gaps = gap_idle(segs, g['start'], rtt)
        pre.append((g, end, segs, raw, 'ack' if use_ack else 'time', first_idle, gaps))
    # 초기 cwnd 하한: 연결 전체에서 관측한 라운드로 올린다(서버가 RFC보다 큰 초기 cwnd를 쓰는 경우)
    iw = init_cwnd_lower_bound([p[3] for p in pre], tls_segments)

    # 2차: 대역폭 제한 판정 → cwnd 라운드 합치기
    stages = []
    delivered = tls_segments
    limit = max(STALL_GAP_MIN_MS, rtt_ms * STALL_GAP_RATIO)
    for i, (g, end, segs, raw, method, first_idle, gaps) in enumerate(pre):
        idle = ([first_idle] if first_idle else []) + list(gaps.values())
        stalls = [round(x * 1000, 1) for x in idle if x * 1000 > limit]
        waits = [round(x * 1000, 1) for x in idle if 0 < x * 1000 <= limit]
        cwnd_at_start = iw + delivered
        delivered_before = delivered
        # 대역폭 판정은 합치기 전에: 대역폭에 막힌 라운드는 cwnd 하한 안에 들어가 합쳐질 수 있기 때문이다
        bw_at = bandwidth_round_index(raw, gaps, rtt)
        head = raw if bw_at is None else raw[:bw_at]
        tail = [] if bw_at is None else raw[bw_at:]
        rounds, delivered = merge_cwnd_rounds(head, delivered, iw)
        delivered += sum(len(r) for r in tail)

        def info(r, kind):
            a, b = r[0][-1], r[-1][-1]
            busy = (r[-1][0] - r[0][0]) - sum(v for j, v in gaps.items() if a <= j < b)
            return {'segments': len(r), 'bytes': sum(x[1] for x in r), 'duration_ms': round(busy * 1000, 2), 'kind': kind}
        rinfo = [info(r, 'round') for r in rounds]
        if tail:
            rinfo.append(dict(info([x for r in tail for x in r], 'bandwidth_limited'),
                              per_round_segments=[len(r) for r in tail]))
        sizes = [x['segments'] for x in rinfo if x['kind'] == 'round']
        growth_samples = []
        full = sizes[:-1]
        if not tail and len(full) >= 2 and full[0] > 0 and full[-1] > full[0]:
            growth_samples.append((full[-1] / full[0]) ** (1 / (len(full) - 1)))
        last_server = segs[-1][0] if segs else g['start']
        next_req_gap = (end - last_server) * 1000 if end != float('inf') else None
        stages.append({
            'index': i + 1,
            'request_count': g['count'] + g.get('mid_requests', 0),
            'mid_stage_requests': g.get('mid_requests', 0),
            'request_time_ms': round((g['start'] - t_syn) * 1000, 1),
            'segments': len(segs),
            'bytes': sum(n for _, n in segs),
            'cwnd_lower_bound_at_start': cwnd_at_start,   # 초기 cwnd 하한 + 이 단계 전까지 받은 세그먼트
            # budget.py가 여러 회차의 초기 cwnd 하한을 모아 다시 합칠 때 쓰는 값
            'delivered_before': delivered_before,
            'raw_round_segments': [len(r) for r in raw],
            'bandwidth_from_raw_round': bw_at,
            'overlaps_previous': g.get('overlaps_previous', False),   # HTML 수신 중에 요청이 나감(early flush)
            'rounds': rinfo,
            'round_segments': [r['segments'] for r in rinfo],
            'round_method': method if segs else None,   # ack: TCP timestamp ACK 인과관계 / time: 시간 공백
            'growth_estimate': round(growth_samples[0], 2) if growth_samples else None,
            'stalls_ms': stalls,                 # 서버 유휴 중 긴 것(CDN MISS 등). 라운드로 세지 않음
            'server_waits_ms': waits,            # 서버 유휴 중 짧은 것(서버 렌더, 오리진 대기). 라운드로 세지 않음
            'bandwidth_limited': bool(tail),
            'idle_before_next_request_ms': round(next_req_gap, 1) if next_req_gap is not None else None,
        })

    first_req = stage_groups[0]['start'] if stage_groups else None
    return {
        'file': path,
        'host': host or flow_sni(flow),
        'server': flow['server'],
        'client_port': flow['cport'],
        'candidate_connections': n_candidates,  # 다른 연결을 보려면 --client-port
        'rtt_ms': round((data_rtt or handshake_rtt) * 1000, 2),   # 전송 중 RTT(TCP timestamp), 없으면 handshake RTT
        'rtt_source': 'tcp_timestamps' if data_rtt else 'handshake',
        'handshake_rtt_ms': round(handshake_rtt * 1000, 2),
        'rtt_samples': len(samples),
        'mss': mss,
        'tcp_timestamps': bool(ts_opt),
        'payload_per_segment': payload,
        'tls_server_segments': tls_segments,
        'init_cwnd_lower_bound': iw,      # 관측한 라운드로 구한 초기 cwnd 하한(최소 10)
        # 프로토콜상 handshake는 2 RTT(TCP 1 + TLS 1.3 1). 이 값은 SYN부터 첫 요청까지 걸린 시간을 RTT로 나눈 것으로,
        # 2보다 크면 그 차이는 서버의 TLS 처리 시간 등 RTT 외 대기다.
        'first_request_after_rtt_units': round((first_req - t_syn) / handshake_rtt, 2) if first_req else None,
        'first_request_after_ms': round((first_req - t_syn) * 1000, 1) if first_req else None,
        'retransmissions': retrans,
        'stages': stages,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pcap', nargs='+')
    ap.add_argument('--host')
    ap.add_argument('--server-ip')
    ap.add_argument('--client-port', type=int)
    ap.add_argument('--window', help='capture.py가 남긴 run-N.json. 그 회차의 시작~끝 시각 패킷만 분석')
    a = ap.parse_args()
    window = None
    if a.window:
        w = json.load(open(a.window))['window']
        window = (w['start'], w['end'])
    out = [analyze(p, a.host, a.server_ip, a.client_port, window) for p in a.pcap]
    json.dump(out if len(out) > 1 else out[0], sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == '__main__':
    main()
