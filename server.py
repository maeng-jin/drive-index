#!/usr/bin/env python3
"""
drive-index HTTP 서버.

캐시 디렉토리(기본: ./data)에 있는 파일 목록(.txt), 썸네일 이미지,
용량 정보(capacity.json)를 HTTP로 서빙합니다. file_tree_viewer.html을
같은 서버에서 함께 제공해서 외부 피시에서도 브라우저로 바로 접속할 수 있어요.

사용법:
    python server.py                          # 기본: ./data, 포트 8765, 모든 인터페이스
    python server.py --data D:\\drive-cache   # 캐시 폴더 지정
    python server.py --port 8080              # 포트 변경
    python server.py --host 127.0.0.1         # 로컬에서만 접속 허용

캐시 디렉토리 안에 둘 것:
    *.txt                       list_drive_files.py가 생성한 파일 목록
    *.jpg / *.png / ...         같은 prefix의 썸네일 이미지 (선택)
    capacity.json               드라이브 라벨/용량 (선택, capacity.example.json 참고)

엔드포인트:
    GET /                       file_tree_viewer.html
    GET /api/manifest           드라이브 목록 + 용량 정보 (JSON)
    GET /api/file/<name>        파일 목록 .txt 원본
    GET /api/thumb/<name>       썸네일 이미지
    GET /api/capacity           capacity.json (있을 때)
"""

import argparse
import json
import mimetypes
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_DATA_DIR = SCRIPT_DIR / "data"
DEFAULT_PORT = 8765
DEFAULT_HOST = "0.0.0.0"

TXT_EXTS = {".txt", ".tsv", ".csv"}
IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}


class DriveIndexHandler(BaseHTTPRequestHandler):
    data_dir: Path = DEFAULT_DATA_DIR
    viewer_path: Path = SCRIPT_DIR / "file_tree_viewer.html"

    def log_message(self, format, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status)

    def _send_file(self, path: Path, content_type: str = None):
        if not path.is_file():
            self.send_error(404, f"Not found: {path.name}")
            return
        if content_type is None:
            guessed, _ = mimetypes.guess_type(str(path))
            content_type = guessed or "application/octet-stream"
        try:
            body = path.read_bytes()
        except OSError as e:
            self.send_error(500, f"Read error: {e}")
            return
        self._send_bytes(body, content_type)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path

        if route in ("/", "/index.html"):
            self._send_file(self.viewer_path, "text/html; charset=utf-8")
            return

        if route == "/api/manifest":
            self._serve_manifest()
            return

        if route == "/api/capacity":
            cap = self._find_capacity_json()
            if cap:
                self._send_file(cap, "application/json; charset=utf-8")
            else:
                self._send_json({})
            return

        if route.startswith("/api/file/"):
            name = unquote(route[len("/api/file/"):])
            self._serve_data_file(name, TXT_EXTS)
            return

        if route.startswith("/api/thumb/"):
            name = unquote(route[len("/api/thumb/"):])
            self._serve_data_file(name, IMG_EXTS)
            return

        self.send_error(404, "Not found")

    def _serve_data_file(self, filename: str, allowed_exts: set):
        # 경로 탈출 방지: 슬래시/백슬래시/.. 차단, 점으로 시작하는 파일 차단
        if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
            self.send_error(400, "Invalid filename")
            return
        path = (self.data_dir / filename).resolve()
        try:
            path.relative_to(self.data_dir.resolve())
        except ValueError:
            self.send_error(400, "Path outside data dir")
            return
        if path.suffix.lower() not in allowed_exts:
            self.send_error(400, "Unsupported extension")
            return
        self._send_file(path)

    def _find_capacity_json(self):
        cap = self.data_dir / "capacity.json"
        if cap.is_file():
            return cap
        for p in sorted(self.data_dir.glob("*.json")):
            if "capacity" in p.name.lower():
                return p
        return None

    def _serve_manifest(self):
        if not self.data_dir.is_dir():
            self._send_json({
                "error": "data directory not found",
                "data_dir": str(self.data_dir),
                "drives": [],
                "capacity": {},
            })
            return

        txt_files = []
        img_by_base = {}

        for p in sorted(self.data_dir.iterdir()):
            if not p.is_file():
                continue
            ext = p.suffix.lower()
            if ext in TXT_EXTS:
                txt_files.append(p)
            elif ext in IMG_EXTS:
                base = p.stem.lower()
                img_by_base.setdefault(base, p)

        drives = []
        for txt in txt_files:
            base = txt.stem.lower()
            thumb = None
            # 정확히 일치 우선, 그 다음 prefix 매칭
            if base in img_by_base:
                thumb = img_by_base[base]
            else:
                for ibase, ipath in img_by_base.items():
                    if base.startswith(ibase + "_") or base.startswith(ibase + "-"):
                        thumb = ipath
                        break

            try:
                size = txt.stat().st_size
                mtime = int(txt.stat().st_mtime)
            except OSError:
                size = 0
                mtime = 0

            drives.append({
                "filename": txt.name,
                "key": base,
                "txt_url": f"/api/file/{quote(txt.name)}",
                "thumbnail_filename": thumb.name if thumb else None,
                "thumbnail_url": f"/api/thumb/{quote(thumb.name)}" if thumb else None,
                "file_size": size,
                "mtime": mtime,
            })

        capacity = {}
        cap_path = self._find_capacity_json()
        if cap_path:
            try:
                capacity = json.loads(cap_path.read_text(encoding="utf-8"))
            except Exception as e:
                capacity = {"_parse_error": str(e)}

        self._send_json({
            "drives": drives,
            "capacity": capacity,
            "capacity_url": "/api/capacity" if cap_path else None,
            "data_dir": str(self.data_dir),
        })


