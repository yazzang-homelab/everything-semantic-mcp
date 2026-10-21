## findex — 이름을 몰라도 찾는 파일 검색 (사람 + AI 에이전트)

설치 없이 실행 파일 하나로 동작합니다.

| OS | 파일 |
|---|---|
| Windows 10/11 (x64) | `findex-windows-x64.exe` |
| macOS (Apple Silicon) | `findex-macos-arm64` |

Intel Mac은 소스로 실행하세요: `pip install git+https://github.com/yazzang-homelab/everything-semantic-mcp && findex`
| Linux (x64) | `findex-linux-x64` |

**사용**
- 더블클릭(또는 `findex`) → 브라우저에 검색 창이 열립니다.
- AI 에이전트 연결: MCP 설정에 `"command": "<경로>/findex", "args": ["mcp"]`.
- Windows에서 voidtools Everything이 실행 중이면 그 색인을 그대로 씁니다. 아니면(맥·리눅스 포함) findex가 자체 색인을 만듭니다(첫 실행 시 홈 폴더 스캔).
- 의미 검색: 처음 한 번 검색 창의 "의미 색인 만들기" 또는 `findex reindex`. 모델(약 120MB)은 첫 사용 때 내려받습니다.

**처음 실행 시 경고**
- Windows SmartScreen: "추가 정보 → 실행". (코드 서명 인증서가 없는 오픈소스 빌드)
- macOS: `xattr -d com.apple.quarantine findex-macos-*` 후 `chmod +x`. 또는 Finder에서 우클릭 → 열기.
- Linux: `chmod +x findex-linux-x64`.

각 파일 옆 `.sha256`으로 무결성을 확인할 수 있습니다.
