# Cua Driver 포함 배포 안내

사용자가 Driver를 따로 찾는 단계를 줄이기 위해, 이번 `v0.7.1` 릴리스에는 **Computer Use MCP와 Cua Driver를 함께 압축한 파일**을 추가합니다. 기존 Driver 별도 준비용 ZIP도 유지합니다.

## 무엇이 들어 있나요?

| 구분 | 내용 |
| --- | --- |
| MCP 본체 | 검증한 Computer Use MCP 0.7.1 배포본 |
| 추가 Driver | Cua Driver 0.28.2 Windows x86_64 공식 ZIP의 6개 파일 전체. 실행파일은 `driver/cua-driver.exe` |
| 출처·파일 정보 | `DRIVER-BUNDLE-MANIFEST.json` |
| Driver 라이선스 | 배포 폴더의 `CUA-DRIVER-LICENSE.md`와 `driver/LICENSE.md` |

이미 이 PC에서 시험한 **0.28.2 실행파일을 사용하며 Driver 버전을 새로 올린 배포가 아닙니다.** 공식 ZIP에 함께 들어 있는 UIA·도우미 파일도 모두 보존합니다. Driver는 MCP 본체와 별도의 외부 구성요소입니다. 출처·파일 해시·버전 등 정확한 정보는 [DRIVER-BUNDLE-MANIFEST.json](DRIVER-BUNDLE-MANIFEST.json)을 확인하세요.

[Driver 포함 ZIP 다운로드](https://github.com/bongG-moon/computeruse_mcp/releases/download/v0.7.1/Computer-Use-MCP-0.7.1-with-driver.zip) · [Driver 별도 준비용 ZIP](https://github.com/bongG-moon/computeruse_mcp/releases/download/v0.7.1/Computer-Use-MCP-0.7.1.zip)

## 출처와 라이선스

Cua 프로젝트의 공개 소스는 [trycua/cua](https://github.com/trycua/cua)에 있습니다. 동봉 Driver의 상위 프로젝트 라이선스는 MIT이며, `cua-driver-rs-v0.28.2`에 고정한 원본 출처와 저작권 고지를 [CUA-DRIVER-LICENSE.md](CUA-DRIVER-LICENSE.md)에 보존합니다. [릴리스의 같은 라이선스 파일](https://github.com/bongG-moon/computeruse_mcp/releases/download/v0.7.1/CUA-DRIVER-LICENSE.md)도 함께 제공합니다.

이 Driver 라이선스 안내는 MCP 본체의 라이선스를 새로 지정한다는 뜻이 아닙니다. 파일 해시 확인도 회사의 반입·사용 승인이나 모든 프로그램의 동작 보장을 대신하지 않습니다.

## 파일을 확인하고 실행하기

1. 포함 ZIP을 모두 압축 풉니다.
2. **Computer Use MCP 설정.exe → 1 연결 준비 → Driver 파일 선택**에서 `driver/cua-driver.exe`를 고릅니다.
3. **연결 확인 (화면 조작 없음)**으로 Driver와 MCP 기능 연결을 확인합니다.
4. 사용할 프로그램을 등록하고 클라이언트에 연결합니다. 상세 순서는 [README.md](README.md)와 [INSTALL.md](INSTALL.md)를 확인하세요.

[릴리스 SHA256SUMS.txt](https://github.com/bongG-moon/computeruse_mcp/releases/download/v0.7.1/SHA256SUMS.txt)에는 내려받을 릴리스 파일의 SHA-256 해시가 있습니다. 동봉 Driver 자체의 해시와 묶음 구성은 `DRIVER-BUNDLE-MANIFEST.json`으로 확인할 수 있습니다. 해시가 같은지는 파일 일치 여부를 보여주며, 실제 업무의 성공 여부는 별도로 시험해야 합니다.

## 포함판을 만드는 방식

[bundle_driver.py](bundle_driver.py)는 검증된 기존 MCP 배포 ZIP에 지정한 로컬 Driver ZIP의 6개 파일 전체와 출처·라이선스 문서를 추가해 포함판을 만듭니다. `--base-zip`, `--driver-zip`, `--license-file`, `--output`으로 기존 파일을 지정하며 Driver를 자동 다운로드하지 않습니다.

기존 MCP 본체 파일은 원래 빌드의 파일 목록과 해시를 기준으로 확인합니다. 새 문서와 Driver 포함판 구성 정보는 배포 포장에 추가된 파일입니다. GitHub에 추가된 안내 문서 때문에 기존 소스 ZIP의 원래 검증 결과까지 새로 확장되었다고 설명하지 않습니다.

Driver 실행파일은 Git 커밋에 넣지 않고 **릴리스 자산으로만** 제공합니다. 개인 설정, `.data` 실행 기록, 실제 화면·업무 데이터는 공개 배포에 포함하지 않습니다.

## 시험 후 Driver 포함 배포를 제외하려면

Driver 포함 ZIP과 별도 `cua-driver.exe` 릴리스 자산을 제외하고, Driver 별도 준비용 ZIP을 계속 제공하는 방식으로 바꿀 수 있습니다. 이미 다운로드한 복사본이 자동 삭제되는 것은 아닙니다.

현재 PC에서 동봉 Driver 사용을 중단하려면 사용할 다른 Driver를 먼저 준비하고, 설정 창에서 새 실행파일 경로를 선택해 연결을 확인한 뒤 설정을 저장합니다. MCP 연결을 다시 연 후 기존 동봉 Driver 파일을 정리할 수 있습니다. 실행 중인 Driver나 현재 설정이 가리키는 파일을 먼저 지우지 마세요.