def detect_lan_ip() -> str:
    """LAN IP 추정 — 외부로 가는 UDP 소켓을 잠깐 만들어 보고 로컬 주소를 얻는다."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="drive-index 캐시 파일 목록을 HTTP로 서빙",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--data", default=str(DEFAULT_DATA_DIR),
                        help=f"캐시 디렉토리 (기본: {DEFAULT_DATA_DIR})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"포트 (기본: {DEFAULT_PORT})")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"바인드 주소 (기본: {DEFAULT_HOST}, 외부 접속 허용. "
                             "127.0.0.1로 두면 로컬만)")
    parser.add_argument("--viewer", default=str(SCRIPT_DIR / "file_tree_viewer.html"),
                        help="뷰어 HTML 경로 (기본: 스크립트 옆 file_tree_viewer.html)")
    args = parser.parse_args()

    data_dir = Path(args.data).expanduser().resolve()
    viewer_path = Path(args.viewer).expanduser().resolve()

    if not viewer_path.is_file():
        print(f"[에러] 뷰어 HTML을 찾을 수 없습니다: {viewer_path}", file=sys.stderr)
        return 1

    if not data_dir.exists():
        print(f"[안내] 캐시 디렉토리가 없어 생성합니다: {data_dir}", file=sys.stderr)
        data_dir.mkdir(parents=True, exist_ok=True)
    elif not data_dir.is_dir():
        print(f"[에러] 캐시 경로가 디렉토리가 아닙니다: {data_dir}", file=sys.stderr)
        return 1

    DriveIndexHandler.data_dir = data_dir
    DriveIndexHandler.viewer_path = viewer_path

    print("=== drive-index server ===", file=sys.stderr)
    print(f"캐시 디렉토리: {data_dir}", file=sys.stderr)
    print(f"뷰어 HTML:    {viewer_path}", file=sys.stderr)
    print(f"바인드:       {args.host}:{args.port}", file=sys.stderr)
    print(f"로컬 접속:    http://127.0.0.1:{args.port}/", file=sys.stderr)
    if args.host in ("0.0.0.0", ""):
        lan = detect_lan_ip()
        if lan:
            print(f"LAN 접속:     http://{lan}:{args.port}/", file=sys.stderr)
    print("Ctrl+C로 종료", file=sys.stderr)
    print("", file=sys.stderr)

    server = ThreadingHTTPServer((args.host, args.port), DriveIndexHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료됨", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
