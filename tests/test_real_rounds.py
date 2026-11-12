"""실측(와이파이, rtt-runs/v2와 2.4GHz 측정)에서 나온 라운드 값으로 판정 규칙을 확인한다.
경험값 없이 물리적 정의만 쓰는 규칙들이다.
  1) cwnd 하한으로 합치기: 합쳐도 '초기 cwnd 하한 + ACK된 세그먼트' 안이면 cwnd가 나눈 라운드가 아님
  2) 초기 cwnd 하한은 관측으로 올림: IW ≥ 라운드 크기 − 그 전까지 받은 세그먼트
  3) 대역폭 제한: BBR의 파이프 판정(전송률이 1.25배 이상 커지지 않는 라운드 3연속, tcp_bbr.c 상수)"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'skills', 'rtt-measure', 'scripts'))
from analyze import merge_cwnd_rounds, init_cwnd_lower_bound, bandwidth_round_index

fail = 0
def check(name, ok, detail):
    global fail
    fail += not ok
    print(f"{'OK ' if ok else 'FAIL'} {name}: {detail}")

def seg_rounds(sizes):
    k, out = 0, []
    for n in sizes:
        out.append([(0, 0, k + j) for j in range(n)]); k += n
    return out

# 1)+2) 단계별 합치기 전 라운드 크기, 정답 RTT. TLS 서버 응답은 5세그먼트.
CASES = [
    ('v2 개선 전 r1', [[2], [30, 60, 62, 90]], [1, 4]),
    ('v2 개선 전 r2 (전송 중 서버 46ms 멈춤)', [[2], [30, 56, 83, 58, 15]], [1, 4]),
    ('v2 개선 전 r3', [[2], [30, 60, 112, 40]], [1, 4]),
    ('v2 개선 전 r4', [[2], [30, 60, 120, 32]], [1, 4]),
    ('v2 개선 후 r1 (꼬리 1세그먼트)', [[5], [32, 42, 69, 1]], [1, 3]),
    ('v2 개선 후 r2 (꼬리 2세그먼트)', [[5], [33, 31, 78, 2]], [1, 3]),
    ('v2 개선 후 r3', [[5], [33, 64, 47]], [1, 3]),
    ('v2 개선 후 r5', [[5], [33, 45, 66]], [1, 3]),
    ('블로그 개선 전', [[3], [30, 60, 116, 53]], [1, 4]),
    ('블로그 개선 후', [[5], [35, 70, 36]], [1, 3]),
    # 서버 렌더 HTML: 헤더 몇 세그먼트 → 대기 → 나머지. 초기 cwnd 10 가정이면 2 RTT로 셌던 경우.
    # 2단계 첫 라운드 59는 이전 보고(SSR 시작 cwnd 59)에서 가져온 값.
    ('v2 SSR r1 (서버 렌더 [2, 20])', [[2, 20], [59, 100]], [1, None]),
    ('v2 SSR r3 (서버 렌더 [20, 4])', [[20, 4], [59, 100]], [1, None]),
]
# 회차 하나만으로는 하한이 낮게 잡히는 경우: 같은 호스트의 다른 회차에서 관측한 하한(23)을 공유
SHARED = [('v2 SSR r3 [20, 4], 회차 단독 하한 15 → 공유 23', [20, 4], 5, 23, 1),
          ('v2 SSR r4 [2, 20], 회차 단독 하한 13 → 공유 23', [2, 20], 5, 23, 1)]
for name, sizes, delivered, iw, answer in SHARED:
    rounds, _ = merge_cwnd_rounds([[0] * n for n in sizes], delivered, iw)
    check(name, len(rounds) == answer, f"{[len(r) for r in rounds]} = {len(rounds)} RTT (정답 {answer})")
for name, stages, answer in CASES:
    raws = [seg_rounds(s) for s in stages]
    iw = init_cwnd_lower_bound(raws, 5)
    delivered, got = 5, []
    for raw in raws:
        rounds, delivered = merge_cwnd_rounds(raw, delivered, iw)
        got.append(len(rounds))
    ok = all(a is None or a == g for a, g in zip(answer, got))
    check(name, ok, f"초기 cwnd 하한 {iw}, 단계별 RTT {got} (정답 {answer})")

# 3) 대역폭 제한 판정: 라운드별 도착 시간(ms)과 최소 RTT(ms)
def timed_rounds(durations_ms, sizes):
    out, k, t = [], 0, 0.0
    for d, n in zip(durations_ms, sizes):
        out.append([(t + d / 1000 * j / max(n - 1, 1), 0, k + j) for j in range(n)]); k += n; t += 0.05
    return out
BW = [
    ('2.4GHz ssg r3 (링크 69Mbps)', [7.74, 13.47, 15.0, 26.08], [51, 45, 101, 123], 7.56, True),
    ('블로그 개선 전 2단계 (대역폭 여유)', [2.09, 2.34, 4.15, 1.16], [30, 60, 116, 53], 6.72, False),
    ('블로그 개선 후 2단계 (페이싱, 대역폭 여유)', [2.7, 3.75, 2.69], [35, 70, 36], 6.94, False),
    # 5GHz에서 1.25배·2연속 규칙이 오판했던 회차. 실측 도착 시간과 전송 중 RTT 19.86ms
    ('v2 CSR r3 [33, 37, 45, 125, 47] (이전 규칙 오판)', [18.41, 23.67, 24.35, 25.94, 21.89], [33, 37, 45, 125, 47], 19.86, False),
]
for name, durs, sizes, rtt_ms, answer in BW:
    got = bandwidth_round_index(timed_rounds(durs, sizes), {}, rtt_ms / 1000) is not None
    check(name, got == answer, f"대역폭 제한 {got} (정답 {answer})")

sys.exit(1 if fail else 0)
