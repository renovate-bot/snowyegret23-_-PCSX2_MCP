# PCSX2 MCP

**[ENGLISH](README.md) | KOREAN**

## 한국어

PCSX2 MCP는 PCSX2의 EE 및 IOP GDB Remote Serial Protocol 엔드포인트를 통해 에뮬레이터를 제어하고 디버깅하는 Windows 중심의 Model Context Protocol 서버입니다. 에뮬레이터 제어, 메모리 조사, 디버거 작업, 추적, 세이브 스테이트, 치트, 스크린샷, 프로세스 관리, 로그 접근 기능을 MCP 도구로 제공합니다.

> [!IMPORTANT]
> 기본 레지스터 및 메모리 기능에는 PCSX2 GDB 서버가 필요합니다. 에뮬레이터 명령, 디버거 표현식, 치트 제어, 스크린샷, 세이브 스테이트 자동화 같은 확장 도구에는 이 서버가 사용하는 `qPcsx2` 명령을 구현한 호환 PCSX2 빌드가 필요합니다.

### 주요 기능

- EE 또는 IOP GDB 서버 연결 및 연결 상태 확인
- 일시 정지, 재개, 단일 스텝, 리셋, 프레임 진행
- 레지스터, 메모리, 문자열, 파일 읽기 및 쓰기
- 메모리 영역 덤프, 와일드카드 바이트 패턴 검색, 스냅샷 비교
- 디스어셈블, 디버거 표현식 평가, 스레드, 모듈, 백트레이스 조사
- 브레이크포인트 및 워치포인트 추가, 제거, 조회, 전체 삭제
- 세이브 스테이트 저장 및 불러오기, 스크린샷, 되감기 링 버퍼
- 패치 다시 불러오기 및 런타임 치트 조회, 활성화, 비활성화
- 레지스터 및 메모리 상태를 JSONL로 주기적 기록
- PCSX2 프로세스 실행, 조회, 종료
- PCSX2 로그 경로 탐색, 마지막 줄 조회, 오류 필터링

### 요구 사항

- Windows
- Python 3.10 이상
- EE/IOP GDB 서버가 활성화된 PCSX2
- 로컬 stdio 서버를 지원하는 MCP 클라이언트

기본 연결 주소는 다음과 같습니다.

| 대상 | 호스트 | 포트 |
| --- | --- | ---: |
| EE | `127.0.0.1` | `10501` |
| IOP | `127.0.0.1` | `10502` |

### 설치

PowerShell에서 저장소 폴더로 이동한 뒤 실행합니다.

```powershell
python -m pip install -e .
```

서버를 직접 실행하려면 다음 명령을 사용합니다.

```powershell
python -m pcsx2_mcp
```

서버는 stdio로 통신하므로 일반적으로 독립형 대화형 터미널이 아니라 MCP 클라이언트에서 실행합니다.

### Codex MCP 설정

PCSX2 MCP를 Codex의 로컬 stdio 서버로 등록하려면 `%USERPROFILE%\.codex\config.toml`에 다음 블록을 추가합니다. `C:\path\to\PCSX2_MCP`는 이 저장소를 복제한 실제 경로로 변경하십시오.

```toml
[mcp_servers.pcsx2]
command = "python"
args = ["-m", "pcsx2_mcp"]
cwd = 'C:\path\to\PCSX2_MCP'

[mcp_servers.pcsx2.env]
PYTHONPATH = 'C:\path\to\PCSX2_MCP\src'
```

`python` 명령은 프로젝트 의존성을 설치한 Python 인터프리터를 가리켜야 합니다. `config.toml`을 변경한 뒤 Codex를 다시 시작합니다.

### PCSX2 설정

1. MCP 클라이언트에서 서버를 시작합니다.
2. `configure_gdb_settings` 도구를 한 번 실행하거나, 호환 PCSX2 설정에서 EE 및 IOP GDB 서버를 직접 활성화합니다.
3. GDB 설정을 변경했다면 PCSX2를 다시 시작합니다.
4. 게임을 실행한 뒤 `target="ee"` 또는 `target="iop"`로 `connect`를 호출합니다.
5. 디버깅 및 자동화 도구를 사용하기 전에 `status` 또는 `pcsx2_status`로 상태를 확인합니다.

### 일반적인 작업 순서

1. `launch_pcsx2`를 사용하거나 PCSX2를 직접 실행합니다.
2. 필요한 대상에 `connect`를 사용합니다.
3. `get_registers`, `read_memory`, `disassemble`, `evaluate`로 상태를 조사합니다.
4. `pause`, `step`, `resume`, 브레이크포인트, 워치포인트로 실행을 제어합니다.
5. PCSX2를 종료하기 전에 `disconnect`를 호출합니다.

### 안전 주의사항

이 서버는 에뮬레이션 메모리 수정, 파일을 통한 메모리 덮어쓰기, 레지스터 변경, 세이브 스테이트 불러오기, 치트 제어, PCSX2 프로세스 종료를 수행할 수 있습니다. 로컬 MCP 클라이언트에서만 사용하고 파괴적인 작업을 승인하기 전에 도구 인수를 확인하십시오.

생성된 로그, 스크린샷, 추적, 되감기 캡처, 가상 환경, 바이트코드, 빌드 결과물은 버전 관리에서 제외됩니다.

### 라이선스

이 프로젝트에는 [LICENSE](LICENSE)에 명시된 라이선스가 적용됩니다.
