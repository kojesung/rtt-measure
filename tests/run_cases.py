import json, os, subprocess, sys
sys.path.insert(0, os.path.dirname(__file__))
from gen import Flow, HOST
D = os.path.dirname(__file__)

def analyze(script, path):
    out = subprocess.run([sys.executable, '-I', script, path, '--host', HOST], capture_output=True, text=True)
    return json.loads(out.stdout)

def case_ssr_wait(path):
    """1단계: HTML 3세그먼트 → 서버 렌더 대기 → 22세그먼트. 2단계: 정상 슬로 스타트 40/80/30
    기대: 1단계 2라운드, 서버 유휴 약 60ms. 이 합성에서는 관측으로 올릴 수 있는 초기 cwnd 하한이 14라 3+22=25가
    하한(14+5) 안에 들어가지 않는다. 즉 초기 cwnd가 실제로 작다면 대기가 없었어도 2라운드가 필요했다"""
    f = Flow(rtt=0.02); t = f.handshake()
    f.request(t); f.burst(t + 0.02, 3); f.burst(t + 0.02 + 0.08, 22)
    t2 = t + 0.02 + 0.08 + 0.03
    f.request(t2); f.burst(t2 + 0.02, 40); f.burst(t2 + 0.04, 80); f.burst(t2 + 0.06, 30)
    f.write(path)

def case_paced_slow_start(path):
    """2단계: 페이싱으로 공백 없이, 47/66/71/137/52.
    기대: 4라운드. 관측으로 구한 초기 cwnd 하한으로 보면 마지막 52는 앞 라운드와 합쳐도 cwnd 하한 안이라 cwnd 때문에 밀린 게 아니다"""
    f = Flow(rtt=0.025); t = f.handshake()
    f.request(t); f.burst(t + 0.025, 5)
    t2 = t + 0.06; f.request(t2)
    s = t2 + 0.025
    for k, n in enumerate([47, 66, 71, 137, 52]):
        f.burst(s + k * 0.025, n, spread=0.015)   # 페이싱: RTT의 0.6
    f.write(path)

def case_bandwidth(path):
    """2단계: 대역폭 제한, RTT 창마다 36개로 평평 (2.4GHz 69Mbps 같은 경우)"""
    f = Flow(rtt=0.006); t = f.handshake()
    f.request(t); f.burst(t + 0.006, 5)
    t2 = t + 0.02; f.request(t2)
    s = t2 + 0.006
    for k, n in enumerate([30, 36, 36, 36, 36, 36, 20]):
        f.burst(s + k * 0.006, n, spread=0.00599)
    f.write(path)

def case_early_flush(path):
    """HTML <head> 3세그먼트 먼저 → 브라우저가 바로 하위 리소스 요청 → HTML 나머지와 리소스가 섞여 옴"""
    f = Flow(rtt=0.02); t = f.handshake()
    f.request(t); f.burst(t + 0.017, 3)
    f.request(t + 0.019)                      # HTML 받는 중에 나간 요청. 실제 RTT가 handshake 때보다 조금 짧았던 경우
    f.burst(t + 0.06, 20); f.burst(t + 0.08, 50); f.burst(t + 0.10, 40)
    f.write(path)

def case_inflated_handshake(path):
    """handshake RTT 60ms(와이파이 절전 해제로 부풂), 실제 RTT 20ms. 2단계는 페이싱 슬로 스타트 40/80/160/40"""
    f = Flow(rtt=0.02, handshake_rtt=0.06); t = f.handshake()
    f.request(t); f.burst(t + 0.02, 5)
    t2 = t + 0.045; f.request(t2)
    s = t2 + 0.02
    for k, n in enumerate([40, 80, 160, 40]):
        f.burst(s + k * 0.02, n, spread=0.012)    # 페이싱: RTT의 0.6
    f.write(path)

def case_phase_shift(path):
    """실측 실패 재현: 정답 4라운드(30/60/120/32). 최소 RTT는 19ms지만 전송 중에는 큐가 쌓여
    라운드 간격이 23ms로 늘었고, 서버가 라운드마다 간격 전체에 걸쳐 페이싱해서 공백이 없다.
    최소 RTT 격자(19ms)로 자르면 라운드 경계와 위상이 어긋나 조각 창이 생긴다."""
    f = Flow(rtt=0.019, handshake_rtt=0.04); t = f.handshake()
    f.request(t); f.burst(t + 0.019, 2)
    t2 = t + 0.03; f.request(t2)
    s = t2 + 0.019
    f.rtt_from.append((s, 0.023))                   # 2단계부터 실제 왕복(큐 포함) 23ms
    for k, n in enumerate([30, 60, 120, 32]):
        f.burst(s + k * 0.023, n, spread=0.014)   # 페이싱: 라운드 간격의 0.6
    f.write(path)

CASES = [('SSR 서버 렌더 대기', case_ssr_wait), ('페이싱 슬로 스타트', case_paced_slow_start),
         ('대역폭 제한', case_bandwidth), ('early flush', case_early_flush),
         ('handshake RTT 부풂', case_inflated_handshake), ('라운드 위상 어긋남', case_phase_shift)]

if __name__ == '__main__':
    scripts = sys.argv[1:]
    for name, fn in CASES:
        p = os.path.join(D, name.replace(' ', '_') + '.pcap'); fn(p)
        for sc in scripts:
            d = analyze(sc, p)
            st = [(s['index'], s['round_segments'], '대역폭제한' if s['bandwidth_limited'] else '', s['stalls_ms'] + s.get('server_waits_ms', [])) for s in d['stages']]
            st.append(f"rtt {d['rtt_ms']}ms")
            print(f'{name:12} | {os.path.basename(os.path.dirname(os.path.dirname(sc)))[:10]:10} | {st}')
