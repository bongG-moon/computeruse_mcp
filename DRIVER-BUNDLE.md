# Cua Driver 포함판 안내 — 0.16.0 배포 후보

Driver 포함판은 Computer Use MCP와 필요한 Cua Driver 파일을 함께 묶어, 사용자가 Driver를 따로 찾는 단계를 줄이는 배포 형식입니다. 이 문서는 **0.16.0 배포 후보의 구성과 사용 방법**을 설명합니다. 게시 완료 여부와 실제 내려받을 파일은 [GitHub Releases](https://github.com/bongG-moon/computeruse_mcp/releases), 검증한 작업은 [검증 기록](VALIDATION.html)에서 확인하세요.

## 파일 구성

| 구분 | 내용 |
| --- | --- |
| MCP 본체 | Computer Use MCP 0.16.0, 포함된 Python 런타임과 화면 도우미 |
| Driver | Cua Driver 0.28.2 Windows x86_64 공식 ZIP의 6개 파일 전체 |
| 실행파일 | driver/cua-driver.exe |
| 출처·파일 정보 | Driver 포함 ZIP에 추가되는 DRIVER-BUNDLE-MANIFEST.json |
| 라이선스 | CUA-DRIVER-LICENSE.md 및 포함판의 driver/LICENSE.md |
| 해시 | 원래 MCP의 SHA256SUMS.txt와 포함판 전체의 BUNDLE-SHA256SUMS.txt |

Driver 버전은 0.28.2로 고정합니다. UIA·보조 실행파일·SDK 등 원본 ZIP에 함께 들어 있는 파일을 보존하며, MCP와 별도의 외부 구성요소입니다. Driver 없는 기본 ZIP에는 driver 폴더와 DRIVER-BUNDLE-MANIFEST.json이 없습니다.

## 처음 사용하는 순서

1. 받은 ZIP **전체**를 계속 사용할 폴더에 압축 풉니다.
2. [INSTALL.md](INSTALL.md)를 채팅 앱에 첨부하고 “기존 설정을 유지하면서 이 안내대로 화면 작업 기능을 연결해줘”라고 요청합니다.
3. 연결 도구는 포함된 driver/cua-driver.exe를 확인하고 사용합니다. 파일을 사용자가 매번 먼저 실행할 필요는 없습니다.
4. 사용할 프로그램을 채팅으로 등록합니다. 메모장·계산기·Microsoft Store·Chrome은 실제 설치 정보를 확인합니다.
5. **말로 작업을 부탁하거나, 프로세스 편집창에서 직접 동작을 녹화하고 검토·시험·저장**합니다.

이미 연결했다면 기존 Driver·프로그램·저장 작업을 유지하면서 새 폴더로 연결을 갱신합니다. MCP를 다시 연결해 버전을 확인하세요. UAC 확인이 필요할 수 있으며 Windows 보안 정책을 바꾸지 않습니다. 처음부터 따라 하는 순서는 [PROCESS_GUIDE.html](PROCESS_GUIDE.html)에 있습니다.

## 출처와 라이선스

Cua 프로젝트의 공개 소스는 [trycua/cua](https://github.com/trycua/cua)에 있습니다. 고정된 Driver 버전은 [cua-driver-rs-v0.28.2](https://github.com/trycua/cua/releases/tag/cua-driver-rs-v0.28.2)입니다. 상위 프로젝트 MIT 라이선스 원문과 저작권 고지는 [CUA-DRIVER-LICENSE.md](CUA-DRIVER-LICENSE.md)에 보존합니다.

이 Driver 라이선스 안내는 MCP 본체의 라이선스를 새로 지정한다는 뜻이 아닙니다. 해시는 파일 일치 여부를 확인하는 자료이며 회사 반입·사용 승인이나 모든 앱의 실행 성공을 대신하지 않습니다.

## 포함판을 만드는 방식

소스 ZIP의 bundle_driver.py가 기존 MCP 배포 ZIP, 수동으로 준비한 공식 Driver ZIP, 고정 라이선스를 검증한 뒤 포함판을 만듭니다. --base-zip, --driver-zip, --license-file, --output으로 로컬 파일을 지정합니다. 네트워크 다운로드나 프로그램 실행은 하지 않습니다.

MCP 본체의 파일과 원래 해시 목록은 그대로 보존합니다. 추가 Driver 및 라이선스·포장 정보는 별도 전체 해시 목록으로 확인합니다. 문서와 실행파일이 서로 다른 버전이면 묶음을 만들지 않습니다.

Driver 실행파일은 Git 소스에 넣지 않고 릴리스 자산으로 제공합니다. 개인 설정, .data 실행 기록, 녹화 결과, 실제 화면과 업무 데이터는 공개 ZIP에 포함하지 않습니다.

## 나중에 Driver 포함판을 제외할 때

Driver 포함 ZIP과 별도 Driver 릴리스 자산을 제외하고 기본 ZIP만 제공할 수 있습니다. 이미 다운로드한 복사본은 자동 삭제되지 않습니다.

이 PC에서 다른 Driver를 사용하려면 대체 파일을 먼저 준비하고 해당 파일로 연결을 갱신·검증한 뒤 기존 동봉 파일을 정리합니다. 실행 중이거나 설정이 가리키는 Driver를 먼저 지우지 마세요.
