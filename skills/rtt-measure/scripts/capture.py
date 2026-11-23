#!/usr/bin/env python3
"""배포된 페이지를 새 브라우저(캐시 없음, 새 연결, HTTP/2)로 여러 번 열면서 측정한다.

캡처 방식은 두 가지다.
  자동 캡처: sudo 없이 tcpdump를 쓸 수 있으면 회차마다 run-N.pcap을 직접 남긴다.
  수동 캡처(--no-capture): 사용자가 본인 터미널에서 sudo tcpdump를 켜 둔 상태에서 브라우저만 돌리고,
                          회차별 시작·끝 시각을 run-N.json에 남긴다. analyze.py가 이 시각으로 파일을 나눈다.

회차마다 run-N.json에는 HTML에 명시된 리소스 목록과 브라우저가 실제로 보낸 요청 묶음이 남는다.

사용:
  capture.py --check                                   # sudo 없이 캡처 가능한지 확인 (종료 코드 0이면 가능)
  capture.py https://example.com/ --runs 5 --warmup --out rtt-runs
  capture.py https://example.com/ --runs 5 --warmup --out rtt-runs --no-capture
"""
import argparse, json, os, platform, shutil, signal, subprocess, sys, time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

RESOURCES_SCRIPT = """() => performance.getEntriesByType('resource').map((r) => ({
  name: r.name, initiator: r.initiatorType, protocol: r.nextHopProtocol,
  requestStart: r.requestStart, encodedBodySize: r.encodedBodySize,
}))"""
NAV_SCRIPT = """() => { const n = performance.getEntriesByType('navigation')[0];
  return n && { protocol: n.nextHopProtocol, encodedBodySize: n.encodedBodySize }; }"""
LCP_URL_SCRIPT = """() => new Promise((resolve) => {
  let last = null;
  new PerformanceObserver((l) => { const e = l.getEntries(); last = e[e.length - 1]; })
    .observe({ type: 'largest-contentful-paint', buffered: true });
  setTimeout(() => resolve(last ? (last.url || null) : null), 300);
})"""

IGNORED_REL = {'prefetch', 'icon', 'shortcut', 'apple-touch-icon', 'manifest', 'preconnect', 'dns-prefetch', 'alternate', 'canonical', 'mask-icon'}


class DeclaredResources(HTMLParser):
    """서버가 보낸 HTML 원문에 명시된 하위 리소스 (JS가 나중에 넣는 것은 포함하지 않는다)"""
    def __init__(self, base):
        super().__init__(); self.base = base; self.urls = {}

    def add(self, url, kind):
        if url and not url.startswith('data:'):
            self.urls.setdefault(urljoin(self.base, url), kind)

    def handle_starttag(self, tag, attrs):
        a = {k: (v or '') for k, v in attrs}
        if tag == 'script' and a.get('src') and 'nomodule' not in a:
            self.add(a['src'], 'script')
        elif tag == 'link' and a.get('href'):
            rels = set(a.get('rel', '').lower().split())
            if rels & IGNORED_REL:
                return
            if 'stylesheet' in rels:
                self.add(a['href'], 'stylesheet')
            elif 'modulepreload' in rels:
                self.add(a['href'], 'modulepreload')
            elif 'preload' in rels:
                self.add(a['href'], f"preload({a.get('as', '')})")
        elif tag == 'img' and a.get('src') and a.get('loading', '').lower() != 'lazy':
            self.add(a['src'], 'img')


def default_iface():
    return 'en0' if platform.system() == 'Darwin' else 'any'


def tcpdump_cmd(iface, path):
    # 패킷 전체를 저장한다: SNI(ClientHello)를 읽어야 대상 연결을 찾을 수 있다
    return ['tcpdump', '-i', iface, '-U', '-w', path, 'tcp port 443']


def can_capture(iface):
    """sudo 없이 tcpdump를 열 수 있는지 1초 안에 확인"""
    if not shutil.which('tcpdump'):
        return False, 'tcpdump가 설치되어 있지 않습니다.'
    try:
        p = subprocess.run(['tcpdump', '-i', iface, '-c', '1', '-w', os.devnull, 'tcp port 443'],
                           capture_output=True, timeout=1.5)
        if p.returncode == 0:
            return True, 'ok'
        return False, p.stderr.decode(errors='replace').strip()
    except subprocess.TimeoutExpired:
        return True, 'ok'   # 권한이 있어 패킷을 기다리는 중이었다
    except Exception as e:
        return False, str(e)


