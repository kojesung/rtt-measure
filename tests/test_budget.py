"""budget.py 보고 규칙을 합성 analysis.json으로 확인한다.
  1) 총 RTT는 단계별 최빈값의 합이 아니라 회차별 합의 범위·분포
  2) 회차별 라운드가 N과 N+1로 갈리면 범위로 내고, 모델 여유와 실측 "N으로 고정하려면"을 함께 낸다
  3) 판정 불가일 때 재측정 안내: 9회 미만이면 회차를 늘리고, 이미 9회 이상이면 네트워크를 바꾼다
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


def stage_row(text, i):
    return next(l for l in text.splitlines() if l.startswith(f'| {i} '))


def note(text, i):
    """'- i단계: … 오갔습니다' 줄과 그 아래 '  - ' 줄들을 한 문자열로"""
    lines = text.splitlines()
    k = next((j for j, l in enumerate(lines) if l.startswith(f'- {i}단계:') and '오갔습니다' in l), None)
    if k is None:
        return ''
    block = [lines[k]]
    for l in lines[k + 1:]:
        if not l.startswith('  - '):
            break
        block.append(l)
    return '\n'.join(block)


# 2) 핫스팟 실측과 같은 모양: 3 RTT 5회, 4 RTT 4회. 4 RTT 회차는 증가가 2배에 크게 못 미쳐(58 → 78) 마지막 라운드가 큼
runs = [run(k, [[5], [60, 123, 222]]) for k in range(1, 6)] + [run(k, [[5], [58, 78, 128, 141]]) for k in range(6, 10)]
text = budget(runs)
check('갈린 단계는 범위로 표시', '| 3~4 (' in stage_row(text, 2), stage_row(text, 2))
n = note(text, 2)
check('분포를 적음', '3 RTT 5회, 4 RTT 4회' in n, n)
check('실측 마지막 라운드로 고정 감소량', '172.0kB 이상 줄이면' in n, n)        # 141 × 1238 / 1.015
check('모델이 N이면 늘릴 수 있는 양을 함께', '18.3kB 넘게 늘면' in n and '항상 4 RTT' in n, n)
check('느린 회차의 배율을 보여 줌', '2번째 라운드에서 1.34배만' in n, n)  # 420 − 405 = 15세그먼트
check('총합도 회차별', '6~7 (6: 5회, 7: 4회)' in total_line(text), total_line(text))

# 크기가 경계에 걸친 경우: 4 RTT 회차가 마지막 라운드에 2세그먼트만 보냄 → 실측 감소량도 작음
runs = [run(k, [[5], [60, 120, 225]]) for k in range(1, 4)] + [run(k, [[5], [60, 118, 225, 2]]) for k in range(4, 6)]
n = note(budget(runs), 2)
check('경계: 실측 감소량이 작음', '2.4kB 이상 줄이면' in n, n)

# 모델이 N+1이면 모델 기준 감소량을 적는다
runs = [run(k, [[5], [60, 125, 245]]) for k in range(1, 4)] + [run(k, [[5], [60, 120, 240, 10]]) for k in range(4, 6)]
n = note(budget(runs), 2)
check('모델이 N+1이면 모델 감소량', '12.2kB 이상 줄이면 2배씩 보낼 때 3 RTT 안에' in n, n)

# 모델이 N도 N+1도 아니면 경계라고 하지 않는다
runs = [run(k, [[5], [150, 100, 50]]) for k in range(1, 4)] + [run(k, [[5], [150, 100, 40, 10]]) for k in range(4, 6)]
n = note(budget(runs), 2)
check('모델이 맞지 않으면 경계 판단 안 함', '판단하지 않고' in n and '경계에 걸쳐 있다는 뜻' not in n, n)

# 2 이상 차이 나면 지금처럼 판정 불가
runs = [run(k, [[5], [60, 120, 200]]) for k in range(1, 4)] + [run(4, [[5], [30, 40, 60, 100, 150]])]
check('2 이상 차이는 판정 불가', '판정 불가' in stage_row(budget(runs), 2), stage_row(budget(runs), 2))


# 3) 재측정 안내
def unstable(n_runs, rtts):
    runs = [run(k + 1, [[5], [60, 120, 200]], rtt_ms=rtts[k]) for k in range(n_runs - 1)]
    runs.append(run(n_runs, [[5], [30, 40, 60, 100, 150]], rtt_ms=rtts[-1]))
    return next(l for l in budget(runs).splitlines() if 'RTT 수를 하나로 말할 수 없습니다' in l)

line = unstable(5, [20] * 5)
check('5회면 9회로 늘리라고 안내', '9회로 늘려' in line, line)
line = unstable(9, [20] * 8 + [45])
check('9회 + RTT 2배 흔들림이면 네트워크 변경', '이미 9회' in line and '2배 이상 흔들렸으므로' in line and '9회로 늘려' not in line, line)
line = unstable(9, [20] * 9)
check('9회 + RTT 안정이면 네트워크 변경', '이미 9회' in line and '유선이나 다른 네트워크' in line, line)

sys.exit(1 if fail else 0)
