# 배포 명세 생성과 검증

사용자는 이 작업을 진행할 필요가 없습니다. 아래는 배포 담당자용 절차입니다.

`BUILD-MANIFEST.json`은 시험한 소스와 런타임을 기록합니다. `DRIVER-BUNDLE-MANIFEST.json`은 Driver 포함 ZIP을 기록합니다. `PUBLICATION-MANIFEST.json`은 이 두 파일, 저장소의 소스, 배포 파일의 버전·크기·SHA-256을 연결합니다. 세 파일은 최종 ZIP에서 자동 생성하며 버전 숫자만 수동으로 바꾸지 않습니다.

<a id="main-source-validation-20261024"></a>
## main 소스 검증 — 2026-10-24 / 아직 미배포

현재 `main`은 마지막 Release `v0.18.8` 이후 수정본입니다. 제품 버전 숫자, 기존 태그, Release 자산, 출시 명세 세 파일은 이번 소스 커밋에서 변경하지 않았습니다. **기존 출시 명세는 v0.18.8 Release에 대한 기록이며 현재 main 전체를 검증한 명세가 아닙니다.** 출시 파일의 일치 검사는 `v0.18.8`을 체크아웃하고 해당 Release 자산으로 수행해야 합니다. 현재 main 또는 수동 workflow를 과거 Release 자산과 비교하면 불일치가 정상입니다. 다음 배포에서는 최종 ZIP으로 명세를 새로 생성해야 합니다.

반영한 변경:

- 네이티브 팝업 캡처의 물리 좌표와 입력 경로를 묶습니다. 프레임·창·기하 정보가 달라지면 입력을 막고, 전달이 불명확한 클릭은 반복하지 않습니다.
- 팝업 선택/호버 색 차이를 제한적으로 보정합니다. 글자 변경·비활성·가림·중복 후보 검사는 유지합니다.
- 명시적인 화면 확인은 불필요한 UIA 조회를 선행하지 않습니다. 잘못된 판정 ID에는 유효한 동일 요청을 돌려주며 무제한 새 프레임 발급을 피합니다.
- 공식 작업 수정 API의 `group_reviews`, `use_popup_image_conditions`로 확인 구간을 새 revision에 명시적으로 정리할 수 있습니다. 기존 녹화본·실행 스냅샷·사람 승인은 자동 변경하지 않습니다. 팝업 존재/이미지 일치는 업무 결과 확인과 구분합니다.
- 창 연결·동작·캡처·판정·호출 사이 시간을 분리하여 기록합니다. Clef 연동은 아직 구현하지 않았습니다.

검증 범위:

- 2026-10-23 영향 경로 회귀 시험: 중복 제거한 734건 중 733건 통과, 1건은 시험 파일시스템의 하드링크 권한 제한으로 생략, 실패/오류 0건. 2026-10-24 아래 17개 구현 파일이 시험 당시 해시와 같은지 재확인했습니다.
- 2026-10-24 게시 전 최종 확인: 팝업 좌표/입력, 선택색 보정, 확인 묶기, 작업 revision, 패키지 및 배포 도구 시험 84건 모두 통과. 소스 전용 게시 사본에는 이미지 런타임을 복사하지 않았으므로 동일 소스 해시의 개발 폴더에서 실행했습니다. 설치 문서의 배포 안내 링크도 패키지 외부 문서를 가리키도록 수정했습니다.
- 시험 앱에서 기존 22단계 녹화의 입력·이미지를 유지한 새 revision을 실제 재생했습니다. 모델 화면 확인 9회에서 4회, 입력 반복 0회, 조회/내보내기 각 1회, 22/22 완료, 생성 XLSX의 데이터 5행을 별도로 확인했습니다. 기존 revision은 보존했습니다.
- 각각 한 번 관측한 기존/정리한 실행의 MCP 호출 시간은 55.6/41.1초, 전체 경과 시간은 281.8/166.9초였습니다. 호출 사이 시간에는 모델 응답 외에 도구 조정·진단도 포함됩니다. 통제된 성능 비교나 운영 SmartFX 검증, 10초 실행 보장은 아닙니다.
- 네이티브 경로는 실제 WinForms 팝업과 긴 mouse-up 처리로 별도 시험했습니다. Driver의 정확한 HWND 누락 응답은 통제한 시험 조건입니다.
- 이번 게시에는 시험 앱 원본 녹화·화면 이미지·사용자 설정·운영 데이터·런타임 바이너리를 넣지 않습니다.

영향 경로 테스트 모듈(중복 import된 테스트는 ID로 한 번씩 실행):

```text
test_checkpoint_groups test_popup_review_revision test_popup_coordinate_routing test_native_pointer
test_transient_popup test_hover_matching test_menu_appearance test_selection_capability
test_image_wait_diagnostics test_airtest_backend test_image_border_clip test_image_backend_routing
test_image_steps test_image_completion test_msaa_dropdown test_recording_windows
test_interaction test_visual_review test_task_capacity test_easy_api
test_process_editor test_workflows test_workflow_visual_review test_workflow_target_review
test_workflow_windows test_workflow_capacity test_task_revision test_process_steps
test_bundle_packaging test_visual_targets test_visual_recovery_scene test_checkpoint_capture
test_guard_performance
```

시험한 구현 파일 SHA-256:

