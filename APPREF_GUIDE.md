# 응용 프로그램 참조(.appref-ms)로 여는 프로그램

Windows에서 파일 형식이 **응용 프로그램 참조**로 표시되는 파일은 ClickOnce 프로그램의 실행 참조입니다. 실행파일(EXE)과 다르므로 참조 파일을 EXE 칸에 넣지 않습니다. 설치를 마친 MCP에서 채팅으로 추가할 수 있습니다.

1. 평소 사용하는 `.appref-ms` 파일의 경로를 알려주고 **“이 파일로 여는 프로그램을 추가해 줘”**라고 요청합니다.
2. MCP가 파일 이름과 실행 주소를 확인하고 열린 프로그램 목록을 보여줍니다. 실제 업무 창 하나를 선택합니다. 아직 열리지 않았다면 평소처럼 참조 파일을 한 번 열어 주세요.
3. **“등록한 프로그램 열어 줘”**라고 하면 이후부터 Windows가 그 참조 파일을 열고, MCP는 등록한 실제 프로그램의 창을 찾습니다. 녹화·요소 선택은 기존 프로세스 편집창에서 그대로 진행합니다.

같은 참조와 실제 프로그램이 이미 등록되어 있으면 다시 창을 선택하지 않아도 됩니다. 등록 시 실행하거나 배포 서버에 접속하지 않습니다. 프로그램을 열 때는 Windows ClickOnce가 해당 배포 주소에 접속하거나 업데이트를 확인할 수 있습니다. 이것은 사내 또는 로컬 배포 주소 설정을 따릅니다.

참조 파일이 바뀌면 자동 실행을 멈추고 다시 등록하도록 안내합니다. ClickOnce 업데이트로 실제 EXE 경로가 바뀐 경우에도 열린 업무 창을 다시 선택해야 합니다. 파일 이름만 같다는 이유로 새 프로그램에 화면 제어 권한을 주지 않습니다. Windows 설치·게시자 확인창이나 회사의 ClickOnce 정책을 우회하지 않습니다.

## 연결 도구를 만드는 개발자용

`computer_register_program`에 `launch_appref`를 전달합니다. 이것만 전달하면 `choose_program_window`와 후보, 참조 메타데이터가 반환됩니다. 실제 업무 창의 `candidate_id`를 선택해 `launch_appref`와 함께 다시 호출합니다. 이미 정확한 실제 EXE를 알고 있다면 `exe`와 함께 등록할 수도 있습니다.

```json
{
  "launch_appref": "C:\\Users\\사용자\\Desktop\\업무 프로그램.appref-ms",
  "candidate_id": "computer_program_candidates가 반환한 값",
  "name": "업무 프로그램"
}
```

`launch_uri`, EXE 인자, 시작 폴더와 함께 지정하지 않습니다. 저장 형식은 `launch: {kind: "appref", target: "로컬 참조 경로", sha256: "등록 시 해시"}`이며 `exe`는 실제 업무 프로그램의 경로입니다. `dfsvc.exe`, `rundll32.exe` 등을 허용 대상으로 추가하지 않습니다. CLI에는 `--launch-appref`가 있으며 기존 `--exe`가 별도로 필요합니다. 설정 화면에서도 기존 실행 주소 칸에 `.appref-ms` 경로를 입력할 수 있습니다.

파일 검사는 64KiB 이내의 로컬 일반 파일, 링크가 없는 경로, ClickOnce 배포 주소와 어셈블리 식별자, 변경 여부를 확인합니다. 참조 파일 해시는 배포 서버 내용이나 프로그램 자체의 신뢰를 보증하는 서명이 아닙니다. 실제 ClickOnce 설치 검증은 Windows가 수행합니다. HTTPS/HTTP 주소와 이 PC의 `file:///.../*.application` 배포를 지원합니다. 네트워크 공유의 참조 파일 및 UNC 배포 주소는 현재 지원하지 않습니다.

## 로컬 검증용 ClickOnce 앱

개발 환경의 `.NET Framework` 컴파일러와 Windows SDK `Mage.exe`가 있으면 다음 명령으로 별도의 로컬 시험 앱을 만들 수 있습니다. 일반 사용자는 이 작업이 필요 없습니다.

```powershell
.\tools\build_clickonce_fixture.ps1
```

기본 출력은 `.data\clickonce-fixture`입니다. 실제 WinForms 앱과 서명된 ClickOnce 앱·배포 매니페스트, 매니페스트의 실제 식별자로 만든 Unicode `.appref-ms`를 생성합니다. EXE 이름만 바꾼 모의 파일이 아닙니다. 테스트 서명 키는 해당 폴더에만 저장하고 인증서 저장소·보안 정책은 수정하지 않습니다. 빌드만으로 설치하거나 앱을 열지 않습니다.

