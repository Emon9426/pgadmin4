##########################################################################
#
# EM_PGAdmin portable (green) package builder
#
# Builds EM_PGAdmin_<version>.zip - an unzip-and-run distribution that
# launches the pgAdmin desktop client (Electron runtime) by default, with
# the same layout as the official installers:
#
#   python/   embedded CPython + pip dependencies
#   web/      pgAdmin web application (Flask backend served on localhost)
#   runtime/  Electron shell (pgAdmin4.exe) + resources/app
#
# A secondary launcher keeps the browser (web) mode available.
#
# Usage:
#   python tools/em/build_portable.py --version 9.18-em1.1.0
#
##########################################################################

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

PYTHON_EMBED_URL = (
    'https://www.python.org/ftp/python/3.13.3/'
    'python-3.13.3-embed-amd64.zip'
)
GET_PIP_URL = 'https://bootstrap.pypa.io/get-pip.py'

# Electron version is pinned to the one resolved in runtime/yarn.lock.
ELECTRON_VERSION = '44.4.3'
ELECTRON_ZIP = 'electron-v{0}-win32-x64.zip'.format(ELECTRON_VERSION)
# npmmirror first (fast in CN networks), official GitHub as fallback.
ELECTRON_URLS = [
    'https://npmmirror.com/mirrors/electron/{0}/{1}'.format(
        ELECTRON_VERSION, ELECTRON_ZIP),
    'https://cdn.npmmirror.com/binaries/electron/{0}/{1}'.format(
        ELECTRON_VERSION, ELECTRON_ZIP),
    'https://github.com/electron/electron/releases/download/v{0}/{1}'.format(
        ELECTRON_VERSION, ELECTRON_ZIP),
]
RCEDIT_URLS = [
    'https://npmmirror.com/mirrors/rcedit/v2.0.0/rcedit-x64.exe',
    'https://github.com/electron/rcedit/releases/download/v2.0.0/'
    'rcedit-x64.exe',
]
# Yarn 4 corepack shims (see docs/em build notes); packageManager in
# runtime/package.json makes corepack pick the right yarn version.
COREPACK_BIN_DIR = r'C:\Users\Emon\AppData\Roaming\npm-corepack'

# Directories / patterns not shipped in the portable package.
WEB_EXCLUDE_DIRS = {
    'node_modules', 'regression', '.yarn', '.coverage', 'htmlcov',
    'tools', '.scannerwork', 'pgadmin.static'
}
WEB_EXCLUDE_NAMES = {
    '.gitignore', '.gitattributes', '.eslintrc.js', '.stylelintrc.json',
    'yarn.lock', 'package.json', 'package-lock.json', '.yarnrc.yml',
    'webpack.config.js', 'webpack.shim.js', 'jest.config.js',
    '.babelrc', '.flake8', '.percy.yml', 'tsconfig.json',
    'vitest.config.ts', 'codecov.yml',
}
# Test directories anywhere inside the tree
EXCLUDE_SUBDIR_NAMES = {'tests', '__pycache__', 'node_modules', 'test'}


def download(urls, dest):
    if isinstance(urls, str):
        urls = [urls]
    if os.path.exists(dest):
        print('Using cached download: {0}'.format(dest))
        return
    last_err = None
    for url in urls:
        try:
            print('Downloading {0} ...'.format(url))
            with urllib.request.urlopen(url) as resp, open(dest, 'wb') as f:
                shutil.copyfileobj(resp, f)
            return
        except Exception as e:
            print('  failed: {0}'.format(e))
            last_err = e
            if os.path.exists(dest):
                os.remove(dest)
    raise RuntimeError('All download mirrors failed for {0}'.format(dest))


def copy_web_sources(src_web, dst_web):
    # NOTE: ignore_patterns match at ANY depth - do not add patterns that
    # collide with package directories inside pgadmin/ (e.g. 'tools'
    # would drop pgadmin/tools).
    shutil.copytree(
        src_web, dst_web,
        ignore=shutil.ignore_patterns(
            'node_modules', 'regression', '__pycache__', '*.pyc',
            '.yarn', '.git*', 'htmlcov', '.scannerwork',
            'yarn.lock', 'package.json', 'jest.config.js',
            'webpack.config.js', 'webpack.shim.js', 'tsconfig.json',
            'vitest.config.ts', '.babelrc', '.stylelintrc.json',
            '.eslintrc.js', '.percy.yml', 'codecov.yml',
            '.coverage', '.nyc_output',
        ))