```json
{
  "checkpoint_groups.py": "2570577452c380e378ace0ffc3c2fed9af96c27cfd9f2d24e6fb1f2bd4386a4d",
  "task_revision.py": "312904e188ba35b2ca10fba026faf21798e380ff5f60996d6a25fa631c3c5fee",
  "process_steps.py": "0dbea349a8529f2ec1b3201db1f2d975807950fa1e2027770d12669beec21231",
  "workflows.py": "7c39874b77db6ff5ea76cabd14b8713b678c50602db5373d54941eadb101ec8b",
  "easy_api.py": "bf6c6686212bbbd95cbd0c07cd1b33a6581125d3517cc816828dc5b5f6737d83",
  "process_editor.py": "8781eadd051f23a0cb4038ec3a9f4fc8c40ffb95697951ded344122c4c697394",
  "vendor/guard.py": "6d1738baf6747cb3684deef222c0d74b766232a45fd798d9910ab0de2d97d613",
  "transient_popup.py": "bdbfca9749d3d51bde64673684e3ddd0e147ff5418c32d5da719128e0f3bf981",
  "airtest_backend.py": "de4a293c1ab3d8b6ebd834404a3a677b05b720ef71a9b5f276e124f4c8dc5700",
  "image_targets.py": "19c695d528015fa48ffcea1b71334d712aabfbbe64bc26bbcd6d22942a0eb88b",
  "image_steps.py": "a6245bb97ace1956694432e13098fedaed2149c79beb7513f64b0645c02d9b22",
  "session_runtime.py": "d83faef169b9876a2f9e696206d158407b73a11ebd12232d9e14474a0ffef782",
  "recording_windows.py": "7ea8bd795d53985bc08faa3c18447e00ddd6d3410a868bcb0ad30819e5cabe8f",
  "msaa_dropdown.py": "deb977b586ca16f21b424aa5d58d2cf08aeacfc5730790c9201add7fc000ed2f",
  "interaction.py": "a40a6a657bbd07be799e41dce183e6235e78ce10f4e2c011902692e50d90653c",
  "visual_review.py": "20147ae44b3264aa6becd33ef6554c88f15b214cf917f7b8b94b989c8faa6b3e",
  "build_portable.py": "5ad0bea7d301b12af9b3a62187eb7fedf282d3a841081f3f6c9fec6db0749a61"
}
```

## 배포 순서

1. `build_portable.py`, `bundle_driver.py`로 시험·검증한 배포 파일을 만듭니다.
2. 배포 폴더에 기본 ZIP, 소스 ZIP, Driver 포함 ZIP, `cua-driver.exe`, `CUA-DRIVER-LICENSE.md`, `DRIVER-BUNDLE-MANIFEST.json`, `SHA256SUMS.txt`의 7개 파일을 준비합니다. `SHA256SUMS.txt`는 나머지 6개 파일의 해시를 포함합니다.
3. 공개할 저장소의 소스를 소스 ZIP과 바이트 단위로 맞춥니다. 줄바꿈을 임의로 변환하지 않습니다. 처음 적용할 때는 이 안내, `tools/publication.py`, `tools/test_publication.py`, `.github/workflows/publication.yml`도 준비합니다.
4. 저장소 루트에서 아래 명령을 실행합니다. `release/assets-0.17.0`은 실제 버전의 배포 폴더로 바꿉니다.

```powershell
python tools/publication.py prepare --repo . --assets release/assets-0.17.0
```

이 명령은 제품 버전, ZIP 내부 파일, 소스·런타임·Driver 해시, 기존 추적 파일 전체를 검사한 뒤 세 명세를 생성합니다. 소스 ZIP과 다른 소스가 있으면 파일을 덮어써서 숨기지 않고 실패합니다. `PUBLICATION-MANIFEST` 자신은 순환 해시를 피하기 위해 목록에서 제외합니다. 제품에 포함되지 않은 배포 도구는 `publication_support_files`로 구분합니다.

5. 변경 파일과 세 명세를 명시적으로 스테이징하고 커밋·푸시합니다. 다음 명령으로 **커밋된 Git 바이트**도 검사합니다.

```powershell
python tools/publication.py check --repo . --assets release/assets-0.17.0 --git-ref HEAD
```

6. GitHub CLI가 연결되어 있다면 다음 명령으로 게시합니다. 이 경로는 검사를 통과해야만 GitHub Release 생성을 호출합니다. 기존 태그나 Release를 덮어쓰지 않습니다.

```powershell
python tools/publication.py publish --repo . --assets release/assets-0.17.0 --github-repo bongG-moon/computeruse_mcp --notes-file release-notes.md
```

시험판이라면 prepare/check/publish 모두에 `--release-status prerelease`를 지정합니다. GitHub의 release 이벤트와 수동 실행 workflow도 공개 파일을 내려받아 검사합니다. 게시 이후 자동 검사는 업로드 전 검사를 대신하지 않습니다.

## 0.16.0 기록 정정

처음 게시한 `v0.16.0` Git 태그에는 두 배포 명세의 0.15.0 정보가 남았습니다. 설치 파일과 별도 첨부한 `Computer-Use-MCP-0.16.0-source.zip`은 정상 0.16.0입니다. 저장소 `main`에서 명세를 정정했고, 시험한 ZIP·EXE의 바이트와 기존 태그는 유지합니다. 따라서 GitHub 자동 생성 **Source code (zip/tar.gz)**의 과거 태그 기록은 그대로이며, 0.16.0의 시험 소스 확인에는 이름이 명시된 첨부 소스 ZIP을 사용합니다.
