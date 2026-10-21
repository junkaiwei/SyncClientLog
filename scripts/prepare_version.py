"""Apply RELEASE_TAG to build-local metadata; never commit the change."""
from pathlib import Path
import os
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from logcatch.version import get_version, version_from_tag


def prepare_version(tag, project_path):
    version = version_from_tag(tag)
    project_path = Path(project_path)
    get_version(project_path)
    text = project_path.read_text(encoding='utf-8')
    # Restrict replacement to [project], without reserializing other TOML fields.
    match = re.search(r'(?ms)^\[project\]\s*\n(?P<body>.*?)(?=^\[|\Z)', text)
    if match is None:
        raise ValueError('缺少 [project]')
    body, count = re.subn(r'(?m)^(version\s*=\s*)["\'][^"\']*["\']',
                         lambda m: f'{m.group(1)}"{version}"', match['body'])
    if count != 1:
        raise ValueError('必须存在唯一的 project.version')
    text = text[:match.start('body')] + body + text[match.end('body'):]
    project_path.write_text(text, encoding='utf-8')
    return get_version(project_path)


if __name__ == '__main__':
    tag = os.environ.get('RELEASE_TAG')
    if not tag:
        raise SystemExit('缺少 RELEASE_TAG')
    try:
        print(prepare_version(tag, ROOT / 'pyproject.toml'))
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc))
