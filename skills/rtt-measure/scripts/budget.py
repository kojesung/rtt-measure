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


def report(cal, what_if, target=None):
    out = ['### RTT 예산', '']
    total_rtt, total_hi = 2, 2
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
        obs_mode = mode(obs)[0] if obs else None
        obs_txt = (f"{min(obs)}~{max(obs)}" if (unstable or rate) and min(obs) != max(obs) else f"{obs_mode}") + f" ({', '.join(map(str, obs))})" if obs else '-'
        if total_rtt is not None:
            if unstable or rate:
                total_rtt += min(obs); total_hi += max(obs)
            elif obs_mode:
                total_rtt += obs_mode; total_hi += obs_mode
        if unstable or rate:
            why = []
            if unstable:
                why.append(f"회차별 라운드 수가 {min(obs)}~{max(obs)}로 {UNSTABLE_SPREAD} 이상 차이 남")
            if rate:
                why.append(f"첫 라운드 크기가 회차별 RTT에 비례함(상관 {s['rate_like']}) → 서버가 일정한 속도로 보낸 데이터를 RTT 길이로 잘라 셌을 가능성")
            out.append(f"| {i} | {s['segments_median']:.0f} | {obs_txt} | - | 판정 불가 | - |")
            notes.append(f"{i}단계: RTT 수를 하나로 말할 수 없습니다. " + '; '.join(why) + ". 크기 비교는 유효하고, RTT는 범위로만 봅니다. 유선 연결이나 회차를 늘려(예: 9회) 다시 재 보세요.")
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
            out.append(f"| {i} | {segs:.0f} | {obs_txt} | 측정 불가 | - | - |")
            if obs_mode == 1:
                notes.append(f"{i}단계: 한 라운드에 끝나 시작 cwnd를 알 수 없습니다. 더 줄여도 RTT는 줄지 않습니다.")
            continue
        p = plan(segs, start, growth)
        cut = f"권장 −{kb((p['cut_segments'] + MARGIN_SEGMENTS) * cal['payload'] / OVERHEAD)} (최소 −{kb(p['cut_segments'] * cal['payload'] / OVERHEAD)})" if p['cut_segments'] else '-'
        has_cut = has_cut or bool(p['cut_segments'])
        head = kb(p['headroom_segments'] * cal['payload'] / OVERHEAD)
        src = f"{start:.0f} ({len(s['start_cwnd_samples'])}회)" if s['start_cwnd'] else f"{start:.0f} (관측한 cwnd 하한)"
        label = f"{i}{' (가정)' if i in what_if else ''}{' (페이싱)' if s['paced_runs'] else ''}"
        out.append(f"| {label} | {segs:.0f} | {obs_txt if i not in what_if else str(p['rounds']) + ' (모델)'} | {src} | {cut} | {head} |")
        if obs_mode and i not in what_if and p['rounds'] != obs_mode:
            notes.append(f"{i}단계: 실측 라운드는 {obs_mode}인데 실측 시작 cwnd·증가율로 계산하면 {p['rounds']}입니다. 모델이 이 단계를 정확히 재현하지 못하므로 '1 RTT 줄이려면'은 참고로만 보세요.")
        prev_end_cwnd = start + segs if growth >= 1.9 else None
        if s['paced_runs']:
            notes.append(f"{i}단계: {s['paced_runs']}/{s['runs']}회에서 서버가 고르게 나눠 보내(페이싱) 라운드 사이 공백이 없었습니다. RTT 창마다 세그먼트 수가 계속 늘어 슬로 스타트로 보고, 창 하나를 1 RTT로 셌습니다. 증가율은 창 크기로 추정한 값입니다.")
        if s['overlap_runs']:
            notes.append(f"{i}단계: {s['overlap_runs']}/{s['runs']}회에서 HTML을 다 받기 전에 요청이 나갔습니다(early flush). 서버가 이 요청을 받기 전에 보낸 HTML은 앞 단계로 셌고, 받은 뒤에 보낸 HTML이 있으면 이 단계와 cwnd를 나눠 써서 이 단계 전송량에 포함됩니다.")
    out.append('')
    upto = f'{target}단계까지' if target else '전체 단계'
    total = '대역폭 제한 구간이 있어 RTT로 합산 불가' if total_rtt is None else (f'{total_rtt}~{total_hi}' if total_hi != total_rtt else f'{total_rtt}')
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
