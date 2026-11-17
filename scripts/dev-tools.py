"""Persistent maintenance tooling. Importing this module runs no tests."""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path.home() / '.local' / 'share' / 'core-http-bridge'


def persistent(path: Path) -> Path:
    path = path.expanduser().resolve()
    for temporary in (Path('/tmp'), Path('/var/tmp'), Path('/dev/shm')):
        if path == temporary or temporary in path.parents:
            raise ValueError('维护工具和验收记录必须使用持久目录：' + str(path))
    return path


def directories():
    root = persistent(Path(os.environ.get('CORE_BRIDGE_MAINTENANCE_DIR', str(DEFAULT_ROOT))))
    return {
        'root': root,
        'python_deps': root / 'tools' / 'python-deps',
        'browser_tools': root / 'tools' / 'browser',
        'playwright_module': root / 'tools' / 'browser' / 'node_modules' / 'playwright',
        'browser_binaries': root / 'tools' / 'playwright-browsers',
        'browser_libraries': root / 'tools' / 'browser' / 'runtime' / 'usr' / 'lib' / 'x86_64-linux-gnu',
        'font_config': root / 'tools' / 'browser' / 'fonts.conf',
        'acceptance': root / 'acceptance',
    }


def environment(paths):
    env = dict(os.environ)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHONPATH'] = os.pathsep.join((str(paths['python_deps']), str(PROJECT)))
    env['PLAYWRIGHT_MODULE'] = str(paths['playwright_module'])
    env['PLAYWRIGHT_BROWSERS_PATH'] = str(paths['browser_binaries'])
    if paths['browser_libraries'].is_dir():
        inherited = [item for item in env.get('LD_LIBRARY_PATH', '').split(os.pathsep) if item]
        env['LD_LIBRARY_PATH'] = os.pathsep.join([str(paths['browser_libraries']), *inherited])
    if paths['font_config'].is_file():
        env['FONTCONFIG_FILE'] = str(paths['font_config'])
    return env


def node_executable(paths):
    candidate = shutil.which('node')
    if candidate:
        return str(persistent(Path(candidate)))
    saved = paths['root'] / 'tools' / 'runtime.json'
    if saved.is_file():
        candidate = json.loads(saved.read_text()).get('node')
        if candidate and os.access(candidate, os.X_OK):
            return str(persistent(Path(candidate)))
    return None


def new_run(paths, label):
    paths['acceptance'].mkdir(mode=0o700, parents=True, exist_ok=True)
    if not label or any(character not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for character in label):
        raise ValueError('运行名称只允许字母、数字、连字符和下划线')
    name = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-') + label + '-' + uuid.uuid4().hex[:8]
    # prepare_environment.py creates this directory itself; reserve only the parent.
    return paths['acceptance'] / name


def run_directory(value):
    path = persistent(Path(value))
    if (path / 'ARCHIVED').exists():
        raise ValueError('这是已完成并清理的历史存档，请用 new-run 准备新的验收目录：' + str(path))
    return path


def path_argument(arguments, flag):
    for index, argument in enumerate(arguments):
        if argument == flag and index + 1 < len(arguments):
            return arguments[index + 1]
        if argument.startswith(flag + '='):
            return argument.split('=', 1)[1]
    return None


def execute(command, env):
    os.chdir(PROJECT)
    os.execvpe(command[0], command, env)


def main():
    parser = argparse.ArgumentParser(description='使用持久目录中的项目测试和浏览器工具；不会自动启动验收')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('paths', help='只显示持久路径和文件是否存在，不运行测试')
    fresh = commands.add_parser('new-run', help='输出一个尚未创建的新验收目录路径')
    fresh.add_argument('label', nargs='?', default='acceptance')
    for name in ('python', 'pytest'):
        child = commands.add_parser(name)
        child.add_argument('arguments', nargs=argparse.REMAINDER)
    browser = commands.add_parser('browser')
    browser.add_argument('--run-dir', required=True)
    browser.add_argument('--url', default='http://127.0.0.1:18080')
    browser.add_argument('script')
    browser.add_argument('arguments', nargs=argparse.REMAINDER)
    arguments = sys.argv[1:]
    if arguments and arguments[0] in ('python', 'pytest'):
        forwarded = arguments[1:]
        if forwarded[:1] == ['--']:
            forwarded = forwarded[1:]
        options = argparse.Namespace(command=arguments[0], arguments=forwarded)
    else:
        options = parser.parse_args(arguments)
    try:
        paths = directories()
        if options.command == 'paths':
            values = {name: {'path': str(path), 'exists': path.exists()} for name, path in paths.items()}
            values['python'] = str(Path(os.environ.get('CORE_BRIDGE_PYTHON', '/opt/core/venv/bin/python')))
            values['node'] = node_executable(paths)
            print(json.dumps(values, ensure_ascii=False, indent=2))
            return
        if options.command == 'new-run':
            print(new_run(paths, options.label))
            return
        env = environment(paths)
        if options.command == 'browser':
            directory = run_directory(options.run_dir)
            script = persistent(Path(options.script) if Path(options.script).is_absolute() else PROJECT / options.script)
            if not script.is_file() or not (paths['playwright_module'] / 'package.json').is_file():
                raise ValueError('浏览器脚本或持久 Playwright 依赖不存在，请查看 WSL 持久化维护文档')
            node = node_executable(paths)
            if not node:
                raise ValueError('当前账户 PATH 中没有 Node.js')
            env.update(BRIDGE_TEST_RUN_DIR=str(directory), BRIDGE_TEST_URL=options.url)
            execute([node, str(script), *options.arguments], env)
            return
        python = os.environ.get('CORE_BRIDGE_PYTHON', '/opt/core/venv/bin/python')
        arguments = options.arguments
        requested = path_argument(arguments, '--run-dir')
        if requested:
            run_directory(requested)
        if options.command == 'pytest':
            if not (paths['python_deps'] / 'pytest').is_dir():
                raise ValueError('持久 pytest 依赖不存在，请查看 WSL 持久化维护文档')
            arguments = arguments or ['tests/', '-v']
            report = path_argument(arguments, '--junitxml')
            if report:
                persistent(Path(report))
            else:
                directory = new_run(paths, 'unit')
                directory.mkdir(mode=0o700)
                arguments = [*arguments, '--junitxml=' + str(directory / 'junit.xml')]
            execute([python, '-m', 'pytest', *arguments], env)
        else:
            execute([python, *arguments], env)
    except (ValueError, OSError) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    main()