빌드 과정은 서명뿐 아니라 Windows ClickOnce가 EXE에서 실제로 읽는 식별자, 매니페스트의 참조, 각 파일의 SHA256까지 대조합니다. C# 컴파일러가 자동 삽입하는 `MyApplication.app` Win32 매니페스트는 ClickOnce 참조와 충돌할 수 있어 이 시험 앱에서는 삽입하지 않습니다. 오프라인 검사 통과와 실제 설치·녹화·재생 성공은 별도로 확인합니다.

Windows에서 생성된 `.application`을 열어 정상 설치 흐름을 진행합니다. 시험 인증서는 신뢰 저장소에 넣지 않으므로 Windows의 게시자 확인이 나올 수 있습니다. 정책 때문에 차단되면 정책을 우회하지 말고 그 제약을 기록합니다. 정상 실행 시 창 상단에 `ClickOnce deployment: active`가 표시됩니다. `Family` 선택, `Search` 입력, `Query`, `Reset`, 결과 표와 완료 문구를 이용해 실제 녹화·저장·재생을 확인할 수 있습니다. 이 앱은 ClickOnce 실행 경로와 일반 컨트롤 검증용이며 특정 사내 프로그램의 컨트롤·권한·렌더링 특성을 재현하지는 않습니다.

### 이 PC에서 확인한 결과 — 2026-10-17

수정한 시험 앱의 실제 ClickOnce 설치와 `.appref-ms`를 통한 재실행을 확인했습니다. 사용자가 Windows의 설치 확인을 진행했으며, 인증서 저장소나 보안 정책은 변경하지 않았습니다.

| 확인 항목 | 결과 |
| --- | --- |
| 실제 ClickOnce 설치 | 완료. ClickOnce 캐시에 설치된 EXE의 창을 확인했습니다. |
| 앱을 닫은 뒤 `computer_open`으로 다시 열기 | 완료. 등록된 `.appref-ms` 실행 요청 뒤 새 창에 연결됐습니다. |
| 재실행한 창 관찰 | 완료. `computer_observe`로 Family 선택 상자 등 15개 컨트롤과 대상 창 이미지를 받았습니다. |
| 녹화 → 저장 → 재생 | 완료. 200% 배율에서도 요소 방식으로 조건 선택·검색어 입력·체크·조회를 녹화해 저장했습니다. 수동 보완·누락 경고 없이 6단계가 저장됐고, 초기화 후 재개 및 처음부터 재생이 각각 6/6단계로 완료됐습니다. 값은 UIA로, 결과 5행은 실제 화면을 읽어 확인했습니다. |

이미지 클릭 좌표를 확인할 수 없는 앱은 인식 가능한 요소만 자동 기록합니다. 사용자가 Windows 배율을 바꿀 필요는 없습니다. 이 시험에서 이미지 클릭까지 검증한 것은 아닙니다. 드롭다운과 체크박스의 실제 선택값 기록, 빈 문자열의 결과 확인도 수정했습니다. 자세한 시험 범위는 [검증 기록](VALIDATION.html#validation-0180)에 있습니다.

처음 설치가 실패한 원인은 시험 앱 제작 시 EXE 내부의 `MyApplication.app` 식별자와 ClickOnce 매니페스트의 참조가 달랐던 빌드 오류였습니다. 사용자의 PC 환경이나 설치 승인 문제로 확인된 것이 아닙니다. 충돌하는 기본 Win32 매니페스트를 제외하고 버전과 이름을 맞췄으며, 추가한 검증 도구로 이전 오류가 재현되고 수정본이 통과하는 것까지 확인한 뒤 실제 설치를 마쳤습니다.

실행·관찰 근거는 `.data/acceptance018-native-20261017/responses/057-appref-open.json`과 `058-appref-observe.json`, 녹화 저장은 `068-record-saved.json`, 재생 완료는 `072-appref-complete.json`과 `074-appref-fresh-complete.json`입니다. 빌드 오류 재현·수정 확인은 `.data/validation018-clickonce-fixture.json`에 있습니다. 이 결과는 로컬 시험 앱에 대한 확인이며 사내 프로그램의 녹화·재생 성공을 의미하지 않습니다.

참고: [Microsoft의 ShortcutAppId 설명](https://learn.microsoft.com/en-us/dotnet/api/system.deployment.application.downloadapplicationcompletedeventargs.shortcutappid?view=netframework-4.8.1), [ClickOnce 수동 배포 절차](https://learn.microsoft.com/en-us/visualstudio/deployment/walkthrough-manually-deploying-a-clickonce-application?view=vs-2022).
