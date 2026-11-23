#!/usr/bin/env python3
"""analyze.py 결과(여러 번 캡처)로 모델을 보정하고, 단계별로 "몇 바이트 줄이면 1 RTT가 줄어드는지" 계산한다.

사용:
  budget.py runs/*.json                      # 측정한 그대로의 예산
  budget.py runs/*.json --stage-bytes 2=170300   # 2단계 전송 크기가 170,300B라면? (압축 후 크기)
"""
import argparse, json, math, os, statistics, sys
from collections import Counter

SLOW_START_GROWTH = 2.0  # 슬로 스타트의 라운드당 cwnd 증가(RFC 5681)
OVERHEAD = 1.015     # 압축된 본문 → TLS 레코드·HTTP/2 프레임 포함 실제 전송량 (블로그 실측 172,832 / 170,300)
MARGIN_SEGMENTS = 2  # 경계에 딱 맞추지 않도록 권장값에 더하는 여유


def load(paths):
    runs = []
    for p in paths:
        d = json.load(open(p))
        runs.extend(d if isinstance(d, list) else [d])
    ok = [r for r in runs if 'error' not in r]
    bad = [r['error'] for r in runs if 'error' in r]
    # 재전송이 있으면 패킷 손실로 cwnd가 줄어 슬로 스타트 전제가 깨지므로 뺀다
    lossy = [r for r in ok if r.get('retransmissions')]
    ok = [r for r in ok if not r.get('retransmissions')]
    bad += [f"{r.get('file', '?')}: 재전송 {r['retransmissions']}회로 제외" for r in lossy]
    return ok, bad


