"""budget.py 보고 규칙을 합성 analysis.json으로 확인한다.
  1) 총 RTT는 단계별 최빈값의 합이 아니라 회차별 합의 범위·분포
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'skills', 'rtt-measure', 'scripts'))
from budget import calibrate, report

fail = 0
def check(name, ok, detail):
    global fail
    fail += not ok
    print(f"{'OK ' if ok else 'FAIL'} {name}: {detail}")


def stage(index, rounds, bandwidth_limited=False):
    return {'index': index, 'round_segments': rounds, 'segments': sum(rounds), 'bandwidth_limited': bandwidth_limited,
            'growth_estimate': None, 'stalls_ms': [], 'server_waits_ms': [], 'idle_before_next_request_ms': 5.0,
            'cwnd_lower_bound_at_start': 31, 'delivered_before': 5, 'paced': False, 'overlaps_previous': False}


def run(n, stages, rtt_ms=20.0):
    return {'file': f'run-{n}.pcap', 'host': 'example.com', 'rtt_ms': rtt_ms, 'rtt_source': 'tcp_timestamps',
            'handshake_rtt_ms': rtt_ms, 'payload_per_segment': 1238, 'tls_server_segments': 5,
            'init_cwnd_lower_bound': 31, 'first_request_after_ms': 2 * rtt_ms, 'retransmissions': 0,
            'stages': [stage(i + 1, r) for i, r in enumerate(stages)]}


def budget(runs, target=2, what_if=None):
    return report(calibrate(runs), what_if or {}, target)


def total_line(text):
    return next(l for l in text.splitlines() if '총 RTT' in l)


# 1) 회차별 합: 1단계가 한 회차만 2 RTT이고, 그 회차의 2단계는 3 RTT
#    단계별 최빈값을 더하면 2 + 1 + 3 = 6, 실제 회차별 합은 6(3회)과 7(1회)
runs = [run(1, [[5], [60, 120, 200]]), run(2, [[5], [60, 120, 200]]), run(3, [[5], [60, 120, 200]]),
        run(4, [[3, 2], [60, 120, 200]])]
line = total_line(budget(runs))
check('1단계만 갈린 회차를 총합에 반영', '6~7 (6: 3회, 7: 1회)' in line, line)

# 같은 회차에서 두 단계가 함께 늘어난 경우: 최빈값 합(6)이나 min~max 합(6~8)이 아니라 6과 8
runs = [run(1, [[5], [60, 120, 200]]), run(2, [[5], [60, 120, 200]]), run(3, [[5], [60, 120, 200]]),
        run(4, [[3, 2], [40, 60, 120, 160]])]
line = total_line(budget(runs))
check('한 회차에서 두 단계가 함께 늘어남', '6~8 (6: 3회, 8: 1회)' in line, line)

# 모든 회차가 같으면 숫자 하나
runs = [run(k, [[5], [60, 120, 200]]) for k in range(1, 4)]
line = total_line(budget(runs))
check('모든 회차가 같음', line.endswith(': 6**'), line)

sys.exit(1 if fail else 0)
