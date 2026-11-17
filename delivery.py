"""Delivery validation and non-destructive service installation."""
import ast
import copy
import hashlib
import json
import re
import os
import tempfile
import shutil
from pathlib import Path


def manifest(value):
    if not isinstance(value, dict):
        raise ValueError('manifest 必须是对象')
    m = copy.deepcopy(value)
    if not isinstance(m.get('name'), str) or not m['name'].strip():
        raise ValueError('manifest.name 必填')
    m['name'] = m['name'].strip()
    if not re.fullmatch(r'[a-zA-Z0-9_-]+',m['name']):
        raise ValueError('清单名仅支持字母、数字、连字符和下划线，须与服务包目录一致')
    image = m.get('image')
    if not isinstance(image, dict) or not isinstance(image.get('name'), str) or not image['name'].strip():
        raise ValueError('manifest.image 必须包含镜像 name 和 tag')
    image['name'] = image['name'].strip()
    image.setdefault('tag', 'latest')
    if not isinstance(image['tag'], str) or not image['tag']:
        raise ValueError('image.tag 必须是非空字符串')
    ports = image.get('ports', [])
    if not isinstance(ports, list) or any(not isinstance(p, int) or isinstance(p, bool) or not 1 <= p <= 65535 for p in ports):
        raise ValueError('image.ports 必须是 1～65535 的整数列表')
    services = m.setdefault('services', [])
    if not isinstance(services, list):
        raise ValueError('services 必须是列表')
    seen = set()
    for s in services:
        if not isinstance(s, dict) or not isinstance(s.get('name'), str) or not s['name']:
            raise ValueError('每个服务必须包含 name')
        if s['name'] in seen:
            raise ValueError('重复服务: '+s['name'])
        seen.add(s['name'])
        if 'ports' in s and (not isinstance(s['ports'],list) or any(not isinstance(p,int) or isinstance(p,bool) or not 1<=p<=65535 for p in s['ports'])):
            raise ValueError('服务 ports 必须是 1～65535 的整数列表')
        if 'software' in s and not isinstance(s['software'],str):
            raise ValueError('服务 software 必须是字符串')
        if m.get('schema_version', 1) < 3 and s.get('mode') not in (None, '', 'normal', 'vuln'):
            raise ValueError('服务 mode 仅支持 normal、vuln 或单模式（留空）')
    return m


def declarations(source):
    tree = ast.parse(source or '')
    out = []
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for n in cls.body:
            targets = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, ast.AnnAssign) else []
            if any(isinstance(t, ast.Name) and t.id == 'name' for t in targets) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
                out.append((n.value, n.value.value))
    return tree, out


def names(source):
    return [name for _, name in declarations(source)[1]]


def rewrite(source, mapping):
    tree, decls = declarations(source)
    # Also rewrite explicit string references (dependencies and template selectors).
    class Rename(ast.NodeTransformer):
        def visit_Constant(self, node):
            if isinstance(node.value, str) and node.value in mapping:
                return ast.copy_location(ast.Constant(mapping[node.value]), node)
            return node
    return ast.unparse(Rename().visit(tree))+'\n'


def install(directory, package, sources, templates, prefix):
    root = Path(directory)
    package = re.sub(r'[^a-zA-Z0-9_-]', '-', package).strip('-')
    if not package:
        raise ValueError('服务包名必填')
    all_names = [n for source in sources.values() for n in names(source) if n]
    if len(set(all_names)) != len(all_names):
        raise ValueError('本次提交内有重复服务定义')
    mapping = {n: prefix(n, package) for n in all_names}
    rewritten = {fn: rewrite(source, mapping) for fn, source in sources.items() if names(source)}
    owners = {}
    for path in root.glob('**/*.py'):
        try:
            for n in names(path.read_text()):
                owners[n] = path
        except (SyntaxError, OSError):
            continue
    # Do not overwrite a file which also contains definitions absent in this submission.
    for source in rewritten.values():
        for n in names(source):
            owner = owners.get(n)
            if owner and owner.relative_to(root).parts[0] != package:
                raise ValueError(f'服务 {n} 已在包 {owner.relative_to(root).parts[0]} 中定义')
    original=root/package
    root.mkdir(parents=True,exist_ok=True)
    stage_parent=Path(tempfile.mkdtemp(prefix='.delivery-stage-',dir=root.parent))
    dst=stage_parent/package
    if original.exists(): shutil.copytree(original,dst)
    else: dst.mkdir()
    try:
        replacements=set(mapping.values())
        for path in dst.glob('*.py'):
            tree,decls=declarations(path.read_text())
            if any(n in replacements for _,n in decls):
                tree.body=[cls for cls in tree.body if not (isinstance(cls,ast.ClassDef) and any(n in replacements for _,n in declarations(ast.unparse(cls))[1]))]
                path.write_text(ast.unparse(tree)+'\n')
        for source in rewritten.values():
            key=hashlib.sha256(','.join(sorted(names(source))).encode()).hexdigest()[:16]
            (dst/f'services_{key}.py').write_text(source)
        (dst/'__init__.py').touch();(dst/'templates').mkdir(exist_ok=True)
        for fn,text in templates.items(): (dst/'templates'/Path(fn).name).write_text(text)
        backup=stage_parent/'previous'
        if original.exists(): os.replace(original,backup)
        try: os.replace(dst,original)
        except BaseException:
            if backup.exists(): os.replace(backup,original)
            raise
    except BaseException:
        shutil.rmtree(stage_parent,ignore_errors=True)
        raise
    import database,platform_db
    def rollback():
        shutil.rmtree(original,ignore_errors=True)
        if backup.exists(): os.replace(backup,original)
        shutil.rmtree(stage_parent,ignore_errors=True)
    database.on_rollback(platform_db.DB_PATH,rollback)
    database.on_commit(platform_db.DB_PATH,lambda:shutil.rmtree(stage_parent,ignore_errors=True))
    return mapping,original