def share_init_cwnd(runs, extra=()):
    """초기 cwnd는 서버 설정이라 같은 호스트면 연결마다 같다. 회차마다 관측으로 구한 하한 중 최댓값도 여전히 관측에 근거한
    하한이므로, 같은 호스트의 모든 회차에 그 값을 적용해 라운드를 다시 합친다. (한 회차만으로는 증거가 부족해 하한이 10~25로 흔들린다)"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from analyze import merge_cwnd_rounds
    by_host = {}
    for r in runs:
        by_host.setdefault(r.get('host'), []).append(r)
    shared = {}
    for host, rs in by_host.items():
        # extra: 같은 서버의 다른 페이지 측정. 초기 cwnd 하한을 정하는 데만 쓰고 예산 계산에는 섞지 않는다
        iw = max(r.get('init_cwnd_lower_bound', 10) for r in list(rs) + [x for x in extra if x.get('host') == host])
        shared[host] = iw
        for r in rs:
            if r.get('init_cwnd_lower_bound', 10) == iw:
                continue
            for s in r['stages']:
                raw = s.get('raw_round_segments')
                if raw is None:
                    continue
                bw = s.get('bandwidth_from_raw_round')
                head = raw if bw is None else raw[:bw]
                tail = [] if bw is None else raw[bw:]
                merged, _ = merge_cwnd_rounds([[0] * n for n in head], s['delivered_before'], iw)
                s['round_segments'] = [len(m) for m in merged] + ([sum(tail)] if tail else [])
                s['cwnd_lower_bound_at_start'] = iw + s['delivered_before']
    return shared


def mode(values):
    values = [v for v in values if v is not None]
    if not values:
        return None, 0
    v, n = Counter(values).most_common(1)[0]
    return v, n


def calibrate(runs):
    payload, n_payload = mode([r['payload_per_segment'] for r in runs])
    tls, n_tls = mode([r['tls_server_segments'] for r in runs])
    rtt = statistics.median(r['rtt_ms'] for r in runs)
    stage_count = mode([len(r['stages']) for r in runs])[0]
    stages = []
    for i in range(stage_count or 0):
        per = [r['stages'][i] for r in runs if len(r['stages']) > i]
        first_full = [s['round_segments'][0] for s in per if len(s['round_segments']) >= 2 and not s['bandwidth_limited']]
        growth = [s['growth_estimate'] for s in per if s.get('growth_estimate') and not s['bandwidth_limited']]
        idx = [k for k, r in enumerate(runs) if len(r['stages']) > i]
        ok = [k for k in idx if not runs[k]['stages'][i]['bandwidth_limited']]
        observed = [len(runs[k]['stages'][i]['round_segments']) for k in ok]
        firsts = [(runs[k]['rtt_ms'], runs[k]['stages'][i]['round_segments'][0]) for k in ok
                  if len(runs[k]['stages'][i]['round_segments']) >= 2]
        stages.append({
            'index': i + 1,
            'runs': len(per),
            'observed_rounds': observed,                       # 회차별로 실제 센 라운드 수
            'run_rounds': {k: len(runs[k]['stages'][i]['round_segments']) for k in ok},   # 회차 번호 → 라운드 수 (총합을 회차별로 더할 때)
            'run_round_segments': {k: runs[k]['stages'][i]['round_segments'] for k in ok},  # 회차 번호 → 라운드별 세그먼트 수
            'rate_like': rate_like(firsts),
            'segments_median': statistics.median(s['segments'] for s in per),
            'rounds_mode': mode([len(s['round_segments']) for s in per]),
            'start_cwnd': statistics.median(first_full) if first_full else None,
            # 시작 cwnd를 라운드로 잴 수 없는 단계(한 라운드에 끝남)는 관측한 cwnd 하한을 쓴다
            'cwnd_lower_bound': statistics.median([s['cwnd_lower_bound_at_start'] for s in per if s.get('cwnd_lower_bound_at_start')]) if any(s.get('cwnd_lower_bound_at_start') for s in per) else None,
            'start_cwnd_samples': first_full,
            'growth': round(statistics.median(growth), 2) if growth else None,
            'bandwidth_limited_runs': sum(1 for s in per if s['bandwidth_limited']),
            'paced_runs': sum(1 for s in per if s.get('paced')),
            'overlap_runs': sum(1 for s in per if s.get('overlaps_previous')),
            'stalls_ms': [x for s in per for x in s['stalls_ms']],
            'server_waits_ms': [x for s in per for x in s.get('server_waits_ms', [])],
            'idle_before_next_ms': [s['idle_before_next_request_ms'] for s in per if s['idle_before_next_request_ms'] is not None],
        })
    # 서버 TLS 처리 = 첫 요청까지 걸린 시간 − TCP(실측 SYN→SYN+ACK) − TLS 1 RTT(전송 중 RTT)
    tls_wait = [r['first_request_after_ms'] - r['handshake_rtt_ms'] - r['rtt_ms'] for r in runs
                if r.get('first_request_after_ms') and r.get('handshake_rtt_ms')]
    hs = [r['handshake_rtt_ms'] for r in runs if r.get('handshake_rtt_ms')]
    return {'runs': len(runs), 'rtt_ms': rtt, 'rtt_range': (min(r['rtt_ms'] for r in runs), max(r['rtt_ms'] for r in runs)),
            'rtt_source': mode([r.get('rtt_source', 'handshake') for r in runs])[0],
            'handshake_rtt_range': (min(hs), max(hs)) if hs else None,
            'tls_wait_ms': statistics.median(tls_wait) if tls_wait else None, 'payload': payload, 'payload_agree': n_payload,
            'tls_segments': tls, 'tls_agree': n_tls, 'stages': stages}


RATE_CORR = 0.8        # 첫 라운드 크기와 RTT의 상관계수가 이보다 크면 속도 기반 전송 의심
RATE_RTT_SPREAD = 1.5  # 회차별 RTT가 이 배수 이상 벌어졌을 때만 상관을 볼 수 있다
UNSTABLE_SPREAD = 2    # 회차별 라운드 수가 이만큼 이상 차이 나면 불안정


def rate_like(pairs):
    """슬로 스타트라면 첫 라운드 크기는 cwnd로 정해져 RTT와 무관하다.
    RTT가 긴 회차일수록 첫 라운드가 크다면, 서버가 일정한 속도로 보내는 데이터를 RTT 길이로 잘라 센 것이다."""
    if len(pairs) < 4:
        return None
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    if max(xs) < min(xs) * RATE_RTT_SPREAD:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs)); sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    r = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)
    return round(r, 2)


def capacities(start, growth, n):
    caps, c = [], start
    for _ in range(n):
        caps.append(math.floor(c)); c *= growth
    return caps


def plan(segments, start, growth):
    """segments를 보내는 데 필요한 라운드 수, 1 RTT 줄이려면 줄일 세그먼트, 다음 RTT까지 여유 세그먼트"""
    caps, total, n = [], 0, 0
    c = start
    while total < segments:
        caps.append(math.floor(c)); total += math.floor(c); c *= growth; n += 1
    prev_total = total - caps[-1] if caps else 0
    cut = segments - prev_total if n > 1 else None
    return {'rounds': n, 'cut_segments': cut, 'headroom_segments': total - segments, 'capacities': caps}


def waits_table(cal, target):
    """RTT로 세지 않은 대기를 한 표로: 구간 | 시간 | 의미"""
    rtt = cal['rtt_ms']
    rows = []
    tw = cal.get('tls_wait_ms')
    if tw is not None:
        rows.append(('서버 TLS 처리', f'약 {tw:.0f}ms (≈ {tw / rtt:.1f} RTT)' if tw >= 0.2 * rtt else '없음',
                     '첫 요청까지 걸린 시간에서 TCP·TLS 왕복을 뺀 나머지'))
    stages = [s for s in cal['stages'] if not target or s['index'] <= target]
    for s in stages:
        if target and s['index'] >= target:
            break
        if s['idle_before_next_ms']:
            m = statistics.median(s['idle_before_next_ms'])
            what = 'HTML 파싱 등 브라우저 쪽 대기' if s['index'] == 1 else 'JS 실행 등 브라우저 쪽 대기'
            rows.append((f"{s['index']}단계 수신 후 다음 요청까지", f'약 {m:.0f}ms (≈ {m / rtt:.1f} RTT)', what))
    stalls = [(s['index'], x) for s in stages for x in s['stalls_ms']]
    rows.append(('CDN MISS 등 긴 대기', ', '.join(f'{i}단계 {x:.0f}ms' for i, x in stalls) if stalls else '없음',
                 '오리진에서 가져오는 시간. 라운드에서 제외' if stalls else '-'))
    waits = [(s['index'], x) for s in stages for x in s['server_waits_ms']]
    rows.append(('서버 대기', ', '.join(f'{i}단계 {x:.0f}ms' for i, x in waits) if waits else '없음',
                 'cwnd를 다 쓰기 전에 생긴 공백(오리진 대기 등). 라운드에서 제외' if waits else '-'))
    out = ['### RTT가 아닌 대기', '', '| 구간 | 시간 | 의미 |', '| --- | --- | --- |']
    out += [f'| {a} | {b} | {c} |' for a, b, c in rows]
    out.append('')
    out.append(f'<sub>"≈ n RTT"는 시간을 RTT로 나눈 참고값이며, 총 RTT에는 더하지 않는다. 여러 회차는 중앙값.</sub>')
    return out


def kb(b):
    return f'{b / 1000:.1f}kB'


def total_text(per_run):
    """회차별 총 RTT의 범위와 분포. 단계별 최빈값을 더하면 실제로 나온 적 없는 조합이 될 수 있어 회차마다 더한다."""
    totals = [v for v in per_run.values() if v is not None]
    if not totals:
        return '판정할 수 있는 회차 없음'
    excluded = len(per_run) - len(totals)
    dist = sorted(Counter(totals).items())
    txt = f'{dist[0][0]}' if len(dist) == 1 else f"{dist[0][0]}~{dist[-1][0]} ({', '.join(f'{v}: {c}회' for v, c in dist)})"
    return txt + (f' · 대역폭 제한 등으로 {excluded}회 제외' if excluded else '')


def slowest_growth(rounds):
    """마지막 라운드(남은 양만 보낸 부분 라운드)를 빼고, 가장 적게 늘어난 라운드 번호와 배율"""
    ratios = [(j + 2, rounds[j + 1] / rounds[j]) for j in range(len(rounds) - 2) if rounds[j]]
    return min(ratios, key=lambda x: x[1]) if ratios else None


def split_note(i, s, p, payload, head):
    """회차별 라운드가 N과 N+1로 갈린 단계. 경계 여부를 기준 숫자로 판정하지 않고, 회차 예시와
    "늘리면 / 줄이면 어떻게 되는지"를 나란히 보여 준다.
    - 늘리는 쪽(모델): 라운드마다 정확히 2배여도 N 라운드에 담을 수 없게 되는 크기
    - 줄이는 쪽(실측): N+1이 나온 회차의 마지막 라운드. 이만큼 줄이면 그 회차도 N 라운드에 담긴다
    둘 다 작으면 크기가 경계에 걸친 것이고, 줄일 양이 크면 그 회차의 전송이 느렸던 것이다."""
    n = min(s['observed_rounds'])
    low = sum(1 for v in s['observed_rounds'] if v == n)
    high = len(s['observed_rounds']) - low
    rs = s['run_round_segments']
    fast = next(rs[k] for k, v in s['run_rounds'].items() if v == n)
    slow = max((rs[k] for k, v in s['run_rounds'].items() if v == n + 1), key=lambda r: r[-1])
    over = slow[-1]
    seq = lambda r: ' → '.join(map(str, r)) + '세그먼트'

    g_fast, g_slow = slowest_growth(fast), slowest_growth(slow)

    lines = [f"{i}단계: {n} RTT와 {n + 1} RTT를 오갔습니다({n} RTT {low}회, {n + 1} RTT {high}회). RTT는 {n}~{n + 1}로 봅니다."]
    lines.append(f"{n} RTT 회차 예: {seq(fast)}" + (f" (라운드마다 {g_fast[1]:.2f}배 이상 늘어남)" if g_fast and g_fast[1] >= 1 else ''))
    why_slow = f"{g_slow[0]}번째 라운드에서 {g_slow[1]:.2f}배만 늘어, " if g_slow and g_slow[1] < SLOW_START_GROWTH else ''
    lines.append(f"{n + 1} RTT 회차 예: {seq(slow)} ({why_slow}마지막 {over}세그먼트가 한 라운드 밀림)")
    cut_fix = f"{kb(over * payload / OVERHEAD)} 이상 줄이면(권장 {kb((over + MARGIN_SEGMENTS) * payload / OVERHEAD)})"
    if p['rounds'] == n:
        lines.append(f"{head} 넘게 늘면: 라운드마다 정확히 2배씩 보내도 {n} 라운드에 담을 수 없어, {n} RTT로 끝나던 회차도 {n + 1} RTT가 됩니다. 이 네트워크에서는 항상 {n + 1} RTT로 봐야 합니다.")
        lines.append(f"{cut_fix}: 이번에 {n + 1} RTT였던 회차도 {n} RTT 안에 들어옵니다(실측).")
    elif p['rounds'] == n + 1:
        lines.append(f"지금 크기는 라운드마다 정확히 2배씩 보내도 {n} 라운드에 담을 수 없어, {n} RTT 회차는 서버가 2배보다 많이 보낸 경우입니다. "
                     f"{kb(p['cut_segments'] * payload / OVERHEAD)} 이상 줄이면 2배씩 보낼 때 {n} RTT 안에 들어옵니다.")
        lines.append(f"{cut_fix}: 이번에 {n + 1} RTT였던 회차도 {n} RTT 안에 들어옵니다(실측).")
    else:
        lines.append(f"{cut_fix}: 이번에 {n + 1} RTT였던 회차도 {n} RTT 안에 들어옵니다(실측).")
        lines.append(f"라운드마다 2배로 계산하면 {p['rounds']} 라운드라 실측과 맞지 않습니다. 경계에 걸친 것인지는 판단하지 않고, '1 RTT 줄이려면'도 참고로만 봅니다.")
        return '\n  - '.join(lines)
    lines.append(f"늘릴 수 있는 양과 줄여야 하는 양이 둘 다 작으면 크기가 RTT 경계에 걸쳐 있다는 뜻이고, 실제 사용자도 {n} RTT와 {n + 1} RTT를 오갑니다. "
                 f"줄여야 하는 양이 훨씬 크면 크기보다 {n + 1} RTT 회차의 전송이 느렸던 것이 원인입니다.")
    return '\n  - '.join(lines)


ENOUGH_RUNS = 9   # SKILL.md가 흔들릴 때 늘리라고 권하는 회차 수


def remeasure_advice(cal):
    """이미 충분히 쟀다면 회차를 더 늘리라는 말은 소용없다. 그때는 네트워크를 바꿔 보라고 한다."""
    n = cal['runs']
    if n < ENOUGH_RUNS:
        return f"회차를 {ENOUGH_RUNS}회로 늘려 다시 재 보세요."
    lo, hi = cal['rtt_range']
    why = f"전송 중 RTT가 회차별 {lo:.1f}~{hi:.1f}ms로 2배 이상 흔들렸으므로" if hi >= 2 * lo else "회차를 더 늘려도 나아지지 않을 수 있으므로"
    return f"이미 {n}회를 쟀고 {why}, 유선이나 다른 네트워크에서 다시 재 보세요."


def report(cal, what_if, target=None):
    out = ['### RTT 예산', '']
    total_rtt = 2   # None이면 대역폭 제한 구간이 있어 합산하지 않는다
    per_run = {k: 2 for k in range(cal['runs'])}   # 회차별 총 RTT (handshake 2 포함). None = 이 회차는 합산에서 뺌

    def add_rounds(counts):
        """counts: 회차 번호 → 이 단계 라운드 수. 없는 회차(대역폭 제한 등)는 총합에서 뺀다"""
        for k in per_run:
            per_run[k] = None if per_run[k] is None or k not in counts else per_run[k] + counts[k]

    out.append('| 단계 | 세그먼트 | 라운드(RTT, 회차별 실측) | 시작 cwnd(실측) | 1 RTT 줄이려면 | 다음 RTT까지 여유 |')
    out.append('| --- | --- | --- | --- | --- | --- |')
    notes = []
    has_cut = False
    prev_end_cwnd = None
    for s in cal['stages']:
        i = s['index']
        if target and i > target:
            break
        if s['bandwidth_limited_runs'] and s['bandwidth_limited_runs'] >= len(s['observed_rounds']):
            out.append(f"| {i} | {s['segments_median']:.0f} | 대역폭 제한 | - | RTT로 계산 불가 | - |")
            notes.append(f"{i}단계: {s['bandwidth_limited_runs']}/{s['runs']}회가 대역폭 제한(라운드 전송률이 3라운드 연속 1.25배 이상 커지지 않음, BBR과 같은 판정) → 이 구간은 RTT 횟수가 아니라 대역폭이 시간을 결정합니다. 크기를 줄인 만큼 비례해서 빨라집니다.")
            total_rtt = None
            continue
        if s['bandwidth_limited_runs']:
            notes.append(f"{i}단계: {s['bandwidth_limited_runs']}/{s['runs']}회는 대역폭 제한이라 RTT 계산에서 뺐습니다(대역폭 제한 회차가 절반 이상이면 단계 전체를 대역폭 제한으로 봅니다). 아래 RTT는 나머지 {len(s['observed_rounds'])}회 기준입니다.")
        obs = s['observed_rounds']
        unstable = obs and max(obs) - min(obs) >= UNSTABLE_SPREAD
        rate = s['rate_like'] is not None and s['rate_like'] >= RATE_CORR
        # 1만 차이 나면 계산은 하되 RTT 수는 범위로 낸다(최빈값으로 고르면 거의 반반인 경우에도 한쪽을 버리게 된다)
        split = bool(obs) and max(obs) - min(obs) == 1 and not rate
        obs_mode = mode(obs)[0] if obs else None
        obs_txt = (f"{min(obs)}~{max(obs)}" if (unstable or rate or split) and min(obs) != max(obs) else f"{obs_mode}") + f" ({', '.join(map(str, obs))})" if obs else '-'
        if unstable or rate:
            add_rounds(s['run_rounds'])
            why = []
            if unstable:
                why.append(f"회차별 라운드 수가 {min(obs)}~{max(obs)}로 {UNSTABLE_SPREAD} 이상 차이 남")
            if rate:
                why.append(f"첫 라운드 크기가 회차별 RTT에 비례함(상관 {s['rate_like']}) → 서버가 일정한 속도로 보낸 데이터를 RTT 길이로 잘라 셌을 가능성")
            out.append(f"| {i} | {s['segments_median']:.0f} | {obs_txt} | - | 판정 불가 | - |")
            notes.append(f"{i}단계: RTT 수를 하나로 말할 수 없습니다. " + '; '.join(why) + ". 크기 비교는 유효하고, RTT는 범위로만 봅니다. " + remeasure_advice(cal))
            continue
        start = s['start_cwnd'] or s.get('cwnd_lower_bound') or prev_end_cwnd
        # 슬로 스타트에서 cwnd는 라운드마다 2배(RFC 5681, BBR STARTUP도 라운드당 2배). 측정한 라운드 크기 비율은
        # cwnd가 허용한 양이 아니라 실제로 보낸 양이라(페이싱 꼬리, 마지막 부분 라운드) 2보다 낮게 나오고,
        # 그 값으로 예측하면 같은 서버의 더 큰 페이지를 RTT 1회 더 많게 틀린다. 그래서 이론값 2를 쓴다.
        growth = SLOW_START_GROWTH
        segs = s['segments_median']
        if i in what_if:
            segs = math.ceil(what_if[i] * OVERHEAD / cal['payload'])
        if start is None:
            add_rounds(s['run_rounds'])
            out.append(f"| {i} | {segs:.0f} | {obs_txt} | 측정 불가 | - | - |")
            if obs_mode == 1:
                notes.append(f"{i}단계: 한 라운드에 끝나 시작 cwnd를 알 수 없습니다. 더 줄여도 RTT는 줄지 않습니다.")
            continue
        p = plan(segs, start, growth)
        add_rounds({k: p['rounds'] for k in per_run} if i in what_if else s['run_rounds'])
        cut = f"권장 −{kb((p['cut_segments'] + MARGIN_SEGMENTS) * cal['payload'] / OVERHEAD)} (최소 −{kb(p['cut_segments'] * cal['payload'] / OVERHEAD)})" if p['cut_segments'] else '-'
        has_cut = has_cut or bool(p['cut_segments'])
        head = kb(p['headroom_segments'] * cal['payload'] / OVERHEAD)
        src = f"{start:.0f} ({len(s['start_cwnd_samples'])}회)" if s['start_cwnd'] else f"{start:.0f} (관측한 cwnd 하한)"
        label = f"{i}{' (가정)' if i in what_if else ''}{' (페이싱)' if s['paced_runs'] else ''}"
        out.append(f"| {label} | {segs:.0f} | {obs_txt if i not in what_if else str(p['rounds']) + ' (모델)'} | {src} | {cut} | {head} |")
        if split and i not in what_if:
            notes.append(split_note(i, s, p, cal['payload'], head))
            has_cut = True
        elif obs_mode and i not in what_if and p['rounds'] != obs_mode:
            notes.append(f"{i}단계: 실측 라운드는 {obs_mode}인데 실측 시작 cwnd·증가율로 계산하면 {p['rounds']}입니다. 모델이 이 단계를 정확히 재현하지 못하므로 '1 RTT 줄이려면'은 참고로만 보세요.")
        prev_end_cwnd = start + segs if growth >= 1.9 else None
        if s['paced_runs']:
            notes.append(f"{i}단계: {s['paced_runs']}/{s['runs']}회에서 서버가 고르게 나눠 보내(페이싱) 라운드 사이 공백이 없었습니다. RTT 창마다 세그먼트 수가 계속 늘어 슬로 스타트로 보고, 창 하나를 1 RTT로 셌습니다. 증가율은 창 크기로 추정한 값입니다.")
        if s['overlap_runs']:
            notes.append(f"{i}단계: {s['overlap_runs']}/{s['runs']}회에서 HTML을 다 받기 전에 요청이 나갔습니다(early flush). 서버가 이 요청을 받기 전에 보낸 HTML은 앞 단계로 셌고, 받은 뒤에 보낸 HTML이 있으면 이 단계와 cwnd를 나눠 써서 이 단계 전송량에 포함됩니다.")
    out.append('')
    upto = f'{target}단계까지' if target else '전체 단계'
    total = '대역폭 제한 구간이 있어 RTT로 합산 불가' if total_rtt is None else total_text(per_run)
    out.append(f"**{upto} 총 RTT (handshake 2 포함, 회차별 실측 라운드 기준): {total}**")

    out.append('')
    if has_cut:
        notes.insert(0, "'1 RTT 줄이려면'의 최소는 1 RTT가 줄어드는 경계까지 줄여야 하는 양이고, 권장은 압축률과 오버헤드 오차를 감안해 조금 더 줄인 양입니다(압축 후 전송 크기 기준).")
    out += [f'- {n}' for n in notes]
    out.append('')
    out += waits_table(cal, target)
    out.append('')
    out += ['### 측정 조건', '', '| 항목 | 값 |', '| --- | --- |',
            f"| 측정 횟수 | {cal['runs']}회 |",
            f"| 전송 중 RTT ({'TCP timestamp' if cal['rtt_source'] == 'tcp_timestamps' else 'handshake'}) | 중앙값 {cal['rtt_ms']:.1f}ms (회차별 {cal['rtt_range'][0]:.1f}~{cal['rtt_range'][1]:.1f}ms) |",
            *( [f"| handshake RTT | 회차별 {cal['handshake_rtt_range'][0]:.1f}~{cal['handshake_rtt_range'][1]:.1f}ms" +
                (" (2배 이상 흔들림: 와이파이 등 측정 환경 불안정)" if cal['handshake_rtt_range'][1] >= 2 * cal['handshake_rtt_range'][0] else '') + ' |']
               if cal.get('handshake_rtt_range') else []),
            f"| 세그먼트당 데이터 | {cal['payload']}B ({cal['payload_agree']}/{cal['runs']}회 일치) |",
            f"| 서버 TLS 응답 | {cal['tls_segments']}세그먼트 ({cal['tls_agree']}/{cal['runs']}회 일치) |",
            f"| 초기 cwnd 하한 (관측, 같은 호스트 회차 공유) | {', '.join(f'{h} {v}' for h, v in cal.get('shared_init_cwnd', {}).items())} |"]
    out.append('')
    out.append(f"<sub>크기는 압축 후 전송 크기 기준(오버헤드 {OVERHEAD}배 반영), 권장값은 오차를 감안해 최소값보다 조금 더 줄인 값. 측정한 네트워크·서버 기준이며, 대역폭이 좁거나 패킷 손실이 있는 사용자에게는 다르게 나타날 수 있습니다.</sub>")
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('analysis', nargs='+')
    ap.add_argument('--stage-bytes', action='append', default=[], help='단계=압축 후 전송 바이트 (예: 2=170300)')
    ap.add_argument('--json', help='보정값을 JSON으로 저장')
    ap.add_argument('--target-stage', type=int, help='이 단계까지의 RTT만 합산 (예: LCP 리소스가 받아지는 단계)')
    ap.add_argument('--init-cwnd-from', nargs='+', default=[], help='같은 서버의 다른 페이지 analysis.json. 초기 cwnd 하한에만 쓴다')
    a = ap.parse_args()
    runs, bad = load(a.analysis)
    if not runs:
        sys.exit('분석 가능한 측정이 없습니다: ' + '; '.join(bad))
    extra, _ = load(a.init_cwnd_from) if a.init_cwnd_from else ([], [])
    shared_iw = share_init_cwnd(runs, extra)
    cal = calibrate(runs)
    cal['shared_init_cwnd'] = shared_iw
    if a.json:
        json.dump(cal, open(a.json, 'w'), ensure_ascii=False, indent=2)
    what_if = {int(k): int(v) for k, v in (x.split('=') for x in a.stage_bytes)}
    print(report(cal, what_if, a.target_stage))
    if bad:
        print('\n> 분석 실패한 측정: ' + '; '.join(bad))


if __name__ == '__main__':
    main()
