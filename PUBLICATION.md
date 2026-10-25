# Computer Use MCP 배포 명세 생성과 검증

이 문서는 배포 담당자용입니다. 일반 사용자는 [INSTALL.md](INSTALL.md)로 연결합니다.

## 0.19.0 검증과 배포 기준

0.19.0은 로컬 녹화 편집·입력값 분리·스킬 미리보기 개선의 배포 대상 버전입니다. **시험 앱의 실제 녹화·저장·앱 재시작 후 재생·Excel 결과를 확인했습니다.** 게시 완료는 아래 절차의 원격 커밋·태그·자산 체크섬 검증으로 판정하며 버전 표기나 문서만으로 주장하지 않습니다. 최종 확인 범위는 [VALIDATION.html](VALIDATION.html)에 반영합니다.

새 기본 정책 `data_policy=local`은 업무 내용이 공개 MCP 응답으로 나가지 않도록 제한합니다. 기존 설정에 정책이 없을 때도 적용되므로, 기존 모델 화면 판정 작업이 첫 입력 전에 중지될 수 있음을 출시 안내에 명시합니다. 기존 녹화·이미지·revision·실행 기록을 자동 변경하지 않으며 connected는 관리자가 명시한 별도 연결 정책입니다.

현재 출시 명세가 이전 Release를 가리키면 그대로 새 소스의 증거로 사용하지 않습니다. **시험을 마친 최종 ZIP으로 BUILD-MANIFEST, DRIVER-BUNDLE-MANIFEST, PUBLICATION-MANIFEST를 함께 다시 생성**합니다. 버전 숫자만 수동 수정하지 않습니다.

## 포함할 검증 기록

- 영향 경로 회귀 검사 381개 중 380개 통과, 시험 환경의 하드링크 권한 제한으로 1개 제외. 배율·미리보기 관련 추가 검사 9개 통과.
- 실제 MCP 읽기 전용 요청 6회: 프로세스 재시작 후 별칭 유지, 원본 작업 바이트 보존, 공개 응답에서 시험 업무 내용 제외.
- 실제 시험 앱의 두 단계 녹화, 보이는 편집창에서 입력 변수 연결, revision 1 저장, 로컬 스킬 미리보기·내보내기 확인.
- 시험 앱 완전 종료·새 프로세스 재시작 후 다른 입력값으로 두 단계 저장 작업을 재생했습니다. 2/2 verified와 새 XLSX의 실제 셀 내용을 확인했고 내보내기는 1회였습니다. 드롭다운 선택을 포함한 운영 업무 전체의 시험이 아닙니다.
- 실제 Driver 재생 active 시간은 12,320.35ms(동작 12,288.6ms, 창 연결 29.81ms)였으며 사용자 입력 대기는 제외했습니다. 기존 버전과 통제한 실제 앱 성능 비교나 10초 업무 완료 보장은 아닙니다.
- 가상 UIA 화면 기반 엔진 비교 120회: 최초 실행 중앙값 +2.73%, 반복 실행 +0.45%, 입력·관찰 횟수 동일. 실제 Driver·모델·운영 서버의 성능 보장은 아닙니다.
- 외부 모델 API·원격 OCR·업로드를 작성 기능에 추가하지 않았습니다. OS 전체의 사외 통신 차단을 검증한 것은 아닙니다.

## 배포 절차

1. 실제 검증을 마치고 소스 버전, 사용자 문서와 결과 범위를 확정합니다. `build_portable.py`로 기본 ZIP과 소스 ZIP을 만들고, `bundle_driver.py`로 검토된 Driver를 포함한 ZIP을 만듭니다. 새 네이티브 소스는 최종 도우미 실행파일로 다시 빌드합니다.
2. 전용 배포 자산 폴더에 정확히 아래 7개 파일을 준비합니다. 다른 문서나 과거 ZIP을 섞지 않습니다.

   - Computer-Use-MCP-0.19.0.zip
   - Computer-Use-MCP-0.19.0-source.zip
   - Computer-Use-MCP-0.19.0-with-driver.zip
   - cua-driver.exe
   - CUA-DRIVER-LICENSE.md
   - DRIVER-BUNDLE-MANIFEST.json
   - SHA256SUMS.txt

   SHA256SUMS.txt에는 자신을 제외한 6개 파일의 해시가 들어갑니다.

