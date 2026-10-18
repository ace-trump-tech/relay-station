"""Package a verified executable, tracked source snapshot, and technical docs."""
import argparse
import hashlib
import io
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import __version__

RUNTIME_INPUTS = ["app", "static", "assets", "desktop.py", "Relay.spec",
                  "requirements.txt", "requirements-desktop.txt", "requirements-build.txt",
                  "packaging/windows-version.txt", "packaging/THIRD_PARTY_LICENSES.txt",
                  "LICENSE", "NOTICE.txt"]


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-commit", help="Actual executable build commit when reusing an unchanged binary")
    args = parser.parse_args()
    if git("status", "--porcelain").strip():
        parser.error("Commit or resolve tracked/untracked changes before packaging")
    head = git("rev-parse", "HEAD").decode().strip()
    runtime = git("rev-parse", args.runtime_commit or "HEAD").decode().strip()
    if git("diff", runtime, head, "--", *RUNTIME_INPUTS).strip():
        parser.error("Runtime or build inputs changed; rebuild the executable from the current commit")
    if not args.exe.is_file():
        parser.error("Executable does not exist")
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / f"Relay-{__version__}-complete.zip"
    document = args.output / f"Relay-{__version__}-TECHNICAL.md"
    checksum_path = args.output / f"Relay-{__version__}-complete-SHA256SUMS.txt"
    source = git("archive", "--format=zip", "--prefix=relay-station/", head)
    info = (f"Runtime version: {__version__}\nRuntime build commit: {runtime}\n"
            f"Source and documentation commit: {head}\n"
            f"Executable SHA256: {hashlib.sha256(args.exe.read_bytes()).hexdigest()}\n"
            "Runtime build and documentation snapshot may differ only in non-runtime files.\n")
    instructions = """Relay · 完整交付包

双击 Relay.exe 打开，首次创建本地工作区，再按需授权邮箱和 Canvas。
无需 Python、Node.js 或 Docker。内嵌窗口需要 Microsoft WebView2 Runtime。

技术文档：docs/TECHNICAL.md；其他说明和界面图片：docs/。
源码快照：source.zip，解压后按 README.md 运行或重新构建。
构建来源：BUILD_INFO.txt；包内文件校验：CONTENTS-SHA256SUMS.txt。

关闭窗口后留在系统托盘运行，从托盘退出才停止后台同步与提醒。
默认数据目录：%LOCALAPPDATA%\\RelayStation。
升级前退出并备份整个数据目录，保留数据库与 vault.key。
渠道授权需用户自行完成，完整能力和限制见技术文档。

https://github.com/ace-trump-tech/relay-station
"""
    entries = {
        "Relay.exe": args.exe.read_bytes(), "source.zip": source,
        "BUILD_INFO.txt": info.encode(), "使用说明.txt": instructions.encode("utf-8-sig"),
        "README.md": (ROOT / "README.md").read_bytes(),
        "LICENSE": (ROOT / "LICENSE").read_bytes(),
        "NOTICE.txt": (ROOT / "NOTICE.txt").read_bytes(),
        "THIRD_PARTY_LICENSES.txt": (ROOT / "packaging/THIRD_PARTY_LICENSES.txt").read_bytes(),
    }
    for path in sorted((ROOT / "docs").rglob("*")):
        if path.is_file() and path.suffix in (".md", ".png"):
            entries[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    inside = "".join(hashlib.sha256(data).hexdigest()+"  "+name+"\n" for name, data in sorted(entries.items()))
    entries["CONTENTS-SHA256SUMS.txt"] = inside.encode("utf-8")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, data in entries.items():
            bundle.writestr("Relay/"+name, data)
    shutil.copyfile(ROOT / "docs/TECHNICAL.md", document)
    with zipfile.ZipFile(archive) as bundle:
        for name, data in entries.items():
            if bundle.read("Relay/"+name) != data:
                raise RuntimeError("Archive verification failed: "+name)
        with zipfile.ZipFile(io.BytesIO(bundle.read("Relay/source.zip"))) as snapshot:
            if snapshot.read("relay-station/docs/TECHNICAL.md") != document.read_bytes():
                raise RuntimeError("Source/documentation mismatch")
    checksum_path.write_text("".join(hashlib.sha256(p.read_bytes()).hexdigest()+"  "+p.name+"\n"
                                      for p in (archive, document)), encoding="ascii")
    print(f"Created and verified {archive.name}, {document.name}, {checksum_path.name}")


if __name__ == "__main__":
    main()
