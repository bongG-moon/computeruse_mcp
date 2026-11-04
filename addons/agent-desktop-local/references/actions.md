# 로컬 조작 방법

사용자의 클릭, 값 변경, 메뉴 실행 요청을 이 경로로 수행한다. 내부 모델은 로컬 결과를 읽고 명령을 구성하며, Python 래퍼가 포함된 `agent-desktop.exe`를 실행한다. 모델 주소나 API 키를 이 래퍼에 넘기지 않는다.

## 1. 현재 대상 확인

`observe.py inventory`의 로컬 `inventory.json`에서 실행파일, PID, HWND, 창 제목을 확인한다. 창과 업무 목적이 일치해야 한다. 이후 아래처럼 조작용 관찰을 만든다. 모든 명령은 스킬 폴더 기준이며 경로와 숫자는 예시다.

```powershell
python -X utf8 -B scripts/interact.py inspect --exe-name APP.exe --pid 1234 --hwnd 5678 --output C:\LocalReview
```

응답의 `receipt`가 새 세션 폴더 이름이다. 예를 들어 receipt가 `0123456789abcdef0123456789abcdef`라면 `C:\LocalReview\0123456789abcdef0123456789abcdef`를 사용한다. `inspection.json`과 `before.json`을 사내 모델에서 읽어 대상의 조상 경로, 역할, 현재 값과 qualified ref를 확인한다. 이름이 같으면 조상 경로와 역할로 구분한다. 라벨의 ref를 옆 콤보의 ref로 대신 쓰지 않는다.

창 하나가 세션 하나의 범위다. 팝업이 별도 창이면 창 목록에서 실제 관계를 확인한 후 그 HWND로 새 inspect를 만든다. 같은 PID의 다른 업무 창을 자동으로 포함하지 않는다. 지정한 창에 노출되지 않는 컨트롤은 임의 좌표로 조작하지 않는다.

## 2. 해당 세션으로 한 번 실행

```powershell
python -X utf8 -B scripts/interact.py act --session C:\LocalReview\0123456789abcdef0123456789abcdef --ref "@s8f3k2p9:e12" --action set-value --value "N"
```

ref는 형식 설명용 예시다. **반드시 방금 생성한 세션의 실제 ref를 사용한다.** `observe.py`의 다른 폴더나 과거 액션 결과에서 가져온 ref는 사용하지 않는다. 세션은 120초 안에 사용하며 한 번 act를 시도하면 소비된다. 실패하거나 결과가 불분명해도 같은 세션을 다시 실행하지 않는다. 새 inspect로 현재 상태부터 확인한다.

위 예시는 현재 콤보가 값 쓰기를 노출한 경우다. `inspection.json`의 `available_actions`와 원시 Provider 근거를 확인해 명령을 고른다. 콤보라는 이유로 항상 `select`를 선택하지 않는다. 실제 합성 Windows 콤보에서도 `select`는 미지원이고 `set-value`는 동작한 사례가 있다. 미지원 응답 뒤에도 펼침 같은 선행 효과가 있었을 수 있으므로 결과와 현재 화면부터 읽는다.

예를 들어 `available_actions`의 `SetValue`는 CLI의 `set-value`, `Select`는 `select`에 대응한다. 같은 이름의 라벨, 콤보와 내부 텍스트 노드가 함께 있을 수 있고 내부 텍스트에도 ref가 붙을 수 있다. 이름이나 ref 유무만으로 고르지 말고 `combobox` 역할, 조상 경로와 현재 값을 확인해 실제 컨트롤을 선택한다. 화면에 보이지 않는 내부 텍스트 노드를 대신 조작하지 않는다.

