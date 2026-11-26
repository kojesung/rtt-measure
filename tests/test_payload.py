"""세그먼트당 데이터: 캡처한 서버 데이터 세그먼트의 TCP 옵션 길이를 MSS에서 뺀다."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'skills', 'rtt-measure', 'scripts'))
sys.path.insert(0, os.path.dirname(__file__))
import dpkt
from analyze import analyze, data_option_bytes
from gen import Flow

fails = 0
def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ('' if ok else f' (기대 {want!r})'))


def seg(opt_len, data=b'x' * 100, s2c=True):
    t = dpkt.tcp.TCP(opts=b'\x01' * opt_len, data=data)
    t.off = 5 + opt_len // 4
    t.plen = len(data)
    return (0.0, 's2c' if s2c else 'c2s', t)


check('timestamp만(12B)', data_option_bytes([seg(12)] * 5), 12)
check('옵션 없음', data_option_bytes([seg(0)] * 5), 0)
check('가장 흔한 값을 씀(SACK 붙은 몇 개 무시)', data_option_bytes([seg(12)] * 8 + [seg(24)] * 2), 12)
check('데이터 없는 ACK·클라이언트 패킷은 제외', data_option_bytes([seg(20, b'')] * 5 + [seg(8, s2c=False)] * 5 + [seg(12)]), 12)
check('서버 데이터가 없으면 None', data_option_bytes([seg(12, b'')]), None)

# 실제 pcap 경로: gen.py는 데이터 세그먼트마다 NOP 2 + timestamp 10 = 12B를 붙인다
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, 'f.pcap')
    f = Flow(mss=1250, payload=1238)
    f.handshake(); f.request(0.06); f.burst(0.08, 20); f.write(p)
    r = analyze(p)
    check('pcap: 옵션 길이', r['tcp_option_bytes'], 12)
    check('pcap: 출처', r['payload_source'], 'header')
    check('pcap: 세그먼트당 데이터', r['payload_per_segment'], 1238)

sys.exit(1 if fails else 0)
