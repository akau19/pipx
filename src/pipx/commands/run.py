import datetime
import hashlib
import logging
import requests
import subprocess
import time
import urllib.parse
import urllib.request

from bs4 import BeautifulSoup
from pathlib import Path
from shutil import which
from typing import List, NoReturn, Optional

from packaging.requirements import InvalidRequirement, Requirement
from packaging import version

from pipx import constants
from pipx.commands.common import package_name_from_spec
from pipx.constants import TEMP_VENV_EXPIRATION_THRESHOLD_DAYS, WINDOWS, PIPX_LOCAL_VENVS
from pipx.emojis import hazard
from pipx.util import (
    PipxError,
    exec_app,
    get_pypackage_bin_path,
    pipx_wrap,
    rmdir,
    run_pypackage_bin,
)
from pipx.venv import Venv

logger = logging.getLogger(__name__)


VENV_EXPIRED_FILENAME = "pipx_expired_venv"

APP_NOT_FOUND_ERROR_MESSAGE = """\
'{app}' executable script not found in package '{package_name}'.
Available executable scripts:
    {app_lines}"""


def maybe_script_content(app: str, is_path: bool) -> Optional[str]:
    # If the app is a script, return its content.
    # Return None if it should be treated as a package name.

    # Look for a local file first.
    app_path = Path(app)
    if app_path.exists():
        return app_path.read_text(encoding="utf-8")
    elif is_path:
        raise PipxError(f"The specified path {app} does not exist")

    # Check for a URL
    if urllib.parse.urlparse(app).scheme:
        if not app.endswith(".py"):
            raise PipxError(
                """
                pipx will only execute apps from the internet directly if they
                end with '.py'. To run from an SVN, try pipx --spec URL BINARY
                """
            )
        logger.info("Detected url. Downloading and executing as a Python file.")

        return _http_get_request(app)

    # Otherwise, it's a package
    return None


def run_script(
    content: str,
    app_args: List[str],
    python: str,
    pip_args: List[str],
    venv_args: List[str],
    verbose: bool,
    use_cache: bool,
) -> NoReturn:
    requirements = _get_requirements_from_script(content)
    if requirements is None:
        exec_app([python, "-c", content, *app_args])
    else:
        # Note that the environment name is based on the identified
        # requirements, and *not* on the script name. This is deliberate, as
        # it ensures that two scripts with the same requirements can use the
        # same environment, which means fewer environments need to be
        # managed. The requirements are normalised (in
        # _get_requirements_from_script), so that irrelevant differences in
        # whitespace, and similar, don't prevent environment sharing.
        venv_dir = _get_temporary_venv_path(requirements, python, pip_args, venv_args)
        venv = Venv(venv_dir)
        _prepare_venv_cache(venv, None, use_cache)
        if venv_dir.exists():
            logger.info(f"Reusing cached venv {venv_dir}")
        else:
            venv = Venv(venv_dir, python=python, verbose=verbose)
            venv.create_venv(venv_args, pip_args)
            venv.install_unmanaged_packages(requirements, pip_args)
        exec_app([venv.python_path, "-c", content, *app_args])


def run_package(
    app: str,
    package_or_url: str,
    app_args: List[str],
    python: str,
    pip_args: List[str],
    venv_args: List[str],
    pypackages: bool,
    verbose: bool,
    use_cache: bool,
) -> NoReturn:
    if which(app):
        logger.warning(
            pipx_wrap(
                f"""
                {hazard}  {app} is already on your PATH and installed at
                {which(app)}. Downloading and running anyway.
                """,
                subsequent_indent=" " * 4,
            )
        )

    if WINDOWS:
        app_filename = f"{app}.exe"
        logger.info(f"Assuming app is {app_filename!r} (Windows only)")
    else:
        app_filename = app

    pypackage_bin_path = get_pypackage_bin_path(app)
    if pypackage_bin_path.exists():
        logger.info(
            f"Using app in local __pypackages__ directory at '{pypackage_bin_path}'"
        )
        run_pypackage_bin(pypackage_bin_path, app_args)
    if pypackages:
        raise PipxError(
            f"""
            '--pypackages' flag was passed, but '{pypackage_bin_path}' was
            not found. See https://github.com/cs01/pythonloc to learn how to
            install here, or omit the flag.
            """
        )

    venv_dir = _get_temporary_venv_path([package_or_url], python, pip_args, venv_args)

    venv = Venv(venv_dir)
    bin_path = venv.bin_path / app_filename
    _prepare_venv_cache(venv, bin_path, use_cache)

    if venv.has_app(app, app_filename):
        logger.info(f"Reusing cached venv {venv_dir}")
        venv.run_app(app, app_filename, app_args)
    else:
        logger.info(f"venv location is {venv_dir}")
        _download_and_run(
            Path(venv_dir),
            package_or_url,
            app,
            app_filename,
            app_args,
            python,
            pip_args,
            venv_args,
            use_cache,
            verbose,
        )


