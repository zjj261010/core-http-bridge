"""Check the offline Bridge environment in the selected CORE interpreter."""
import argparse
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import platform
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / 'requirements-linux-x86_64-py310.txt'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docker', action='store_true', help='检查 Docker CLI')
    parser.add_argument('--network-tools', action='store_true', help='检查背景流量和 Snort 的宿主工具')
    args = parser.parse_args()
    errors = []
    if sys.version_info[:2] != (3, 10) or platform.system() != 'Linux' or platform.machine() != 'x86_64':
        errors.append('此依赖清单适用于 Linux x86_64 / Python 3.10，请使用对应的 CORE Python 环境。')
    for line in LOCK.read_text(encoding='utf-8').splitlines():
        if not line or line.startswith('#'):
            continue
        spec, expected = line.split('==', 1)
        name = spec.partition('[')[0]
        try:
            installed = version(name)
        except PackageNotFoundError:
            errors.append(f'缺少 {name}=={expected}')
        else:
            if installed != expected:
                errors.append(f'{name} 当前为 {installed}，部署清单要求 {expected}')
    try:
        sys.path.insert(0, str(ROOT))
        from app import SWAGGER_ASSETS
        for name in ('swagger-ui-bundle.js', 'swagger-ui.css', 'favicon-32x32.png'):
            if not ROOT.joinpath(SWAGGER_ASSETS, name).is_file():
                errors.append('缺少离线文档资源：' + name)
        if args.network_tools:
            from network_activity import _missing_tools, _tools
            tools = _tools()
            for kind in ('http', 'iperf3', 'snort'):
                if missing := _missing_tools(kind, tools):
                    errors.append(kind + '：' + missing)
    except ImportError as error:
        errors.append('无法加载 Bridge 或 CORE Python SDK：' + str(error))
    if args.docker and not shutil.which('docker'):
        errors.append('找不到 Docker CLI')
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    print('离线部署自检通过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
