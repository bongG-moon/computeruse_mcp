# Computer Use MCP 0.6.0

Claude Code 등 MCP를 지원하는 클라이언트에서 **이 PC의 프로그램 화면을 읽고 조작하는 Windows용 도구**입니다. Chrome, 메모장, 설치된 Excel뿐 아니라 사용자가 실행파일을 지정한 다른 프로그램도 등록할 수 있습니다. 실제 화면을 확인해 클릭·입력하고, 요청한 결과까지 확인하는 것을 목표로 합니다.

## 다운로드

처음 사용하는 분은 **Driver 포함 ZIP**을 받으세요. Python을 따로 설치할 필요가 없습니다.

| 파일 | 용도 |
| --- | --- |
| [Computer-Use-MCP-0.6.0-with-driver.zip](https://github.com/bongG-moon/computeruse_mcp/releases/download/v0.6.0/Computer-Use-MCP-0.6.0-with-driver.zip) | MCP와 Cua Driver 0.28.2를 함께 준비하는 권장 묶음 |
| [Computer-Use-MCP-0.6.0.zip](https://github.com/bongG-moon/computeruse_mcp/releases/download/v0.6.0/Computer-Use-MCP-0.6.0.zip) | Driver를 직접 준비하는 기존 배포본 |
| [Computer-Use-MCP-0.6.0-source.zip](https://github.com/bongG-moon/computeruse_mcp/releases/download/v0.6.0/Computer-Use-MCP-0.6.0-source.zip) | 개발·코드 확인용 원본 소스 |
| [cua-driver.exe](https://github.com/bongG-moon/computeruse_mcp/releases/download/v0.6.0/cua-driver.exe) | 실행파일만 받기. 전체 구성은 Driver 포함 ZIP 사용 |

[전체 릴리스와 파일 해시](https://github.com/bongG-moon/computeruse_mcp/releases/tag/v0.6.0) · [SHA256SUMS.txt](https://github.com/bongG-moon/computeruse_mcp/releases/download/v0.6.0/SHA256SUMS.txt)

GitHub의 **Code → Download ZIP**은 소스 다운로드입니다. 설정 실행파일과 동봉 Python이 필요한 일반 사용자는 위의 배포 ZIP을 받으세요. 지원 대상은 Windows x86_64이며, 사용할 프로그램과 Claude Code 등 MCP 클라이언트는 이 PC에 설치되어 있어야 합니다.

## 처음 연결하기

1. 권장 ZIP을 내려받아 **유지할 폴더에 모두 압축 풉니다.** ZIP 안에서 바로 실행하지 마세요. 연결 후에는 이 폴더를 옮기거나 삭제하지 않습니다.
2. 압축 푼 `Computer-Use-MCP` 폴더에서 **Computer Use MCP 설정.exe**를 두 번 누릅니다.
3. **1 연결 준비 → Driver 파일 선택**에서 동봉된 `driver\cua-driver.exe`를 선택합니다. 이미 올바른 경로가 표시되어 있으면 그대로 사용합니다. **연결 확인 (화면 조작 없음)**을 눌러 확인합니다.
4. **2 사용할 프로그램**에서 쓸 프로그램을 확인합니다. 다른 앱은 **프로그램 추가 → 파일 선택**으로 실제 `.exe`를 고릅니다. 위치를 모르면 앱을 먼저 열고 **열린 창에서 선택**으로 고를 수 있습니다. 원하는 이름을 적고 **저장**합니다.
5. **1 연결 준비 → 저장하고 Claude Code에 연결**을 누릅니다. 이 버튼은 현재 사용자 전체의 Claude Code 연결을 등록합니다. 특정 작업 폴더에서만 쓰려면 아래의 채팅 설치 방법을 사용하세요.
6. Claude Code를 다시 열고 `/mcp`에서 `local-computer-use` 연결을 확인합니다. 먼저 새 시험 문서로 작은 작업을 요청하세요.

> 메모장의 새 시험 문서에 ‘자동화 연습 123’을 입력해줘. 다시 읽은 전체 내용이 정확히 같은지 확인한 뒤 멈춰줘.

설정 창은 연결 후 닫아도 됩니다. MCP가 필요할 때 동봉 Driver를 실행합니다. 별도의 Driver 창을 계속 열어 둘 필요는 없습니다. 새 설정의 기본 승인 방식은 이 MCP 자체의 시작·조작 팝업을 생략하며, Claude Code의 승인 여부는 Claude Code 설정에 따릅니다.

### Claude Code에 설치를 부탁하기

압축 푼 폴더의 [INSTALL.md](INSTALL.md)를 Claude Code에 첨부하고 아래처럼 요청할 수도 있습니다. 사용자에게 PowerShell 명령이나 JSON 편집을 요구하지 않는 설치 안내입니다.

> 이 안내대로 화면 작업 기능을 연결해줘. Driver는 이 배포 폴더의 driver/cua-driver.exe야. 메모장과 내가 지정하는 프로그램을 사용하고, 이 작업 폴더에서만 쓰고 싶어. 없는 정보만 물어봐줘. 아직 화면을 읽거나 조작하지 마.

기존 연결에 다른 앱을 추가하는 방법도 `INSTALL.md`와 사용 안내에 있습니다. 새 앱을 등록했다고 그 앱의 모든 화면을 자동으로 읽거나 조작할 수 있는 것은 아닙니다.

## 화면을 읽는 방식과 결과 확인

- **버튼·글자 정보(UIA)**: Windows가 제공하는 버튼 이름, 입력칸, 선택 상태 등을 읽습니다. 처음에는 이 방식으로 확인합니다.
- **화면 이미지(visual)**: 화면 캡처를 클라이언트에 전달하고 연결된 모델이 위치를 판단합니다. 클라이언트와 모델이 이미지 입력을 지원해야 합니다.

이 MCP에는 LLM이 들어 있지 않습니다. 모델은 연결한 클라이언트의 설정을 사용합니다. 사내 LLM까지 연결한 상태는 이번 검증 범위에 포함되지 않았습니다. 화면 내용·이미지는 연결한 클라이언트와 모델로 전달될 수 있으므로, 실제 외부 통신 여부는 그 연결 구성을 확인해야 합니다.

**클릭이나 입력이 전달됐다는 응답만으로 업무 완료를 판단하지 않습니다.** 선택값·입력 내용·저장 파일 등 필요한 결과를 다시 확인해야 합니다. UIA를 제공하지 않는 화면, 높은 권한의 앱, 사용자 정의 화면 등은 제약이 있으며 이미지 방식도 모든 앱의 성공을 보장하지 않습니다.

## 작업 후 종료까지 확인하기

0.6.0은 종료 요청 전에 원래 창·프로세스를 확인하고, 종료 후 상태를 다시 읽습니다. 저장·종료 확인창이 남으면 완료로 처리하지 않습니다. 숨김·최소화와 실제 종료를 구분하고, **특정 창이 닫혔는지**와 **원래 프로세스가 종료됐는지**도 구분합니다.

> 작업이 끝나면 지정한 시험 파일에 저장하고 프로그램을 종료해줘. 저장·종료 확인창도 처리하고 실제 종료됐는지 확인해줘.

저장 또는 버리기 방침이 불명확하면 임의로 선택하지 않습니다. 저장 내용 확인은 종료 확인과 별도이며, 저장이 목적이면 파일을 다시 열어 내용까지 확인해야 합니다. `computer_end`는 화면 작업 세션을 끝내는 기능이고 앱 종료 확인을 대신하지 않습니다.

## 검증 범위와 안내

0.6.0 자동 테스트는 **471개 중 469개 통과, 2개 제외**입니다. 시험용 Windows 앱에서 저장·버리기·취소·직접 종료·숨김·최소화·여러 창을 다룬 6가지 종료 시나리오를 소스 실행과 배포본 실행에서 모두 통과했습니다. 개별 업무 프로그램의 성공이나 무인 운영까지 검증한 결과는 아닙니다.

공개용 소스를 별도 폴더에서 다시 검사한 결과는 **471개 중 467개 통과, 4개 제외**입니다. 공개 소스에는 개발 PC의 실측 자료와 이전 배포 ZIP을 넣지 않아, 해당 자료를 요구하는 검사도 제외됩니다. MCP 본체 코드는 검증된 원본과 일치합니다.

최근 실제 Excel 시험에서는 요청한 전체 값 중 일부만 입력되는 문제가 남아 있습니다. 업무에 쓰기 전에 전체 셀 값과 저장 파일을 직접 대조해야 합니다. 자세한 결과와 이전 시험 기록은 아래 보고서에 있습니다.

- [처음 사용하는 분을 위한 사용 안내 — README.html](README.html)
- [Claude용 설치·프로그램 추가 안내 — INSTALL.md](INSTALL.md)
- [버전별 검증 결과와 남은 한계 — VALIDATION.html](VALIDATION.html)
- [Driver 포함 배포의 출처·라이선스·제거 방법 — DRIVER-BUNDLE.md](DRIVER-BUNDLE.md)

GitHub에서는 HTML이 소스로 보일 수 있습니다. **배포 ZIP을 압축 푼 뒤 README.html 또는 VALIDATION.html을 두 번 누르면 브라우저에서 안내와 보고서를 읽을 수 있습니다.**

Driver는 Git 소스에 넣지 않고 릴리스 파일로 제공합니다. 포함판과 별도 준비판을 함께 유지하며, 실제 환경 시험 후 Driver 포함 배포를 제외할 수 있습니다. 기존 0.6.0 MCP 본체는 그대로 제공됩니다.