def setup_runtime(runtime_dir, cache_dir, requirements, extra_reqs=None):
    embed_zip = os.path.join(cache_dir, 'python-embed.zip')
    download(PYTHON_EMBED_URL, embed_zip)

    print('Extracting embedded Python into {0} ...'.format(runtime_dir))
    with zipfile.ZipFile(embed_zip) as zf:
        zf.extractall(runtime_dir)

    # Enable site-packages and a Lib directory next to the interpreter.
    pth_files = [f for f in os.listdir(runtime_dir) if f.endswith('._pth')]
    pth_path = os.path.join(runtime_dir, pth_files[0])
    with open(pth_path, 'w') as f:
        f.write('python313.zip\n')
        f.write('.\n')
        f.write('Lib\\site-packages\n')
        f.write('import site\n')

    get_pip = os.path.join(cache_dir, 'get-pip.py')
    download(GET_PIP_URL, get_pip)

    python_exe = os.path.join(runtime_dir, 'python.exe')

    # The ._pth enables site-packages, which also picks up the *user*
    # site-packages of whatever machine runs the build - pip would then
    # treat those as satisfied and skip installing them into the package
    # (missing on target machines). Force pip to only see the embedded
    # interpreter's own site-packages.
    pip_env = os.environ.copy()
    pip_env['PYTHONNOUSERSITE'] = '1'

    subprocess.check_call([
        python_exe, get_pip, '--no-warn-script-location', '--quiet'
    ], env=pip_env)

    reqs = [requirements]
    if extra_reqs:
        reqs.extend(extra_reqs)
    for req in reqs:
        subprocess.check_call([
            python_exe, '-m', 'pip', 'install', '-r', req,
            '--no-warn-script-location', '--disable-pip-version-check',
        ], env=pip_env)


CONFIG_LOCAL = '''\
# EM_PGAdmin portable configuration - keeps everything inside the
# package folder. Generated by tools/em/build_portable.py.
import os

_WEB_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(os.path.dirname(_WEB_DIR), 'data')
os.makedirs(_DATA_DIR, exist_ok=True)

SERVER_MODE = False
DEFAULT_SERVER = '127.0.0.1'
DEFAULT_SERVER_PORT = 5050

DATA_DIR = _DATA_DIR
SQLITE_PATH = os.path.join(_DATA_DIR, 'pgadmin.db')
LOG_FILE = os.path.join(_DATA_DIR, 'pgadmin.log')
SESSION_DB_PATH = os.path.join(_DATA_DIR, 'sessions')
STORAGE_DIR = os.path.join(_DATA_DIR, 'storage')
AZURE_CREDENTIAL_CACHE_DIR = os.path.join(_DATA_DIR, 'azure')
KERBEROS_CCACHE_DIR = os.path.join(_DATA_DIR, 'kerberos')

UPGRADE_CHECK_ENABLED = False
'''

START_BAT = '''\
@echo off
rem EM_PGAdmin desktop launcher - starts the Electron desktop client.
start "" "%~dp0runtime\\pgAdmin4.exe"
'''

START_WEB_BAT = '''\
@echo off
rem EM_PGAdmin web-mode launcher - starts the local server and opens the
rem default browser. The desktop client is the default; use this only if
rem the desktop client cannot run on your machine.
setlocal
cd /d "%~dp0web"

if not exist "..\\data" mkdir "..\\data"

start "EM_PGAdmin" /min "..\\python\\python.exe" -s "%~dp0web\\pgAdmin4.py"

rem Wait for the server to accept connections, then open the browser.
powershell -NoProfile -Command "$opened=$false; for ($i=0; $i -lt 120; $i++) { try { $c = New-Object Net.Sockets.TcpClient('127.0.0.1', 5050); $c.Close(); $opened=$true; break } catch { Start-Sleep -Milliseconds 500 } }; if ($opened) { Start-Process 'http://127.0.0.1:5050' } else { Write-Host 'pgAdmin failed to start within 60 seconds. Check data\\pgadmin.log' }"
endlocal
'''

STOP_BAT = '''\
@echo off
rem Stops the EM_PGAdmin desktop client and its backend server.
taskkill /IM pgAdmin4.exe /T >nul 2>&1
taskkill /FI "WINDOWTITLE eq EM_PGAdmin*" /T >nul 2>&1
echo EM_PGAdmin stopped.
pause
'''