def start_capture(iface, path):
    proc = subprocess.Popen(tcpdump_cmd(iface, path), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    time.sleep(1.0)
    if proc.poll() is not None:
        sys.exit('tcpdump를 시작하지 못했습니다: ' + proc.stderr.read().decode(errors='replace').strip() +
                 '\n수동 캡처(--no-capture)로 진행하세요.')
    return proc


def stop_capture(proc):
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def group_requests(entries, declared, gap_ms):
    """requestStart가 gap_ms 이상 벌어지면 다른 묶음 → pcap의 요청 단계와 순서·개수를 맞춰 보는 용도"""
    groups = []
    for e in sorted(entries, key=lambda x: x['requestStart']):
        if groups and e['requestStart'] - groups[-1]['last'] <= gap_ms:
            groups[-1]['items'].append(e); groups[-1]['last'] = e['requestStart']
        else:
            groups.append({'first': e['requestStart'], 'last': e['requestStart'], 'items': [e]})
    return [{'request_start_ms': round(g['first'], 1),
             'encoded_bytes': sum(i['encodedBodySize'] or 0 for i in g['items']),
             'resources': [{'name': i['name'], 'encodedBodySize': i['encodedBodySize'], 'declared_in_html': i['name'] in declared}
                           for i in g['items']]} for g in groups]


def visit(url, settle_ms):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        # 매 회차 새 브라우저: 캐시·연결 재사용 없음. QUIC을 꺼서 HTTP/2(TCP)로 연결되게 한다.
        browser = p.chromium.launch(args=['--disable-quic'])
        page = browser.new_context(viewport={'width': 1440, 'height': 900}).new_page()
        resp = page.goto(url, wait_until='load', timeout=60000)
        html = resp.text() if resp else ''
        page.wait_for_timeout(settle_ms)
        data = {'html': html, 'nav': page.evaluate(NAV_SCRIPT), 'resources': page.evaluate(RESOURCES_SCRIPT),
                'lcp_url': page.evaluate(LCP_URL_SCRIPT)}
        browser.close()
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('url', nargs='?')
    ap.add_argument('--check', action='store_true', help='sudo 없이 캡처 가능한지만 확인')
    ap.add_argument('--runs', type=int, default=5)
    ap.add_argument('--out', default='rtt-runs')
    ap.add_argument('--iface', default=default_iface())
    ap.add_argument('--no-capture', action='store_true', help='캡처는 사용자가 따로 켜 두고 브라우저만 돌린다')
    ap.add_argument('--settle-ms', type=int, default=1500, help='load 이후 남은 요청을 기다리는 시간')
    ap.add_argument('--warmup', action='store_true', help='CDN 캐시를 데우는 회차를 한 번 먼저 버린다')
    ap.add_argument('--group-gap-ms', type=float, default=10, help='requestStart가 이만큼 벌어지면 다른 요청 묶음')
    a = ap.parse_args()

    if a.check:
        # 수동 캡처 명령의 -w 경로가 이 폴더 안이다. 폴더가 없으면 tcpdump가 파일을 열지 못하고 바로 끝난다.
        os.makedirs(a.out, exist_ok=True)
        ok, why = can_capture(a.iface)
        print('자동 캡처 가능' if ok else f'자동 캡처 불가: {why}')
        print('수동 캡처 명령어(본인 터미널에서 실행): sudo ' + ' '.join(tcpdump_cmd(a.iface, os.path.abspath(os.path.join(a.out, 'capture.pcap')))))
        sys.exit(0 if ok else 1)
    if not a.url:
        ap.error('URL이 필요합니다')

    host = urlparse(a.url).hostname
    os.makedirs(a.out, exist_ok=True)
    if a.warmup:
        visit(a.url, 300)
    for n in range(1, a.runs + 1):
        pcap = os.path.join(a.out, f'run-{n}.pcap')
        proc = None if a.no_capture else start_capture(a.iface, pcap)
        started = time.time()
        try:
            data = visit(a.url, a.settle_ms)
        finally:
            time.sleep(0.5)
            ended = time.time()
            if proc:
                stop_capture(proc)
        parser = DeclaredResources(a.url); parser.feed(data['html'])
        declared = {u: k for u, k in parser.urls.items() if urlparse(u).hostname == host}
        other_host = sorted(u for u in parser.urls if urlparse(u).hostname != host)
        same_host = [r for r in data['resources'] if urlparse(r['name']).hostname == host]
        seen = {r['name'] for r in same_host}
        protocols = sorted({r['protocol'] for r in same_host if r['protocol']} | ({data['nav']['protocol']} if data['nav'] else set()))
        meta = {
            'url': a.url, 'host': host, 'protocols': protocols,
            'window': {'start': started, 'end': ended},          # 수동 캡처 파일을 회차별로 나누는 기준
            'pcap': None if a.no_capture else pcap,
            'html_encoded_bytes': data['nav'] and data['nav']['encodedBodySize'],
            'declared': [{'url': u, 'kind': k, 'requested': u in seen} for u, k in declared.items()],
            'declared_other_host': other_host,                    # 다른 호스트라 이 측정 범위 밖
            'lcp_in_scope': data['lcp_url'] in declared if data['lcp_url'] else None,
            'request_groups': group_requests(same_host, set(declared), a.group_gap_ms),
        }
        json.dump(meta, open(os.path.join(a.out, f'run-{n}.json'), 'w'), ensure_ascii=False, indent=2)
        print(f"[{n}/{a.runs}] protocol={protocols} 명시 리소스 {len(declared)}개")
        # HTTP/3(QUIC)은 TCP가 아니고, HTTP/1.1은 브라우저가 연결을 여러 개(보통 6개) 열어 리소스를 나눠 받으므로
        # 연결 하나의 라운드로 세는 분석이 맞지 않는다(rilog.kr 실측: 고른 연결이 HTML 다음 명시되지 않은 2MB 폰트만 실음).
        if any(p != 'h2' for p in protocols):
            print('  ! HTTP/2가 아닌 프로토콜이 섞였습니다. 이 회차는 분석에서 빼세요.')
        time.sleep(1.0)  # 회차 사이 간격: 수동 캡처 파일에서 회차가 겹치지 않게


if __name__ == '__main__':
    main()
