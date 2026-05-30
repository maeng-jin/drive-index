#!/usr/bin/env python3
"""
외장하드(또는 임의의 디렉토리) 내부의 모든 파일을 재귀적으로 탐색하여
풀 패스(full path) 목록을 텍스트 파일로 저장하는 스크립트.

대용량(수백만 파일)에서 빠르게 동작하도록 최적화:
- os.scandir 사용 (stat 정보가 DirEntry에 캐시됨 → --with-size일 때 syscall 절반)
- 버퍼링된 쓰기 (1만 개씩 모아서 flush)
- tqdm 프로그래스 바로 진행률/ETA/속도 표시 (없으면 자동으로 stderr 폴백)
- 두 단계 옵션:
    1) 파일 수 사전 카운팅 → 정확한 진행률 바
    2) --no-count 옵션으로 카운팅 생략 → 즉시 시작

[출력 포맷 — 그룹 포맷]
같은 폴더 안의 여러 파일은 부모 경로를 공유하므로, 부모 경로를 한 번만
기록하고 파일은 들여쓰기로 표시합니다. 텍스트 파일 크기가 크게 줄어듭니다.

    A:\\[게임]\\Pillowheads
    \tPillowheads.exe\t650752
    \tUnityPlayer.dll\t22904264
    A:\\[게임]\\다른게임
    \tgame.exe\t100000

- 탭으로 시작하지 않는 줄: 폴더(절대 경로)
- 탭(\\t)으로 시작하는 줄: 그 폴더 안의 파일 (이름, 선택적으로 \\t크기)

사용법:
    python list_drive_files.py <대상경로> [-o 출력파일.txt] [--include-hidden] [--with-size] [--no-count] [--flat]

예시:
    # macOS 외장하드 + 크기 + 진행률 (그룹 포맷)
    python list_drive_files.py /Volumes/MyDrive -o files.txt --with-size

    # Windows 외장하드 (카운팅 생략으로 즉시 시작)
    python list_drive_files.py E:\\ -o files.txt --with-size --no-count

    # 기존 flat 포맷이 필요하면 --flat 사용 (호환성 목적)
    python list_drive_files.py /media/user/MyDrive -o files.txt --with-size --flat
"""

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path


# 루트 바로 아래에서 제외할 시스템 폴더 (Windows / macOS / Linux 공통 시스템 메타)
# 대소문자 무관하게 비교.
SYSTEM_FOLDERS_AT_ROOT = {
    # Windows
    "system volume information",
    "$recycle.bin",
    "$recycler",
    "recycler",
    "recycled",
    "found.000",
    "msocache",
    "config.msi",
    # macOS
    ".trashes",
    ".spotlight-v100",
    ".fseventsd",
    ".documentrevisions-v100",
    ".temporaryitems",
    ".apdisk",
    # Linux
    "lost+found",
}


# tqdm이 있으면 사용, 없으면 간단한 폴백
try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False


class SimpleProgress:
    """tqdm이 없을 때 쓰는 최소 진행률 표시기. 1초마다 stderr에 갱신."""
    def __init__(self, total=None, desc="진행", unit="files"):
        self.total = total
        self.desc = desc
        self.unit = unit
        self.n = 0
        self.start = time.time()
        self.last_print = 0.0

    def update(self, k=1):
        self.n += k
        now = time.time()
        if now - self.last_print >= 1.0:
            self.last_print = now
            self._render(now)

    def _render(self, now):
        elapsed = now - self.start
        rate = self.n / elapsed if elapsed > 0 else 0
        if self.total:
            pct = self.n / self.total * 100
            eta = (self.total - self.n) / rate if rate > 0 else 0
            msg = (f"{self.desc}: {self.n:,}/{self.total:,} "
                   f"({pct:5.1f}%) · {rate:,.0f} {self.unit}/s · "
                   f"ETA {self._fmt_time(eta)}")
        else:
            msg = (f"{self.desc}: {self.n:,} {self.unit} · "
                   f"{rate:,.0f} {self.unit}/s · 경과 {self._fmt_time(elapsed)}")
        print(f"\r{msg}", end="", file=sys.stderr, flush=True)

    @staticmethod
    def _fmt_time(seconds):
        if seconds < 60:
            return f"{seconds:.0f}s"
        if seconds < 3600:
            m, s = divmod(int(seconds), 60)
            return f"{m}m{s:02d}s"
        h, rem = divmod(int(seconds), 3600)
        m = rem // 60
        return f"{h}h{m:02d}m"

    def close(self):
        self._render(time.time())
        print("", file=sys.stderr)


def make_progress(total, desc, unit="files"):
    """tqdm이 있으면 tqdm, 없으면 SimpleProgress 반환."""
    if HAS_TQDM:
        return tqdm(
            total=total,
            desc=desc,
            unit=unit,
            unit_scale=False,
            mininterval=0.5,
            file=sys.stderr,
            dynamic_ncols=True,
        )
    return SimpleProgress(total=total, desc=desc, unit=unit)


