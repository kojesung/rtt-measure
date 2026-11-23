# rtt-measure

> 번들을 1바이트 줄이면 로딩이 빨라질까요?

### 🍪 [이 스킬을 쓰면 뭐가 좋은가? →](docs/why-it-helps.md)

과자 봉지 예시로 3분 만에 설명합니다.

대부분은 빨라지지 않습니다. 서버는 파일을 한 번에 보내지 않고, 왕복(RTT) 한 번에 보낼 수 있는 양만큼씩 나눠 보냅니다. 그래서 로딩 시간은 바이트 수에 비례해서 줄지 않고, **왕복 횟수가 한 번 줄어드는 경계를 넘을 때** 계단처럼 줄어듭니다.

`rtt-measure`는 배포된 페이지의 첫 로딩을 패킷 단위로 실측해서 그 경계가 어디인지 알려 주는 에이전트 스킬입니다.

- 지금 이 페이지는 HTML과 리소스를 받는 데 **몇 RTT**가 걸리는가
- **몇 kB를 줄여야** 1 RTT가 줄어드는가
- **몇 kB까지는 늘어도** RTT가 그대로인가

Claude Code에서 만들었지만 [Agent Skills](https://agentskills.io) 형식이라 Cursor, Codex 등 스킬을 지원하는 다른 에이전트에서도 쓸 수 있습니다. 스크립트만 직접 실행해도 됩니다.

## 결과 예시

실제 서비스 한 곳을 7회 측정한 결과입니다(호스트만 가렸습니다).

| 단계 | 세그먼트 | 라운드(RTT, 회차별 실측) | 시작 cwnd(실측) | 1 RTT 줄이려면 | 다음 RTT까지 여유 |
| --- | --- | --- | --- | --- | --- |
| 1 (HTML) | 5 | 1 (1, 1, 1, 1, 1, 1, 1) | 31 (관측한 cwnd 하한) | - | 31.7kB |
| 2 (JS·CSS 등) | 182 | 3~4 (4, 3, 3, 4, 3, 3, 3) | 34 (7회) | 권장 −100.0kB (최소 −97.6kB) | 68.3kB |

이 표가 말해 주는 것은 세 가지입니다.

- 2단계 리소스(압축 후 약 225kB)는 7회 중 5회 **3 RTT**, 2회 4 RTT에 도착했습니다. 3라운드 한도까지 68kB가 남아 있어 크기가 경계에 걸친 것은 아니고, 4 RTT 회차는 그 회차의 전송이 느렸던 것입니다.
- 번들을 10kB, 50kB 줄여도 RTT는 그대로입니다. **약 100kB를 줄여야** 2 RTT가 됩니다.
- 반대로 **68.3kB까지는 늘어도** 3 RTT 그대로입니다.

표의 각 열과 나머지 섹션(RTT가 아닌 대기, 측정 조건)을 읽는 방법은 [결과 읽는 법](docs/reading-results.md)에 정리했습니다. RTT, TCP, TLS를 몰라도 읽을 수 있게 썼습니다.

## 왜 RTT로 봐야 하나

TCP 서버는 연결 직후 조금만 보내고, 받았다는 응답(ACK)이 돌아올 때마다 한 번에 보내는 양을 약 2배씩 늘립니다(슬로 스타트). 교과서 값(세그먼트 1,460B, 처음에 10개)으로 계산하면 이렇습니다.

| 왕복 | 이번 왕복에 보내는 양 | 여기까지 누적 |
| --- | --- | --- |
| 1번째 | 10세그먼트, 약 14.6kB | 14.6kB |
| 2번째 | 20세그먼트, 약 29.2kB | 43.8kB |
| 3번째 | 40세그먼트, 약 58.4kB | 102.2kB |
| 4번째 | 80세그먼트, 약 116.8kB | 219.0kB |

100kB 파일과 50kB 파일은 둘 다 3번째 왕복에 끝납니다. 50kB를 줄였는데 도착 시각은 같습니다. 반면 105kB를 100kB로 줄이면 5kB만으로 왕복 한 번이 사라집니다. 번들을 줄이는 일은 결국 **왕복 횟수를 줄이는 일**이고, 얼마나 줄여야 의미가 있는지는 경계가 어디인지 알아야 정할 수 있습니다.

문제는 그 경계가 서버마다 다르다는 점입니다. 위 표는 교과서 값일 뿐입니다.

- 세그먼트당 데이터는 1,460B가 아닌 경우가 많습니다(예: MSS 1250 − TCP timestamp 12 = 1,238B).
- 처음에 보내는 양(초기 cwnd)도 10개가 아닙니다. CDN은 20~30개 이상인 경우가 흔합니다.
- TLS 응답, 앞 단계에서 이미 늘어난 cwnd, CDN MISS, 서버 대기가 섞입니다.

브라우저 개발자 도구에는 이 값들이 보이지 않습니다. 패킷을 캡처해서 직접 세야 하는데 손으로 하기에는 번거롭습니다. 이 스킬이 캡처부터 계산까지 대신 합니다.

## 이럴 때 씁니다

**최적화의 근거가 필요할 때**
"왜 번들 크기를 줄여야 한다고 판단했나요?"라는 질문에 "크면 느리니까요" 대신 "2단계 리소스가 3 RTT인데, 98kB를 줄이면 2 RTT가 되는 것을 패킷으로 확인했습니다"라고 답할 수 있습니다. 회고, PR 설명, 면접에서 그대로 쓸 수 있는 근거입니다.

**트레이드오프의 기준이 필요할 때**
번들을 줄이려고 코드를 복잡하게 만들거나 도구를 새로 들일지, 반대로 코드를 깔끔하게 하려고 라이브러리를 추가할지 고민될 때 숫자로 정할 수 있습니다. "이 라이브러리는 압축 후 12kB이고 여유가 68kB라서 RTT가 늘지 않는다. 넣는다." 또는 "40kB를 줄여도 RTT가 그대로라서 이 최적화는 하지 않는다."

**최적화 효과를 확인할 때**
preview나 스테이징 주소를 같은 방법으로 재서, 실제로 왕복이 줄었는지 머지 전에 확인합니다.

**이론 계산 도구를 보정할 때**
빌드 결과물로 RTT를 계산하는 도구를 쓴다면, 기본 가정(MSS 1,460, 초기 cwnd 10) 대신 실측값을 넣을 수 있습니다.

## 설치

**여러 에이전트 공통** ([skills CLI](https://github.com/vercel-labs/skills))

```bash
npx skills add kojesung/rtt-measure
```

이 방법만 Node.js(`npx`)가 필요합니다. 설치하는 데만 쓰고, 측정은 Python으로 합니다. Node.js가 없으면 아래 두 방법을 쓰세요.

**Claude Code 플러그인**

```
/plugin marketplace add kojesung/rtt-measure
/plugin install rtt-measure@rtt-measure
```

**직접 복사**

```bash
git clone https://github.com/kojesung/rtt-measure.git
cp -r rtt-measure/skills/rtt-measure ~/.claude/skills/     # 모든 프로젝트에서 사용
# 또는 프로젝트의 .claude/skills/ 아래에 복사
```

다른 에이전트는 `skills/rtt-measure` 폴더를 그 에이전트의 스킬 폴더에 복사합니다. 경로는 버전에 따라 바뀔 수 있으니 각 문서에서 확인하세요.

| 에이전트 | 모든 프로젝트 | 프로젝트 하나 |
| --- | --- | --- |
| Claude Code | `~/.claude/skills/` | `.claude/skills/` |
| Codex | `~/.agents/skills/` | `.agents/skills/` |
| Cursor | `~/.cursor/skills/` | `.cursor/skills/` (`.claude/skills/`도 읽음) |

복사한 뒤 에이전트를 다시 시작하면 스킬 목록에 뜹니다.

## 사용

에이전트에게 주소와 함께 요청하면 됩니다.

```
/rtt-measure https://example.com/ 첫 로딩이 몇 RTT인지 재 줘
```

```
https://example.com/ 과 /search 페이지에서 몇 kB 줄이면 RTT가 줄어드는지 측정해 줘
```

Claude Code 플러그인으로 설치했다면 슬래시 명령에 플러그인 이름이 앞에 붙어 `/rtt-measure:rtt-measure`가 됩니다. `/rtt`까지 입력하면 자동완성에 뜹니다. 슬래시 명령 없이 두 번째 예시처럼 말로 요청해도 에이전트가 스킬을 불러옵니다.

에이전트는 다음 순서로 진행합니다.

1. 임시 폴더에 Python 가상 환경과 Chromium을 설치합니다(프로젝트에는 설치하지 않습니다).
2. 회차마다 새 브라우저(캐시 없음, 새 연결, HTTP/2)로 페이지를 열면서 패킷을 캡처합니다.
3. 캡처에서 MSS, 시작 cwnd, 라운드별 세그먼트 수, CDN MISS와 서버 대기를 뽑습니다.
4. 회차끼리 비교해 믿을 수 있는 회차만 남기고 RTT 예산을 계산합니다.
5. 단계별 결과, RTT 예산, RTT가 아닌 대기, 측정 조건을 표로 보고합니다.

측정 결과는 에이전트를 실행한 폴더(보통 작업 중인 프로젝트)의 `rtt-runs/`에 남습니다. 캡처 파일(`.pcap`)에는 접속한 호스트와 IP가 들어 있으니, git 저장소 안이라면 `.gitignore`에 `rtt-runs/`를 추가해 커밋되지 않게 하세요. 끝나면 임시 환경(수백 MB)을 지울지 에이전트가 묻습니다. 지우면 다음 측정 때 Chromium을 다시 받습니다.

### 필요한 것

- macOS 또는 Linux, Python 3
- Linux는 시스템 패키지가 더 필요할 수 있습니다. Debian/Ubuntu의 `python3-venv`, Chromium이 쓰는 시스템 라이브러리(`playwright install-deps`)입니다. 없으면 에이전트가 설치 명령을 알려 주거나, 데스크톱 환경이면 비밀번호 창(`pkexec`)으로 설치합니다.
- `tcpdump` (macOS는 기본 포함. Linux에서 없으면 에이전트가 설치합니다.)

Python 패키지(dpkt, playwright)와 Chromium은 미리 설치하지 않아도 됩니다. 처음 측정할 때 에이전트가 임시 폴더에 설치합니다.
- 패킷 캡처 권한. 권한이 없으면 에이전트가 아래 중 하나를 고르게 합니다. 어느 쪽이든 비밀번호는 OS 창이나 본인 터미널에 직접 입력하고, 에이전트는 비밀번호를 보거나 대신 입력하지 않습니다.
  1. **이번 부팅 동안 자동 캡처 (macOS 권장)**: macOS 비밀번호 창이 한 번 뜹니다. 재부팅 전까지는 재측정해도 다시 묻지 않고, 재부팅하면 원래대로 돌아갑니다.
  2. **직접 캡처**: 에이전트가 알려 주는 `sudo tcpdump ...`를 새 터미널 창에서 켜 두고, 측정이 끝나면 Ctrl+C로 끕니다.
  3. **영구 설정**: macOS는 Wireshark(ChmodBPF 포함) 또는 `brew install --cask wireshark-chmodbpf`, Linux는 tcpdump에 `setcap`으로 권한을 줍니다. 한 번 설정하면 다시 묻지 않습니다.
- 배포된 HTTPS 주소. 운영, preview, 스테이징 모두 됩니다. `localhost`는 RTT가 0에 가깝고 MSS가 달라서 받지 않습니다.

## 측정 범위와 한계

- 서버가 보낸 HTML 원문에 명시된 같은 호스트의 리소스까지 잽니다. stylesheet, script, modulepreload, preload, `loading=lazy`가 아닌 `<img>`가 해당됩니다. JS 실행 뒤에 요청하는 리소스, API, 다른 호스트는 범위 밖입니다.
- 측정한 사람의 네트워크에서 서버 하나를 잰 값입니다. RTT 횟수는 서버의 cwnd와 MSS가 주로 정해서 ms보다 일반화하기 쉽지만, 대역폭이 좁은 사용자는 더 일찍 대역폭 제한에 걸립니다.
- 실행할 때마다 네트워크가 달라 결과가 흔들리므로 CI의 PR 비교용으로는 맞지 않습니다. 실제 사용자 전체의 로딩 시간은 RUM(web-vitals, CrUX)으로 봐야 합니다.
- 회차별 라운드 수가 크게 다르거나 대역폭 제한에 걸린 경우에는 RTT 수를 단정하지 않고 범위나 "판정 불가"로 보고합니다.
- QUIC을 끄고 HTTP/2(TCP)로 측정합니다.

## 어떻게 세나

판정 규칙은 경험적으로 맞춘 숫자 대신 TCP 동작의 정의를 씁니다. 자세한 설명은 [SKILL.md](skills/rtt-measure/SKILL.md)의 "해석 참고"에 있습니다.

- **라운드 구분**: 시간 간격이 아니라 TCP timestamp의 ACK 인과관계로 나눕니다(BBR이 라운드를 세는 방식과 같습니다).
- **cwnd가 나눈 라운드만 셈**: 합쳐도 "초기 cwnd 하한 + ACK된 세그먼트" 안에 들어가는 라운드는 페이싱이나 서버 대기로 쪼개진 것이므로 합칩니다.
- **대역폭 제한 판정**: 전송률이 1.25배 이상 커지지 않는 라운드가 3번 연속이면 대역폭 제한으로 봅니다(Linux `tcp_bbr.c`가 STARTUP을 끝내는 기준과 같은 값).
- **제외**: 재전송이 있는 회차, CDN MISS와 서버 대기 시간은 RTT 계산에서 뺍니다.

손으로 Wireshark를 보고 센 값과 비교한 기록은 [docs/verification.md](docs/verification.md)에 있습니다.

## 스크립트만 쓰기

에이전트 없이도 실행할 수 있습니다.

```bash
python3 -m venv .venv && .venv/bin/pip install dpkt playwright
.venv/bin/python -m playwright install chromium
cd skills/rtt-measure

../../.venv/bin/python scripts/capture.py https://example.com/ --runs 5 --warmup --out rtt-runs
for f in rtt-runs/run-*.pcap; do
  ../../.venv/bin/python scripts/analyze.py "$f" --host example.com > "${f%.pcap}.analysis.json"
done
../../.venv/bin/python scripts/budget.py rtt-runs/run-*.analysis.json --target-stage 2
```

캡처 권한이 없을 때의 수동 캡처, 목표 단계를 정하는 방법, 크기를 바꿔 보는 `--stage-bytes`는 [SKILL.md](skills/rtt-measure/SKILL.md)를 참고하세요.

## 저장소 구조

```
skills/rtt-measure/
  SKILL.md            에이전트가 따르는 절차와 해석 규칙
  scripts/capture.py  새 브라우저로 페이지를 열고 패킷을 캡처
  scripts/analyze.py  캡처에서 RTT, MSS, 라운드, 대기를 추출
  scripts/budget.py   회차를 모아 RTT 예산 표를 계산
tests/                합성 캡처와 실측 라운드 값으로 판정 규칙 확인
docs/reading-results.md  결과 표 읽는 법
docs/why-it-helps.md  과자 봉지 예시로 보는 이 스킬의 쓸모 (그림: docs/images/)
docs/verification.md  검증 기록
.claude-plugin/       Claude Code 플러그인·마켓플레이스 설정
```

## 테스트

```bash
pip install dpkt
python tests/test_real_rounds.py                                   # 실측 라운드 값으로 판정 규칙 확인
python tests/test_budget.py                                        # 합성 회차로 예산 보고 규칙 확인
python tests/run_cases.py skills/rtt-measure/scripts/analyze.py    # 합성 캡처 6종의 판정 결과 출력
```

## 기여

오판 사례 제보를 환영합니다. 이슈에 `budget.py` 출력과 회차별 `round_segments`(`run-N.analysis.json`)를 붙여 주시면 재현 테스트로 추가하기 쉽습니다. pcap 파일에는 접속한 호스트와 IP가 들어 있으니 공개해도 되는 것만 올려 주세요.

## 라이선스

[MIT](LICENSE)
