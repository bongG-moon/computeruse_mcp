---
name: agent-desktop-local
description: Windows 업무 앱의 메뉴, 콤보, 표와 속성 편집기를 agent-desktop으로 읽기 전용 관찰한다. 지정한 창과 명시한 부모 및 별도 팝업의 접근성 근거를 로컬에서 확인할 때 사용한다.
---

# Agent Desktop 로컬 인식

이 패키지는 Windows x64용 agent-desktop 1.1.0과 별도 관찰 래퍼다. 기존 CUA 설치, MCP 연결, 녹화 및 보안 설정은 변경하지 않는다. Python 3.10 이상과 현재 로그인한 Windows 세션이 필요하다. CUA는 필수 의존성이 아니다.

사용자가 설치나 첫 실행 방법을 요청하면 [사용가이드.html](사용가이드.html)을 안내한다. CUA Driver는 필요 없고 `bin/agent-desktop.exe`는 Windows용 ZIP에 포함된다. Python은 별도 필요하다. 기존 CUA v0.19.5 ZIP과 이 스킬의 배포 ZIP을 구분한다.

## 데이터 경계

- `scripts/observe.py`만 실행한다. 포함된 원본 EXE의 다른 명령이나 내장 스킬로 우회하지 않는다. 래퍼는 `list-windows`와 `snapshot`만 구성하며 바이너리 해시를 확인한다.
- JEV, 외부 모델 호출, 다운로드, 업데이트 확인, 원격 로그 전송을 실행하지 않는다. API 키나 서버 주소를 받지 않는다. 화면 캡처, 클립보드 읽기, 입력, 펼치기, 스크롤, 포커스 변경도 수행하지 않는다.
- 표 값, 창 제목, 요소 이름, 원시 트리와 ref 상태는 지정한 로컬 결과 폴더에 저장한다. stdout은 상태와 개수, 임의 영수증 ID만 반환한다. 결과 폴더 전체가 업무 자료다.
- **내용 해석은 회사에서 승인한 사내 모델 환경에서만** 로컬 JSON을 읽어 진행한다. 외부 모델 대화에 원시 JSON, 스크린샷, 경로, 창 제목을 첨부하지 않는다. 호스트의 네트워크 전송 여부는 이 래퍼가 통제하지 못한다.
- 로컬 결과 폴더는 동기화되지 않는 회사 관리 디스크를 사용한다. UNC, 네트워크 드라이브와 reparse 경로는 거부하지만 백업/클라우드 동기화 여부까지 판별하지 못한다. 방화벽, UAC, 백신 정책을 바꾸지 않는다. 실행이 차단되면 중단한다.

## 실행

스킬 폴더에서 아래 명령을 실행한다. `APP.exe`, PID, HWND와 경로는 사용자가 지정한 실제 대상에 맞춘다. 대상 실행파일 이름은 와일드카드 없이 정확히 지정한다. 결과 폴더는 먼저 로컬에 준비한다.

```powershell
python -X utf8 -B scripts/observe.py inventory --exe-name APP.exe --output C:\LocalReview
python -X utf8 -B scripts/observe.py observe --exe-name APP.exe --pid 1234 --hwnd 5678 --stage combo --output C:\LocalReview
```

`inventory`의 로컬 결과에서 현재 PID와 HWND를 확인한다. 앱을 실행하거나 창을 복원하지 않는다. 사용자가 직접 필요한 화면을 열어 둔다. 여러 업무 창이 같은 PID여도 하나를 임의 선택하지 않는다.

읽으려는 팝업이 목록에 없거나 트리가 얇으면 확인된 부모 HWND를 `--parent-hwnd 9012`로 지정해 별도 관찰한다. 독립 업무 창이나 검색 창은 `--related-hwnd 3456`으로 명시한다. 동일 PID만으로 업무 관계가 증명되지는 않는다. 관찰 결과는 창과 surface별로 유지하며 섞어서 하나의 표나 메뉴로 만들지 않는다.

`--stage`는 `window`, `menu`, `combo`, `grid`, `property` 중 하나이며 해석의 종류를 지정한다. 모두 지정한 HWND의 window surface만 읽는다. 원본의 menu surface는 PID 전체에서 메뉴를 찾으므로 사용하지 않는다. 같은 PID에 다른 업무 창이 있을 때 범위가 넓어질 수 있기 때문이다. 이미 열린 메뉴가 별도 HWND라면 확인 후 `--related-hwnd`로 명시한다. 목록에 없는 팝업은 미관찰로 남긴다. HWND가 사라지거나 PID, 경로, 프로세스 생성 시점이 달라지면 재사용하지 않는다.

## 해석

FX/FxMenuAgent 계열처럼 WPF와 WinForms가 섞인 앱은 [관찰 전략](references/recognition.md)을 먼저 읽는다. 정적 REA에서 얻은 구조는 탐색 힌트이며 실행 중의 AutomationId, 패턴 지원 또는 성공 증거가 아니다.

로컬 `recognition` 자료에서 관찰된 트리 경로와 역할, 이름, 표시값 및 선택 상태를 확인한다. 빈 트리나 오류는 성공으로 보지 않는다. 노드 수가 많다는 이유로 인식률이 좋아졌다고 결론 내리지 않는다. 중복 이름, 잘린 트리, 패턴 정보 미노출은 미확인으로 남긴다. 기존 별도 접근성 진단 결과를 사용하더라도 출처와 범위를 구분한다.

이 스킬은 업무 동작이나 저장, 조회 완료, Excel 생성 성공을 판정하는 실행기가 아니다. 실제 회사 FX/FxMenuAgent와 물리 DPI별 인수 시험은 별도로 필요하다.