def is_excluded_at_root(entry_name: str, depth: int) -> bool:
    return depth == 1 and entry_name.lower() in SYSTEM_FOLDERS_AT_ROOT


def scan_count(root: str, include_hidden: bool) -> int:
    """파일 개수만 빠르게 사전 카운팅 (stat 없이)."""
    count = 0
    stack = [(root, 1)]
    progress = make_progress(None, desc="파일 수 계산", unit="entries")
    try:
        while stack:
            path, depth = stack.pop()
            try:
                with os.scandir(path) as it:
                    for entry in it:
                        name = entry.name
                        if not include_hidden and name.startswith("."):
                            continue
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            continue
                        if is_dir:
                            if is_excluded_at_root(name, depth):
                                continue
                            stack.append((entry.path, depth + 1))
                        else:
                            count += 1
                            if count % 1000 == 0:
                                progress.update(1000)
            except (PermissionError, OSError) as e:
                print(f"\n[경고] 스캔 실패 {path}: {e}", file=sys.stderr)
    finally:
        remaining = count - (progress.n if hasattr(progress, "n") else 0)
        if remaining > 0:
            progress.update(remaining)
        progress.close()
    return count


def iter_dirs(root: str, include_hidden: bool, with_size: bool):
    """
    폴더 단위로 (folder_path, [(file_name, size_or_None), ...])를 yield.
    그룹 포맷 쓰기에 최적화된 형태.
    파일이 없는 폴더는 yield하지 않음 (텍스트 파일 크기 절약).
    """
    stack = [(root, 1)]
    while stack:
        path, depth = stack.pop()
        files_in_dir = []
        subdirs = []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    name = entry.name
                    if not include_hidden and name.startswith("."):
                        continue
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError as e:
                        print(f"\n[경고] {entry.path}: {e}", file=sys.stderr)
                        continue
                    if is_dir:
                        if is_excluded_at_root(name, depth):
                            print(f"\n[건너뜀] 시스템 폴더 제외: {name}", file=sys.stderr)
                            continue
                        subdirs.append((entry.path, depth + 1))
                    else:
                        if with_size:
                            try:
                                size = entry.stat(follow_symlinks=False).st_size
                            except OSError as e:
                                print(f"\n[경고] stat 실패 {entry.path}: {e}", file=sys.stderr)
                                size = 0
                            files_in_dir.append((name, size))
                        else:
                            files_in_dir.append((name, None))
        except (PermissionError, OSError) as e:
            print(f"\n[경고] 디렉토리 접근 실패 {path}: {e}", file=sys.stderr)
            continue

        # 자식 폴더는 스택에 추가
        stack.extend(subdirs)
        # 파일이 있을 때만 yield
        if files_in_dir:
            yield path, files_in_dir