def setup_electron(repo, runtime_dir, cache_dir, version):
    """Stage the Electron shell exactly like the official installer:
    runtime/resources/app holds the shell sources + production
    node_modules, and the stock Electron distribution is unpacked next to
    it with electron.exe renamed to pgAdmin4.exe."""
    src_runtime = os.path.join(repo, 'runtime')
    app_dir = os.path.join(runtime_dir, 'resources', 'app')

    print('== Staging Electron app sources ==')
    os.makedirs(app_dir)
    for name in ('assets', 'src'):
        shutil.copytree(os.path.join(src_runtime, name),
                        os.path.join(app_dir, name))
    for name in ('package.json', '.yarnrc.yml'):
        shutil.copy2(os.path.join(src_runtime, name),
                     os.path.join(app_dir, name))

    print('== Installing production JS dependencies ==')
    env = os.environ.copy()
    env['PATH'] = COREPACK_BIN_DIR + os.pathsep + env.get('PATH', '')
    # Mirror the official Make.bat: install without a lockfile, prod deps
    # only (skips the electron npm package and its binary download).
    subprocess.check_call('yarn workspaces focus --production',
                          cwd=app_dir, env=env, shell=True)
    shutil.rmtree(os.path.join(app_dir, '.yarn'), ignore_errors=True)

    print('== Unpacking Electron distribution ==')
    electron_zip = os.path.join(cache_dir, ELECTRON_ZIP)
    download(ELECTRON_URLS, electron_zip)
    with zipfile.ZipFile(electron_zip) as zf:
        zf.extractall(runtime_dir)
    os.rename(os.path.join(runtime_dir, 'electron.exe'),
              os.path.join(runtime_dir, 'pgAdmin4.exe'))

    print('== Applying icon and version info ==')
    rcedit = os.path.join(cache_dir, 'rcedit-x64.exe')
    try:
        download(RCEDIT_URLS, rcedit)
        icon = os.path.join(repo, 'pkg', 'win32', 'Resources', 'pgAdmin4.ico')
        subprocess.check_call([
            rcedit, os.path.join(runtime_dir, 'pgAdmin4.exe'),
            '--set-icon', icon,
            '--set-version-string', 'FileDescription', 'EM_PGAdmin',
            '--set-version-string', 'ProductName', 'EM_PGAdmin',
            '--set-product-version', version,
        ])
    except Exception as e:
        print('WARNING: rcedit step failed ({0}); the exe keeps the stock '
              'Electron icon/metadata. Continuing...'.format(e))


def zip_dir(src_dir, zip_path):
    print('Creating {0} ...'.format(zip_path))
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED,
                         compresslevel=9) as zf:
        for root, _dirs, files in os.walk(src_dir):
            for file in files:
                full = os.path.join(root, file)
                rel = os.path.relpath(full, os.path.dirname(src_dir))
                zf.write(full, rel)
    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print('Done: {0} ({1:.1f} MB)'.format(zip_path, size_mb))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', required=True,
                        help='EM version, e.g. 9.18-em1.0.0')
    parser.add_argument('--repo', default=os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    parser.add_argument('--out-dir', default=None)
    parser.add_argument('--cache-dir', default=None)
    parser.add_argument('--skip-zip', action='store_true')
    args = parser.parse_args()

    repo = os.path.abspath(args.repo)
    # Finished zips land in <repo>/release (gitignored) so they are ready
    # to attach to a GitHub Release; staging and the download cache stay
    # out of the way under dist/.
    out_dir = os.path.abspath(args.out_dir or os.path.join(repo, 'release'))
    cache_dir = os.path.abspath(
        args.cache_dir or os.path.join(repo, 'dist', 'cache'))

    pkg_name = 'EM_PGAdmin_{0}'.format(args.version)
    staging = os.path.join(out_dir, pkg_name)

    if os.path.exists(staging):
        print('Removing existing staging dir: {0}'.format(staging))
        shutil.rmtree(staging)
    os.makedirs(staging)
    os.makedirs(cache_dir, exist_ok=True)

    print('== Copying web sources ==')
    copy_web_sources(os.path.join(repo, 'web'), os.path.join(staging, 'web'))

    print('== Setting up embedded Python runtime ==')
    setup_runtime(os.path.join(staging, 'python'), cache_dir,
                  os.path.join(repo, 'requirements.txt'))

    setup_electron(repo, os.path.join(staging, 'runtime'), cache_dir,
                   args.version)

    print('== Writing portable config and launchers ==')
    with open(os.path.join(staging, 'web', 'config_local.py'), 'w',
              encoding='utf-8') as f:
        f.write(CONFIG_LOCAL)
    with open(os.path.join(staging, '启动pgAdmin.bat'), 'w',
              encoding='gbk') as f:
        f.write(START_BAT)
    with open(os.path.join(staging, '启动pgAdmin-网页模式.bat'), 'w',
              encoding='gbk') as f:
        f.write(START_WEB_BAT)
    with open(os.path.join(staging, '停止pgAdmin.bat'), 'w',
              encoding='gbk') as f:
        f.write(STOP_BAT)

    for extra in ('EM_README.md', 'LICENSE'):
        src = os.path.join(repo, extra)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(staging, extra))

    os.makedirs(os.path.join(staging, 'data'), exist_ok=True)

    if not args.skip_zip:
        zip_dir(staging, os.path.join(out_dir, pkg_name + '.zip'))

    print('Build complete.')


if __name__ == '__main__':
    sys.exit(main())