| 명령 | 필수 추가 인수 | 의미와 사용 조건 |
|---|---|---|
| `click` | 없음 | 현재 ref의 기본 동작. 버튼 호출이나 메뉴 실행 |
| `set-value` | `--value "내용"` | 접근성 값 쓰기. 편집기의 전체 값 변경 |
| `select` | `--value "항목"` | 콤보나 선택 컨트롤이 제공한 항목 선택 |
| `expand`, `collapse` | 없음 | 펼치기와 접기. 메뉴/콤보의 Provider 지원 필요 |
| `check`, `uncheck` | 없음 | 체크 상태를 지정. 임의 토글 횟수로 계산하지 않음 |
| `clear` | 없음 | 편집기의 내용 비우기 |
| `scroll` | 선택: `--direction down --amount 3` | 해당 컨트롤 범위 스크롤. 방향은 up/down/left/right |
| `double-click`, `right-click` | `--headed` | 명시한 ref에 물리 입력. 포커스/마우스에 영향 |
| `type` | `--value "내용" --headed` | 현재 삽입 위치에 입력. 전체 내용 교체는 set-value 우선 |
| `focus` | `--headed` | 해당 요소로 포커스 이동 |

표는 래퍼가 받는 명령 목록이다. 실제 대상이 그 명령을 지원한다는 뜻은 아니다. 선택할 코드와 표시 이름이 다르면 현재 관찰된 항목을 확인한다. 다열 콤보의 N이라는 표시가 저장 코드 N을 뜻한다고 추정하지 않는다. 표 셀은 먼저 업무 행 키, 열과 편집기를 확인한다. row 역할만으로 임의 셀 편집을 시도하지 않는다.

기본 모드는 접근성 API를 사용한다. 지원 명령에 `--headed`를 붙이면 물리 입력과 포커스 변경을 허용하므로, 에이전트가 조작하는 동안 같은 PC에서 마우스와 키보드를 사용하지 않는다. 실행 중 다른 앱으로 대상을 바꾸지 않는다. 물리 입력이 필요하다는 현재 근거가 있을 때만 사용하며 실패를 이유로 자동 전환하지 않는다.

Windows의 `type`은 대상 편집기에 실제 포커스가 있어야 한다. 전체 값을 바꾸려면 `set-value`를 우선한다. 삽입 입력을 요청했고 현재 대상이 포커스를 갖지 않았다면, 요청 범위의 준비 동작으로 `focus --headed`를 한 번 수행한 뒤 새 inspect를 만들고 같은 편집기를 다시 식별해 `type --headed`를 실행한다. 포커스 실패나 전환을 감지하면 입력을 반복하지 않는다. 두 조작에 같은 세션을 사용하지 않는다.

```powershell
python -X utf8 -B scripts/interact.py act --session C:\LocalReview\0123456789abcdef0123456789abcdef --ref "@s8f3k2p9:e12" --action right-click --headed
```

이 예시는 앞 명령과 연속 실행하는 절차가 아니다. 각 act에는 새 inspect에서 얻은 별도 세션과 ref가 필요하다. 임의 좌표, 일반 키 조합, 클립보드, 창 실행, shell 명령 전달은 제공하지 않는다.

## 3. 결과 확인과 다음 행동

세션 안에 조작 결과 `action.json`, 새 관찰 `after.json`, 정리된 `verification.json`이 남는다. 오류나 중단 시 일부 파일이 없을 수 있다. `inspection-receipt.json`은 조작 전 관찰, `dispatch-receipt.json`은 엔진 응답 직후, `action-receipt.json`은 사후 관찰까지 처리한 요약이다. 필요한 원시 자료는 회사에서 승인한 사내 모델 환경에서 읽는다. `consumed.json`과 `dispatch-started.json`은 실행 흔적이며 지우거나 수정해 재실행하지 않는다.

사후 트리가 값을 생략했지만 동일 컨트롤이 유일하게 확인되면 래퍼가 그 새 ref의 값을 별도로 읽어 `after-value.json`에 남길 수 있다. 이 추가 관찰과 `verification.json`을 함께 확인한다. 빈 문자열도 실제로 읽은 값이어야 하며, 값 필드가 없다는 이유로 비었다고 판단하지 않는다.

