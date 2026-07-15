# 가지마켓 설치 및 실행 방법

이 문서는 새 PC에서 현재 프로젝트를 실행하기 위한 절차입니다.

## 1. 사전 준비

- Python 3.12 권장
- `git`
- ngrok 외부 공개가 필요하면 ngrok 계정 authtoken

## 2. 프로젝트 위치로 이동

```bash
cd /path/to/gazimarket
```

## 3. 환경 구성

### 방법 A: Conda 사용

```bash
conda env create -f environment.yaml
conda activate gazimarket
```

### 방법 B: Python venv 사용

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## 4. 서버 실행

기본 설정은 `config.example.yaml`로 바로 실행됩니다. 로컬에서 별도 설정을 쓰려면 아래처럼 복사해서 수정합니다.

```bash
cp config.example.yaml config.yaml
```

`config.yaml`은 개인 로컬 설정 파일이므로 GitHub에 올리지 않습니다. 외부 공개나 장기 실행 환경에서는 세션 보호를 위해 `GAZIMARKET_SECRET_KEY` 환경변수를 별도로 지정하세요.

```bash
export GAZIMARKET_SECRET_KEY=충분히_긴_랜덤_문자열
```

venv를 자동으로 준비하고 실행하려면:

```bash
bash scripts/serve.sh
```

직접 실행하려면:

```bash
flask --app app run --host 0.0.0.0 --port 5000
```

브라우저에서 접속:

```text
http://127.0.0.1:5000
```

## 5. ngrok으로 외부 공개

먼저 4번 단계의 Flask 서버를 실행한 상태에서, 다른 터미널을 열어 실행합니다.

처음 실행하며 authtoken을 저장하는 경우:

```bash
NGROK_AUTHTOKEN=발급받은_토큰 bash scripts/ngrok.sh
```

이미 authtoken을 저장한 경우:

```bash
bash scripts/ngrok.sh
```

실행 후 ngrok이 출력하는 `https://...ngrok-free.app` 주소로 접속하면 됩니다.

## 6. 데이터 저장 위치

- SQLite DB: `instance/gazimarket.sqlite3`
- 업로드 이미지: `static/uploads/`
- 로컬 ngrok 설정: `.ngrok/ngrok.yml`

위 파일들은 실행 중 생성되는 로컬 데이터입니다.

## 7. 기본 관리자 계정

- 아이디: `admin`
- 비밀번호: `admin1234`

운영 목적 사용 전 반드시 로그인 후 비밀번호를 변경하세요.