def run(
    app: str,
    spec: str,
    is_path: bool,
    app_args: List[str],
    python: str,
    pip_args: List[str],
    venv_args: List[str],
    pypackages: bool,
    verbose: bool,
    use_cache: bool,
) -> NoReturn:
    """Installs venv to temporary dir (or reuses cache), then runs app from
    package
    """

    check_version(app)
    
    # For any package, we need to just use the name
    try:
        package_name = Requirement(app).name
    except InvalidRequirement:
        # Raw URLs to scripts are supported, too, so continue if
        # we can't parse this as a package
        package_name = app

    content = None if spec is not None else maybe_script_content(app, is_path)
    if content is not None:
        run_script(content, app_args, python, pip_args, venv_args, verbose, use_cache)
    else:
        package_or_url = spec if spec is not None else app
        run_package(
            package_name,
            package_or_url,
            app_args,
            python,
            pip_args,
            venv_args,
            pypackages,
            verbose,
            use_cache,
        )

'''
Constants:
VERSION_CHECK_FILENAME = "pipx_version_check"
VERSION_CHECK_EXPIRATION_THRESHOLD_HOURS = 24
'''
def check_version(app: str):   
    package_venv = PIPX_LOCAL_VENVS / "{app}"
    
    if (package_venv / "pipx_version_check").exists() or _is_version_check_expired(package_venv):        
        # creates new file named "pipx_version_check" if one doesn't exist; overwrites old one if one did exist
        with open(package_venv/"pipx_version_check", "w"):
            pass
        
        latest_version = _get_latest_version(app)

        venv = Venv(package_venv)
        current_version = venv.package_metadata[app].package_version
        
        if version.parse(latest_version) > version.parse(current_version):
           subprocess.run(["pipx", "upgrade", app])
        
def _is_version_check_expired(package_venv: Path) -> bool:
    version_check_file = package_venv / "pipx_version_check"
    created_time_sec = version_check_file.stat().st_ctime
    current_time_sec = time.mktime(datetime.datetime.now().timetuple())
    age = current_time_sec - created_time_sec
    expiration_threshold_sec = 60 * 60 * 24     # 24 hours
    return age > expiration_threshold_sec

def _get_latest_version(package_name):
    pypi_url = f'https://pypi.org/project/{package_name}/'

    try:
        response = requests.get(pypi_url)
        response.raise_for_status()

        html_content = response.text

    except requests.RequestException as e:
        print(f"Error during request: {e}")
        return None

    if html_content:
        # Parse the HTML content using BeautifulSoup
        soup = BeautifulSoup(html_content, 'html.parser')
    
        # Extract the latest version from the HTML
        version_element = soup.select_one('.package-header__name')
    
        if version_element:
            # Extract the version from the h1 element text
            version_text = version_element.text.strip()
            # Split the text and get the last part, assuming version is at the end
            latest_version = version_text.split()[-1]
            print(f"The latest version of {package_name} is: {latest_version}")
        else:
            print(f"Unable to find version information on the PyPI page for {package_name}.")
    else:
        print(f"Unable to retrieve HTML content for {package_name}.")