3. 전용 공개 저장소의 소스를 소스 ZIP과 바이트 단위로 맞춥니다. 새 소스 파일을 먼저 추적 목록에 추가합니다. 줄바꿈을 변환하지 않습니다. 사용자 설정, 원본 녹화·캡처, 실제 업무 결과, `.data`, 과거 ZIP·런타임 바이너리를 소스 커밋에 넣지 않습니다.
4. 아래 명령으로 최종 자산에서 세 명세를 생성합니다.

```powershell
python tools/publication.py prepare --repo . --assets release/assets-0.19.0
```

소스 ZIP, 런타임, Driver 파일의 재고·버전·체크섬과 저장소 소스를 검사한 뒤 명세를 씁니다. 불일치 파일을 덮어써서 숨기지 않고 실패합니다. PUBLICATION-MANIFEST 자신은 순환 해시를 피하기 위해 해시 목록에서 제외합니다. 별도 게시 도구는 `publication_support_files`로 구분합니다.

5. 소스와 명세를 명시적으로 stage하고 diff를 확인해 커밋합니다. 커밋된 Git 바이트도 검사합니다.

```powershell
python tools/publication.py check --repo . --assets release/assets-0.19.0 --git-ref HEAD
```

6. 커밋을 푸시하고 원격 브랜치 SHA가 같은지 확인합니다. Release notes는 7개 자산 폴더 밖에 둡니다. 다음 명령은 검사된 자산 사본만 게시하며 기존 태그·Release를 덮어쓰지 않습니다.

```powershell
python tools/publication.py publish --repo . --assets release/assets-0.19.0 --github-repo bongG-moon/computeruse_mcp --notes-file release-notes.md
```

게시 후 태그가 검사한 커밋을 가리키는지, 원격 7개 자산의 크기·SHA-256이 일치하는지 다시 검사합니다. GitHub Release 이벤트의 자동 검사는 업로드 전 검사를 대신하지 않습니다. 시험판이라면 prepare/check/publish 모두에 `--release-status prerelease`를 지정합니다.

기본/Driver 포함 ZIP은 핵심 실행 파일과 필요한 런타임만 포함합니다. 개발 시험·소스는 별도 source ZIP에 둡니다. 사용자 PC의 기존 작업 상태는 ZIP에 넣지 않습니다.

<a id="main-source-validation-20261024"></a>
## 이전 검증 기록과 구분

2026-10-24의 main 소스 커밋은 당시 마지막 Release v0.18.8 이후 팝업 좌표·입력, 선택색, 확인 구간 묶기와 같은 실행 재개 개선을 포함했습니다. 그 소스 커밋은 당시 새 Release ZIP으로 게시된 상태가 아니었습니다. 이번 0.19.0에서는 이 소스 개선과 로컬 작성 기능을 함께 최종 빌드·검증한 뒤 명세를 새로 생성합니다. 이전 Release 명세를 새 main 전체의 증거로 사용하지 않습니다.

기존 22단계 중첩 팝업 재생과 XLSX 데이터 5행 확인은 이전 시험 앱 검증입니다. 그 성공을 이번 입력폼·편집창·스킬 경로의 전체 재생 성공으로 대신하지 않습니다.

## 0.16.0 기록 정정

과거 v0.16.0 Git 태그에는 두 배포 명세의 0.15.0 정보가 남았으나, 설치 파일과 별도 첨부한 Computer-Use-MCP-0.16.0-source.zip은 0.16.0이었습니다. 태그와 시험한 ZIP·EXE 바이트는 보존하고 main의 명세를 정정했습니다. GitHub 자동 생성 Source code ZIP과 별도 첨부한 시험 소스 ZIP은 구분해야 합니다.
