"""합성 캡처 생성기: 실제 캡처 없이 analyze.py의 판정 규칙을 검증하기 위한 것.

모든 패킷에 TCP timestamp 옵션을 넣고, 클라이언트는 서버 데이터 2개마다 ACK를 보낸다.
서버 패킷의 TSecr는 '그 패킷을 보낼 때 서버에 도착해 있던 가장 최근 클라이언트 패킷'의 TSval이다.
"""
import bisect, dpkt, socket, struct

C, S = socket.inet_aton('192.168.0.10'), socket.inet_aton('3.3.3.3')
HOST = 'example.com'
T0 = 1_700_000_000.0


def client_hello(host):
    name = host.encode()
    sni = struct.pack('>HB H', len(name) + 3, 0, len(name)) + name
    ext = struct.pack('>HH', 0, len(sni)) + sni
    body = b'\x03\x03' + b'\x00' * 32 + b'\x00' + b'\x00\x02\x13\x01' + b'\x01\x00' + struct.pack('>H', len(ext)) + ext
    hs = b'\x01' + len(body).to_bytes(3, 'big') + body
    return b'\x16\x03\x01' + struct.pack('>H', len(hs)) + hs


class Flow:
    def __init__(self, rtt=0.02, mss=1250, payload=1238, handshake_rtt=None):
        self.rtt = rtt                                  # 전송 중 실제 RTT
        self.hs_rtt = handshake_rtt or rtt              # SYN→SYN+ACK (와이파이 절전 해제 등으로 부풀 수 있음)
        self.mss, self.payload = mss, payload
        self.events = []                                # (t, c2s, data, flags, mss_opt)
        self.rtt_from = []                              # [(시각, 그 시각부터의 실제 왕복)] 큐가 쌓여 RTT가 늘어나는 경우

    def add(self, t, c2s, data=b'', flags=dpkt.tcp.TH_ACK, mss_opt=None):
        self.events.append((t, c2s, data, flags, mss_opt))

    def handshake(self):
        r, h = self.rtt, self.hs_rtt
        self.add(0, True, flags=dpkt.tcp.TH_SYN, mss_opt=1460)
        self.add(h, False, flags=dpkt.tcp.TH_SYN | dpkt.tcp.TH_ACK, mss_opt=self.mss)
        self.add(h, True, client_hello(HOST))
        for k in range(5):
            self.add(h + r + k * 1e-4, False, b'\x17' * self.payload)
        self.add(h + 2 * r, True, b'\x17' * 64)       # Finished
        return h + 2 * r

    def request(self, t, size=120):
        self.add(t, True, b'\x17' * size)

    def burst(self, t, n, spread=0.0):
        """t부터 n세그먼트. spread>0이면 그 시간 동안 고르게(페이싱)"""
        for k in range(n):
            self.add(t + (spread * k / n if spread else k * 1e-5), False, b'\x17' * self.payload)

    def write(self, path):
        ev = sorted(self.events, key=lambda e: e[0])
        # 서버 데이터 2개마다 클라이언트 ACK
        acks, cnt = [], 0
        for t, c2s, data, flags, _ in ev:
            if not c2s and data:
                cnt += 1
                if cnt % 2 == 0:
                    acks.append((t + 1e-6, True, b'', dpkt.tcp.TH_ACK, None))
        ev = sorted(ev + acks, key=lambda e: e[0])
        client_times, client_vals = [], []
        w = dpkt.pcap.Writer(open(path, 'wb'))
        cseq, sseq = 1000, 5000
        for t, c2s, data, flags, mss_opt in ev:
            if c2s:
                tsval, tsecr = 100000 + int(t * 1000), 0
                client_times.append(t); client_vals.append(tsval)
            else:
                tsval = 900000 + int(t * 1000)
                # 서버가 이 패킷을 보낸 시각 ≈ t - rtt/2, 그때 서버에 도착해 있던 클라이언트 패킷 = 보낸 시각 ≤ t - rtt
                cur_rtt = self.rtt
                for since, r in self.rtt_from:
                    if t >= since:
                        cur_rtt = r
                k = bisect.bisect_right(client_times, t - cur_rtt + 1e-9) - 1
                tsecr = client_vals[k] if k >= 0 else (client_vals[0] if client_vals else 0)
            opts = b''
            if mss_opt:
                opts += struct.pack('>BBH', 2, 4, mss_opt)
            opts += b'\x01\x01' + struct.pack('>BBII', 8, 10, tsval, tsecr)
            tcp = dpkt.tcp.TCP(sport=50000 if c2s else 443, dport=443 if c2s else 50000, flags=flags, opts=opts, data=data)
            tcp.off = 5 + len(opts) // 4
            if c2s:
                tcp.seq = cseq; cseq += max(len(data), 1 if flags & dpkt.tcp.TH_SYN else 0)
                tcp.ack = sseq if sseq > 5000 else 0      # 지금까지 받은 서버 데이터 다음 바이트
            else:
                tcp.seq = sseq; sseq += max(len(data), 1 if flags & dpkt.tcp.TH_SYN else 0)
            ip = dpkt.ip.IP(src=C if c2s else S, dst=S if c2s else C, p=6, data=tcp)
            ip.len = 20 + len(bytes(tcp))
            w.writepkt(bytes(dpkt.ethernet.Ethernet(src=b'\x01' * 6, dst=b'\x02' * 6, type=0x0800, data=ip)), ts=T0 + t)
