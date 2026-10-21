"""Read the same project metadata from source and PyInstaller resources."""
from pathlib import Path
import re
import sys
import tomllib

# Use canonical PEP 440 release/pre-release syntax for metadata and release tags.
VERSION_PATTERN = re.compile(r'(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)'
                             r'(?:(?:a|b|rc)(?:0|[1-9]\d*))?'
                             r'(?:\.post(?:0|[1-9]\d*))?'
                             r'(?:\.dev(?:0|[1-9]\d*))?')


def validate_version(value):
    if not isinstance(value, str) or not VERSION_PATTERN.fullmatch(value):
        raise ValueError(f'无效版本号: {value!r}；应为 3.1.0、3.1.0rc1 等规范格式')
    return value


def version_from_tag(tag):
    return validate_version(tag[1:] if tag.startswith(('v', 'V')) else tag)


def metadata_path():
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS) / 'pyproject.toml'
    return Path(__file__).resolve().parent.parent / 'pyproject.toml'


def get_version(project_path=None):
    path = Path(project_path) if project_path is not None else metadata_path()
    try:
        with path.open('rb') as file:
            return validate_version(tomllib.load(file)['project']['version'])
    except (OSError, KeyError, ValueError) as exc:
        raise RuntimeError(f'无法读取项目版本 ({path}): {exc}') from exc
