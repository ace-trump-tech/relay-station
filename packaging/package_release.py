"""Create a portable user-facing release archive and checksum file."""
import argparse
import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import __version__


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    stem = f"Relay-{__version__}-Windows-x64"
    executable = args.output / (stem + ".exe")
    shutil.copyfile(args.exe, executable)
    archive = args.output / (stem + ".zip")
    instructions = f"""Relay {__version__} · Windows x64

双击 Relay.exe 即可打开，无需 Python、Node.js 或 Docker。
首次启动请创建本地工作区，随后在连接中心授权账号。
Gmail / Agent 客户端信息可在偏好设置的服务配置中填写。
Canvas / 学校作业平台在连接中心使用学校允许的授权令牌接入。
自动拾取的作业先在“我的事项”确认，再配置重要程度、提醒和排期。
课表可按周添加或用 CSV 导入；“遗漏拾取”帮助回顾未落地事项。

关闭主窗口后可留在系统托盘继续同步和提醒。
要完全退出，请从托盘菜单选择“退出并停止提醒”。
默认数据目录：%LOCALAPPDATA%\\RelayStation
更新时替换 exe 即可；不要删除数据目录或 vault.key。

推荐 Windows 10 / 11 x64。内嵌窗口需要 Microsoft WebView2 Runtime；
无法启动内嵌窗口时尝试系统浏览器模式。
发行物未做商业代码签名，请使用随附 SHA256SUMS.txt 校验文件。

使用说明与源码：
https://github.com/ace-trump-tech/relay-station

平台授权仍需用户自行配置。个人微信 / QQ 全量聊天读取不受支持，
企业微信与 QQ 官方机器人适配器尚在扩展计划中。
"""
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        package.write(executable, "Relay/Relay.exe")
        package.write(ROOT / "LICENSE", "Relay/LICENSE")
        package.write(ROOT / "NOTICE.txt", "Relay/NOTICE.txt")
        package.write(ROOT / "packaging/THIRD_PARTY_LICENSES.txt", "Relay/THIRD_PARTY_LICENSES.txt")
        package.writestr("Relay/使用说明.txt", instructions.encode("utf-8-sig"))
    checksums = []
    for path in (executable, archive):
        checksums.append(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name)
    (args.output / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="ascii")
    print(f"Created {executable.name} and {archive.name}")


if __name__ == "__main__":
    main()