def _download_and_run(
    venv_dir: Path,
    package_or_url: str,
    app: str,
    app_filename: str,
    app_args: List[str],
    python: str,
    pip_args: List[str],
    venv_args: List[str],
    use_cache: bool,
    verbose: bool,
) -> NoReturn:
    venv = Venv(venv_dir, python=python, verbose=verbose)
    venv.create_venv(venv_args, pip_args)

    if venv.pipx_metadata.main_package.package is not None:
        package_name = venv.pipx_metadata.main_package.package
    else:
        package_name = package_name_from_spec(
            package_or_url, python, pip_args=pip_args, verbose=verbose
        )

    venv.install_package(
        package_name=package_name,
        package_or_url=package_or_url,
        pip_args=pip_args,
        include_dependencies=False,
        include_apps=True,
        is_main_package=True,
    )

    if not venv.has_app(app, app_filename):
        apps = venv.pipx_metadata.main_package.apps

        # If there's a single app inside the package, run that by default
        if app == package_name and len(apps) == 1:
            app = apps[0]
            print(f"NOTE: running app {app!r} from {package_name!r}")
            if WINDOWS:
                app_filename = f"{app}.exe"
                logger.info(f"Assuming app is {app_filename!r} (Windows only)")
            else:
                app_filename = app
        else:
            all_apps = (
                f"{a} - usage: 'pipx run --spec {package_or_url} {a} [arguments?]'"
                for a in apps
            )
            raise PipxError(
                APP_NOT_FOUND_ERROR_MESSAGE.format(
                    app=app,
                    package_name=package_name,
                    app_lines="\n    ".join(all_apps),
                ),
                wrap_message=False,
            )

    if not use_cache:
        # Let future _remove_all_expired_venvs know to remove this
        (venv_dir / VENV_EXPIRED_FILENAME).touch()

    venv.run_app(app, app_filename, app_args)


def _get_temporary_venv_path(
    requirements: List[str], python: str, pip_args: List[str], venv_args: List[str]
) -> Path:
    """Computes deterministic path using hashing function on arguments relevant
    to virtual environment's end state. Arguments used should result in idempotent
    virtual environment. (i.e. args passed to app aren't relevant, but args
    passed to venv creation are.)
    """
    m = hashlib.sha256()
    m.update("".join(requirements).encode())
    m.update(python.encode())
    m.update("".join(pip_args).encode())
    m.update("".join(venv_args).encode())
    venv_folder_name = m.hexdigest()[:15]  # 15 chosen arbitrarily
    return Path(constants.PIPX_VENV_CACHEDIR) / venv_folder_name


def _is_temporary_venv_expired(venv_dir: Path) -> bool:
    created_time_sec = venv_dir.stat().st_ctime
    current_time_sec = time.mktime(datetime.datetime.now().timetuple())
    age = current_time_sec - created_time_sec
    expiration_threshold_sec = 60 * 60 * 24 * TEMP_VENV_EXPIRATION_THRESHOLD_DAYS
    return age > expiration_threshold_sec or (venv_dir / VENV_EXPIRED_FILENAME).exists()


def _prepare_venv_cache(venv: Venv, bin_path: Optional[Path], use_cache: bool) -> None:
    venv_dir = venv.root
    if not use_cache and (bin_path is None or bin_path.exists()):
        logger.info(f"Removing cached venv {str(venv_dir)}")
        rmdir(venv_dir)
    _remove_all_expired_venvs()


def _remove_all_expired_venvs() -> None:
    for venv_dir in Path(constants.PIPX_VENV_CACHEDIR).iterdir():
        if _is_temporary_venv_expired(venv_dir):
            logger.info(f"Removing expired venv {str(venv_dir)}")
            rmdir(venv_dir)


def _http_get_request(url: str) -> str:
    try:
        res = urllib.request.urlopen(url)
        charset = res.headers.get_content_charset() or "utf-8"
        return res.read().decode(charset)
    except Exception as e:
        logger.debug("Uncaught Exception:", exc_info=True)
        raise PipxError(str(e)) from e


def _get_requirements_from_script(content: str) -> Optional[List[str]]:
    # An iterator over the lines in the script. We will
    # read through this in sections, so it needs to be an
    # iterator, not just a list.
    lines = iter(content.splitlines())

    for line in lines:
        if not line.startswith("#"):
            continue
        line_content = line[1:].strip()
        if line_content == "Requirements:":
            break
    else:
        # No "Requirements:" line in the file
        return None

    # We are now at the first requirement
    requirements = []
    for line in lines:
        # Stop at the end of the comment block
        if not line.startswith("#"):
            break
        line_content = line[1:].strip()
        # Stop at a blank comment line
        if not line_content:
            break

        # Validate the requirement
        try:
            req = Requirement(line_content)
        except InvalidRequirement as e:
            raise PipxError(f"Invalid requirement {line_content}: {str(e)}") from e

        # Use the normalised form of the requirement,
        # not the original line.
        requirements.append(str(req))

    return requirements