def iter_files_flat(root: str, include_hidden: bool, with_size: bool):
    """기존 flat 포맷용. (file_path, size) yield."""
    stack = [(root, 1)]
    while stack:
        path, depth = stack.pop()
        try:
            with os.scandir(path) as it:
                for entry in it:
                    name = entry.name
                    if not include_hidden and name.startswith("."):
                        continue
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError as e:
                        print(f"\n[경고] {entry.path}: {e}", file=sys.stderr)
                        continue
                    if is_dir:
                        if is_excluded_at_root(name, depth):
                            print(f"\n[건너뜀] 시스템 폴더 제외: {name}", file=sys.stderr)
                            continue
                        stack.append((entry.path, depth + 1))
                    else:
                        if with_size:
                            try:
                                size = entry.stat(follow_symlinks=False).st_size
                            except OSError as e:
                                print(f"\n[경고] stat 실패 {entry.path}: {e}", file=sys.stderr)
                                size = 0
                            yield entry.path, size
                        else:
                            yield entry.path, None
        except (PermissionError, OSError) as e:
            print(f"\n[경고] 디렉토리 접근 실패 {path}: {e}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="외장하드 전체 파일 목록을 풀 패스로 추출 (그룹 포맷, 대용량 최적화)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("path", help="탐색할 루트 경로 (예: /Volumes/MyDrive, E:\\)")
    parser.add_argument(
        "-o", "--output",
        default="file_list.txt",
        help="출력 파일 경로 (기본값: file_list.txt) — 자동으로 _YYMMDD_HHMM 추가",
    )
    parser.add_argument("--include-hidden", action="store_true", help="숨김 파일/폴더 포함")
    parser.add_argument("--with-size", action="store_true", help="파일 크기(bytes) 함께 기록")
    parser.add_argument("--no-count", action="store_true", help="사전 카운팅 생략 (즉시 시작)")
    parser.add_argument("--flat", action="store_true",
                        help="기존 flat 포맷으로 저장 (호환성 목적, 파일 크기 증가)")
    parser.add_argument("--buffer-size", type=int, default=10000,
                        help="쓰기 버퍼 크기 (기본: 10000)")
    args = parser.parse_args()

    root = Path(args.path).expanduser().resolve()
    if not root.exists():
        print(f"[에러] 경로가 존재하지 않습니다: {root}", file=sys.stderr)
        return 1
    if not root.is_dir():
        print(f"[에러] 디렉토리가 아닙니다: {root}", file=sys.stderr)
        return 1

    output_path = Path(args.output).expanduser().resolve()
    timestamp = datetime.now().strftime("_%y%m%d_%H%M")
    output_path = output_path.with_name(f"{output_path.stem}{timestamp}{output_path.suffix}")

    fmt = "flat" if args.flat else "grouped"
    print(f"탐색 시작: {root}", file=sys.stderr)
    print(f"출력 파일: {output_path}", file=sys.stderr)
    print(f"옵션: format={fmt}, include_hidden={args.include_hidden}, "
          f"with_size={args.with_size}, no_count={args.no_count}", file=sys.stderr)
    if not HAS_TQDM:
        print("[안내] tqdm이 설치되어 있지 않아 간단 진행률 모드로 동작합니다. "
              "더 나은 표시를 원하면 `pip install tqdm`을 실행하세요.", file=sys.stderr)

    # === 단계 1: 사전 카운팅 (옵션) ===
    total = None
    if not args.no_count:
        t0 = time.time()
        total = scan_count(str(root), args.include_hidden)
        elapsed = time.time() - t0
        print(f"\n파일 수: {total:,} 개 (카운팅 {elapsed:.1f}s 소요)\n", file=sys.stderr)

    # === 단계 2: 실제 기록 ===
    count = 0
    total_size = 0
    t0 = time.time()
    progress = make_progress(total, desc="파일 기록", unit="files")

    # 파일 헤더(첫 줄)에 포맷 표시 — 뷰어가 명시적으로 감지하기 좋게 (선택적)
    header = "# format: grouped, with_size\n" if (not args.flat and args.with_size) else \
             "# format: grouped\n" if not args.flat else \
             "# format: flat, with_size\n" if args.with_size else \
             "# format: flat\n"

    with open(output_path, "w", encoding="utf-8", buffering=1024 * 1024, newline="\n") as f:
        f.write(header)
        buf = []
        buf_limit = args.buffer_size

        try:
            if args.flat:
                # 기존 flat 포맷
                for file_path, size in iter_files_flat(str(root), args.include_hidden, args.with_size):
                    if args.with_size:
                        buf.append(f"{file_path}\t{size}\n")
                        total_size += size
                    else:
                        buf.append(f"{file_path}\n")
                    count += 1
                    if len(buf) >= buf_limit:
                        f.write("".join(buf))
                        buf.clear()
                        progress.update(buf_limit)
            else:
                # 그룹 포맷
                for folder_path, files_in_dir in iter_dirs(str(root), args.include_hidden, args.with_size):
                    buf.append(f"{folder_path}\n")
                    for fname, fsize in files_in_dir:
                        if args.with_size:
                            buf.append(f"\t{fname}\t{fsize}\n")
                            total_size += fsize
                        else:
                            buf.append(f"\t{fname}\n")
                        count += 1
                    if len(buf) >= buf_limit:
                        f.write("".join(buf))
                        buf.clear()
                        # progress.update는 count 기준으로 정확히 못 맞추므로 한꺼번에 update
                        # tqdm의 n 속성을 직접 맞춰주는 게 가장 정확
                        if hasattr(progress, 'n'):
                            delta = count - progress.n
                            if delta > 0:
                                progress.update(delta)

            if buf:
                f.write("".join(buf))
                if hasattr(progress, 'n'):
                    delta = count - progress.n
                    if delta > 0:
                        progress.update(delta)
        except KeyboardInterrupt:
            if buf:
                f.write("".join(buf))
            progress.close()
            print(f"\n[중단] 사용자 중단. 지금까지 {count:,}개 기록됨.", file=sys.stderr)
            return 130

    progress.close()
    elapsed = time.time() - t0
    rate = count / elapsed if elapsed > 0 else 0

    print(f"\n완료: 총 {count:,}개 파일 기록 ({elapsed:.1f}s, {rate:,.0f} files/s)",
          file=sys.stderr)
    if args.with_size:
        print(f"총 용량: {total_size:,} bytes ({total_size / (1024**3):.2f} GiB / "
              f"{total_size / (1024**4):.2f} TiB)", file=sys.stderr)

    out_size = output_path.stat().st_size
    print(f"결과: {output_path} ({out_size:,} bytes / {out_size / (1024**2):.1f} MiB)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
