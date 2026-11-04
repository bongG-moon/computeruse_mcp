# Agent Desktop Local 0.2.0

Claude Code에서 Windows 업무 창을 읽고, 요청한 클릭과 값 변경을 실행한 뒤 결과를 다시 확인하는 별도 스킬입니다. 기존 읽기 전용 관찰 기능은 유지하며 **0.2.0부터 조작 명령을 추가**했습니다. 원본 agent-desktop 엔진은 1.1.0으로 고정합니다. 기존 Computer Use MCP 0.19.5와 설정을 교체하지 않습니다.

**실제 사용자는 [Windows x64 ZIP](https://github.com/bongG-moon/computeruse_mcp/releases/download/agent-desktop-local-v0.2.0/Agent-Desktop-Local-0.2.0-windows-x64.zip)을 받고, 압축을 푼 뒤 [사용가이드.html](사용가이드.html)을 여세요.** `source.zip`과 GitHub의 `Source code`는 개발용입니다.

## 무엇이 포함되나요?

| 구성 요소 | 필요 여부 |
|---|---|
| CUA Driver (`cua-driver.exe`) | 필요 없음 |
| `bin/agent-desktop.exe` | Windows ZIP에 포함된 실행 엔진 |
| Python 3.10 이상 | 별도 필요. 회사에서 승인한 기존 런타임 사용 |
| 로컬 파일 읽기와 명령 실행 도구 | 필요. 사내 모델을 사용하는 Claude Code 등 |
| JEV, npm, 별도 API 키, 외부 모델 | 이 스킬에는 필요 없음 |
| 관리자 권한 설치, 서비스, 커널 드라이버 | 이 스킬이 설치하지 않음 |

Windows 10 1809 이상 x64의 현재 로그인한 사용자 세션에서 실행합니다. 채팅 전용 모델에 ZIP을 첨부하는 것만으로 PC 조작 도구가 생기지는 않습니다. 포함된 EXE가 실행하고, 사내 모델은 로컬 결과를 읽어 다음 동작을 결정합니다.

## Claude Code에서 시작하기

1. ZIP 전체를 동기화되지 않는 로컬 폴더에 풉니다. EXE만 따로 옮기지 않습니다.
2. Claude Code를 사용하는 프로젝트의 `.claude/skills/agent-desktop-local/`에 **폴더 내용 전체**를 복사합니다. 최종 경로는 `.claude/skills/agent-desktop-local/SKILL.md`여야 합니다. 기존 CUA 스킬과 개인 Claude 설정은 덮어쓰지 않습니다.
3. 새 Claude Code 대화에서 `/agent-desktop-local`을 호출하거나 아래처럼 실제 `SKILL.md` 경로와 작업을 알려 줍니다. 프로젝트에 설치하지 않고 경로로 읽게 해도 됩니다.

> 이 경로의 Agent Desktop Local 0.2.0 SKILL.md를 읽어 줘. 열어 둔 업무 창에서 MES Setup 콤보를 N으로 변경하고, 현재 선택값을 다시 읽어서 확인해 줘. 같은 이름의 창이나 항목이 있으면 먼저 구분해 줘. 저장은 이번 요청에 포함하지 않을게. 결과는 로컬 results 폴더에 보관해 줘.

위 문장은 예시입니다. 실제로 저장까지 원하면 요청에 포함하세요. 이미 요청한 조작마다 별도 재확인을 요구하도록 만들지 않았습니다. Claude Code의 기존 도구 권한과 회사 정책은 그대로 적용됩니다. 모델 주소, 인증, MCP, hooks, 전역 권한은 변경하지 않습니다.

스킬 설치 방식은 [Claude Code 공식 문서](https://code.claude.com/docs/en/skills)를 따릅니다. 이전 0.1.x와 같은 이름의 스킬이 다른 위치에 있으면 사용 중인 `SKILL.md`의 전체 경로와 첫 제목의 버전을 확인하세요. 기존 개인 스킬을 자동 삭제하지 말고 새 대화에서 새 경로를 명시합니다.

## 두 실행 경로

| 목적 | 진입점 | 동작 |
|---|---|---|
| 읽기와 비교만 요청 | `scripts/observe.py` | 창 목록과 지정한 창의 트리 관찰. 입력 없음 |
| 조작까지 요청 | `scripts/interact.py` | 현재 창을 inspect한 뒤 ref로 한 번 조작하고 새 관찰 저장 |

조작 명령과 결과 해석은 [references/actions.md](references/actions.md)에 있습니다. 인식 범위는 [references/recognition.md](references/recognition.md)를 참고하세요. 기본 조작은 접근성 API를 사용하고, 지원되는 물리 입력과 포커스 변경은 `--headed`로 명시합니다. 임의 좌표 입력, 일반 셸 명령 전달, 클립보드 명령은 제공하지 않습니다.

업무 화면이 노출하지 않는 컨트롤을 복원하지는 못합니다. 예를 들어 콤보의 표시 문자열과 내부 코드가 다르면 각각 읽힌 근거로 판단합니다. “값이 바뀜”과 “업무 저장 완료”도 구분합니다. 전달 여부가 불확실한 입력을 자동 재시도하지 않습니다.

## 외부 전송 범위

래퍼는 네트워크 클라이언트와 외부 서비스 연동을 포함하지 않습니다. JEV, npm 설치 스크립트, 업데이트 확인과 원격 로그 전송을 배포하지 않습니다. 업무 트리, 값, 조작 내용과 원시 응답은 로컬 파일에 남고 stdout은 제한된 요약을 반환합니다. 사내 모델이 해당 파일을 읽어 해석합니다. 결과 폴더 전체는 업무 자료이며 외부 모델이나 공개 이슈에 첨부하지 마세요.

OS 네트워크 차단이나 악성 사용자 방어용 샌드박스는 아닙니다. 원본 EXE에는 래퍼가 제공하지 않는 명령도 있으므로 스킬의 Python 진입점을 사용합니다. 호스트의 모델 전송 정책과 결과 폴더의 클라우드 동기화는 별도 관리 대상입니다. UAC, 백신과 방화벽 설정은 변경하지 않습니다. 원본 바이너리는 Authenticode 서명이 없으며 회사 정책이 차단하면 우회하지 않습니다.

agent-desktop은 자체 ref 상태와 Windows의 사용자별 interaction lock을 만들 수 있습니다. 모든 흔적이 지정한 결과 폴더 안에만 생긴다고 보장하지 않습니다. 다른 사용자 세션, 잠긴 화면과 UAC 보안 데스크톱은 대상이 아닙니다. 원본의 PID 전체 메뉴 탐색도 사용하지 않습니다.

## 배포와 검증 범위

이 스킬은 [agent-desktop-local-v0.2.0](https://github.com/bongG-moon/computeruse_mcp/releases/tag/agent-desktop-local-v0.2.0)이라는 별도 릴리스입니다. 기존 CUA `v0.19.5` ZIP에 포함되지 않습니다. 기존 폴더를 덮어쓰기보다 새 버전을 별도 폴더에 풀고 새 경로를 사용하세요.

로컬 합성 화면과 코드 검증의 범위는 [VALIDATION.md](VALIDATION.md)에 기록합니다. 실제 회사 FX/FxMenuAgent는 다른 PC에 있어 현장 인식률, 실제 업무 결과와 DPI별 동작은 검증하지 않았습니다. 명령 전달 성공만으로 이를 검증했다고 표현하지 않습니다.

원본: [vercel-labs/agent-desktop](https://github.com/vercel-labs/agent-desktop), [v1.1.0](https://github.com/vercel-labs/agent-desktop/releases/tag/v1.1.0). 원본 EXE는 수정하지 않았습니다. `UPSTREAM.json`, `LICENSE`, `NOTICE.txt`, `THIRD_PARTY_NOTICES.txt`를 함께 확인하세요. 고지문에는 원본 Cargo.lock의 다른 플랫폼과 개발 의존성도 포함돼 있습니다.
