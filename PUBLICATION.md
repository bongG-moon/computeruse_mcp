# 배포 명세 생성과 검증

사용자는 이 작업을 진행할 필요가 없습니다. 아래는 배포 담당자용 절차입니다.

`BUILD-MANIFEST.json`은 시험한 소스와 런타임을 기록합니다. `DRIVER-BUNDLE-MANIFEST.json`은 Driver 포함 ZIP을 기록합니다. `PUBLICATION-MANIFEST.json`은 이 두 파일, 저장소의 소스, 배포 파일의 버전·크기·SHA-256을 연결합니다. 세 파일은 최종 ZIP에서 자동 생성하며 버전 숫자만 수동으로 바꾸지 않습니다.

## 배포 순서

1. `build_portable.py`, `bundle_driver.py`로 시험·검증한 배포 파일을 만듭니다.
2. 배포 폴더에 기본 ZIP, 소스 ZIP, Driver 포함 ZIP, `cua-driver.exe`, `CUA-DRIVER-LICENSE.md`, `DRIVER-BUNDLE-MANIFEST.json`, `SHA256SUMS.txt`의 7개 파일을 준비합니다. `SHA256SUMS.txt`는 나머지 6개 파일의 해시를 포함합니다.
3. 공개할 저장소의 소스를 소스 ZIP과 바이트 단위로 맞춥니다. 줄바꿈을 임의로 변환하지 않습니다. 처음 적용할 때는 이 안내, `tools/publication.py`, `tools/test_publication.py`, `.github/workflows/publication.yml`도 준비합니다.
4. 저장소 루트에서 아래 명령을 실행합니다. `release/assets-0.16.0`은 실제 버전의 배포 폴더로 바꿉니다.

```powershell
python tools/publication.py prepare --repo . --assets release/assets-0.16.0
```

이 명령은 제품 버전, ZIP 내부 파일, 소스·런타임·Driver 해시, 기존 추적 파일 전체를 검사한 뒤 세 명세를 생성합니다. 소스 ZIP과 다른 소스가 있으면 파일을 덮어써서 숨기지 않고 실패합니다. `PUBLICATION-MANIFEST` 자신은 순환 해시를 피하기 위해 목록에서 제외합니다. 제품에 포함되지 않은 배포 도구는 `publication_support_files`로 구분합니다.

5. 변경 파일과 세 명세를 명시적으로 스테이징하고 커밋·푸시합니다. 다음 명령으로 **커밋된 Git 바이트**도 검사합니다.

```powershell
python tools/publication.py check --repo . --assets release/assets-0.16.0 --git-ref HEAD
```

6. GitHub CLI가 연결되어 있다면 다음 명령으로 게시합니다. 이 경로는 검사를 통과해야만 GitHub Release 생성을 호출합니다. 기존 태그나 Release를 덮어쓰지 않습니다.

```powershell
python tools/publication.py publish --repo . --assets release/assets-0.16.0 --github-repo bongG-moon/computeruse_mcp --notes-file release-notes.md
```

시험판이라면 prepare/check/publish 모두에 `--release-status prerelease`를 지정합니다. GitHub의 release 이벤트와 수동 실행 workflow도 공개 파일을 내려받아 검사합니다. 게시 이후 자동 검사는 업로드 전 검사를 대신하지 않습니다.

## 0.16.0 기록 정정

처음 게시한 `v0.16.0` Git 태그에는 두 배포 명세의 0.15.0 정보가 남았습니다. 설치 파일과 별도 첨부한 `Computer-Use-MCP-0.16.0-source.zip`은 정상 0.16.0입니다. 저장소 `main`에서 명세를 정정했고, 시험한 ZIP·EXE의 바이트와 기존 태그는 유지합니다. 따라서 GitHub 자동 생성 **Source code (zip/tar.gz)**의 과거 태그 기록은 그대로이며, 0.16.0의 시험 소스 확인에는 이름이 명시된 첨부 소스 ZIP을 사용합니다.