inspect의 `ready`는 조작용 관찰이 준비됐다는 뜻이다. act의 `completed`는 새 관찰에서 대상 컨트롤의 값이나 상태가 일치했다는 뜻이며, 업무 저장 성공을 의미하지 않는다. `executed_unverified`는 전달됐으나 요청한 상태를 자동 검증하지 못한 경우이고, `verification_failed`는 읽힌 값이나 상태가 기대와 다르다는 뜻이다. `uncertain`, `interrupted`와 조작 뒤의 오류도 미전달로 간주하지 않는다. 클릭만 성공한 경우에는 보통 `executed_unverified`이며 에이전트가 업무 결과를 별도로 확인한다.

`input_dispatched`는 `false`, `true`, `null`을 구분한다. `null`은 입력이 없었다는 뜻이 아니라 전달 여부를 확정하지 못했다는 뜻이다. `delivery`의 의미는 다음과 같다.

| delivery | 판단 |
|---|---|
| `not_delivered` | 엔진이 전달하지 않았다고 보고함. 대상과 원인을 다시 확인 |
| `delivered_verified` | 엔진의 해당 요소 검증이 맞았음. 요청한 업무 결과는 새 관찰로 별도 확인 |
| `delivered_unverified` | 전달됐으나 결과를 충분히 검증하지 못함. 재입력하지 않고 관찰 |
| `possibly_dispatched` | 타임아웃, 중단 등으로 전달 여부 불확실. 재입력하지 않고 관찰 |

`set-value` 뒤 같은 필드가 요청 값인지, `select` 뒤 콤보의 선택값이 맞는지 확인한다. 체크와 펼침은 현재 상태를 확인한다. 버튼은 호출 성공만으로 업무 성공을 주장하지 말고 사용자가 요청한 결과 표식, 새 조회 결과나 생성 파일을 확인한다. `after.json`이 없거나 원하는 요소가 사라지면 미확인으로 보고한다.

사용자가 “MES Setup을 N으로 변경”이라고 했다면 새 관찰에서 선택값 N을 확인해 보고한다. 저장까지 요청하지 않았다면 저장 버튼으로 범위를 넓히지 않는다. 저장을 요청했다면 그 동작을 수행하고 별도 완료 근거를 확인한다. 매번 기계적으로 재승인을 받는 절차는 없다.

사후 트리에 선택/체크 해제 상태가 노출되지 않는 경우 해제됐다고 추정하지 않는다. `action.json`의 엔진 `post_state` 등 다른 현재 근거가 있다면 출처를 밝혀 해석하며, 충분한 근거가 없으면 미확인으로 남긴다. 모든 조작 요약의 `business_commit`은 `not_verified`다.

다음 동작은 새 inspect부터 시작한다. 열린 메뉴, 새 팝업, 동적 편집기, 정렬된 표는 ref가 바뀔 수 있다. 이미 전달된 입력을 반복해서 상태를 맞추려 하지 않는다. 저장 성공이나 데이터베이스 반영은 래퍼의 자동 판정 범위가 아니다.

## 중단과 경계

Ctrl+C, 실행 타임아웃 또는 사후 관찰 실패 뒤에도 동작이 이미 전달됐을 수 있다. 다음 요청은 상태 확인부터 한다. 작업을 방해한 예외 문자열이나 원시 응답을 외부에 붙여 넣지 않는다. 경로, 업무 텍스트와 조작 값이 포함된 세션 폴더 전체를 로컬 업무 자료로 취급한다.

조작은 사용자가 요청한 범위에서만 수행한다. 대상이 모호하거나 요청 밖의 업무 변경이 필요하면 그 부분만 묻는다. 기존 Claude Code 도구 승인과 회사 정책을 우회하거나 관리자 권한을 자동 요청하지 않는다. 이 래퍼는 OS 보안 샌드박스가 아니다.
