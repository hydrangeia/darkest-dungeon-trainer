"""Build a small, isolated CPython/Tcl-Tk runtime for the trainer.

This script only owns dist/runtime, dist/licenses, the launcher executable,
and the runtime manifest. It intentionally preserves dist/app so the main
agent can populate the application files after the runtime is prepared.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_PYTHON_ROOT = Path(sys.base_prefix)
DIST_DIR = PROJECT_ROOT / "dist"
LAUNCHER_SOURCE = PROJECT_ROOT / "Launcher.cs"
LAUNCHER_OUTPUT = DIST_DIR / "暗黑地牢测试修改器.exe"
MANIFEST_PATH = DIST_DIR / "runtime_manifest.json"
RUNTIME_LIMIT = 40 * 1024 * 1024

CORE_FILES = (
    "python.exe",
    "pythonw.exe",
    "python3.dll",
    "python313.dll",
    "zlib.dll",
    "ucrtbase.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
)
EXTENSION_FILES = (
    "_tkinter.pyd",
    "_ctypes.pyd",
    "_hashlib.pyd",
    "_socket.pyd",
    "_bz2.pyd",
    "_lzma.pyd",
    "pyexpat.pyd",
    "select.pyd",
    "unicodedata.pyd",
)
EXCLUDED_STDLIB_PARTS = {
    "site-packages", "test", "tests", "__pycache__", "idlelib", "turtledemo", "ensurepip", "venv",
}
LICENSE_FALLBACKS = {
    # The installed expat 2.7.3 license is MIT; the local package cache has
    # later expat builds with the identical upstream COPYING notice.
    "expat": "expat-*",
}


class PackageError(RuntimeError):
    pass


def _load_pefile() -> Any:
    dependency_dir = PROJECT_ROOT / ".deps"
    sys.dont_write_bytecode = True
    if dependency_dir.is_dir():
        sys.path.insert(0, str(dependency_dir))
    try:
        import pefile  # type: ignore[import-not-found]
    except Exception as exc:
        raise PackageError(f"无法加载工作区内置 PE 依赖分析器 pefile：{exc}") from exc
    return pefile


def _safe_owned_target(path: Path, dist: Path, allowed: set[str]) -> None:
    resolved_dist = dist.resolve()
    resolved = path.resolve()
    if resolved.parent != resolved_dist or path.name not in allowed:
        raise PackageError(f"拒绝清理 dist 外部或未登记的路径：{path}")


def _clear_owned_outputs(dist: Path) -> None:
    allowed_dirs = {"runtime", "licenses"}
    for name in allowed_dirs:
        target = dist / name
        if target.exists() or target.is_symlink():
            _safe_owned_target(target, dist, allowed_dirs)
            if target.is_symlink() or target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target)
    allowed_files = {LAUNCHER_OUTPUT.name, MANIFEST_PATH.name}
    for name in allowed_files:
        target = dist / name
        if target.exists() or target.is_symlink():
            _safe_owned_target(target, dist, allowed_files)
            target.unlink()


def _find_source_file(name: str, search_dirs: tuple[Path, ...]) -> Path | None:
    wanted = name.casefold()
    for directory in search_dirs:
        direct = directory / name
        if direct.is_file():
            return direct
        try:
            for entry in directory.iterdir():
                if entry.is_file() and entry.name.casefold() == wanted:
                    return entry
        except OSError:
            continue
    return None


def _is_windows_system_import(name: str, system_dir: Path) -> bool:
    lowered = name.casefold()
    if lowered.startswith(("api-ms-win-", "ext-ms-win-")):
        return True
    return (system_dir / name).is_file()


def _pe_imports(pefile: Any, path: Path) -> list[str]:
    try:
        image = pefile.PE(str(path), fast_load=True)
        directories = [pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
        delay_directory = pefile.DIRECTORY_ENTRY.get("IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT")
        if delay_directory is not None:
            directories.append(delay_directory)
        image.parse_data_directories(directories=directories)
    except Exception as exc:
        raise PackageError(f"PE 依赖分析失败：{path.name}: {exc}") from exc

    names: set[str] = set()
    for attribute in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
        for item in getattr(image, attribute, ()) or ():
            raw_name = getattr(item, "dll", None)
            if raw_name:
                names.add(raw_name.decode("ascii", "replace"))
    return sorted(names, key=str.casefold)


def _copy_native_closure(source_root: Path, runtime: Path, pefile: Any) -> tuple[list[Path], dict[str, list[str]]]:
    dll_dir = runtime / "DLLs"
    dll_dir.mkdir(parents=True, exist_ok=True)
    search_dirs = (source_root, source_root / "DLLs", source_root / "Library" / "bin", source_root / "Library" / "lib")
    system_root = Path(os.environ.get("WINDIR", r"C:\Windows"))
    system_dir = system_root / "System32"

    queue: list[Path] = []
    copied_sources: dict[str, Path] = {}
    source_by_output_name: dict[str, Path] = {}
    import_manifest: dict[str, list[str]] = {}

    def add_native(source: Path, *, extension: bool = False) -> Path:
        source = source.resolve()
        key = source.name.casefold()
        if key in source_by_output_name:
            existing_source = source_by_output_name[key]
            if existing_source != source and existing_source.stat().st_size != source.stat().st_size:
                raise PackageError(f"运行库包含同名但内容不同的 DLL：{existing_source} 和 {source}")
            return (dll_dir if source.suffix.casefold() == ".pyd" or extension else runtime) / source.name

        destination = (dll_dir if source.suffix.casefold() == ".pyd" or extension else runtime) / source.name
        if not destination.exists():
            shutil.copy2(source, destination)
        source_by_output_name[key] = source
        copied_sources[str(source).casefold()] = source
        queue.append(source)
        return destination

    for name in CORE_FILES:
        source = _find_source_file(name, search_dirs)
        if source is None:
            if name == "vcruntime140_1.dll":
                continue
            raise PackageError(f"Anaconda Python 目录缺少运行文件：{name}")
        add_native(source)

    for name in EXTENSION_FILES:
        source = _find_source_file(name, search_dirs)
        if source is None:
            raise PackageError(f"Anaconda Python DLLs 目录缺少扩展模块：{name}")
        add_native(source, extension=True)

    scanned: set[str] = set()
    while queue:
        source = queue.pop(0)
        source_key = str(source).casefold()
        if source_key in scanned:
            continue
        scanned.add(source_key)
        imports = _pe_imports(pefile, source)
        import_manifest[source.name] = imports
        for imported_name in imports:
            if imported_name.casefold() in source_by_output_name:
                continue
            lowered_import = imported_name.casefold()
            if lowered_import.startswith(("api-ms-win-", "ext-ms-win-")):
                continue
            dependency = _find_source_file(imported_name, search_dirs)
            if dependency is not None:
                add_native(dependency)
                continue
            if _is_windows_system_import(imported_name, system_dir):
                continue
            raise PackageError(f"{source.name} 依赖的 DLL 未能在 Anaconda 运行时或 Windows 找到：{imported_name}")

    return list(copied_sources.values()), import_manifest


def _write_stdlib_zip(source_root: Path, runtime: Path) -> int:
    library = source_root / "Lib"
    if not library.is_dir():
        raise PackageError(f"找不到 Python 标准库目录：{library}")
    archive = runtime / "python313.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as out:
        for source in sorted(library.rglob("*")):
            if not source.is_file() or source.suffix.casefold() in {".pyc", ".pyo"}:
                continue
            relative = source.relative_to(library)
            parts = {part.casefold() for part in relative.parts}
            if parts.intersection(EXCLUDED_STDLIB_PARTS):
                continue
            out.write(source, relative.as_posix())
    return archive.stat().st_size


def _write_python_pth(runtime: Path) -> None:
    content = """# Isolated CPython search path for the portable trainer.
python313.zip
.
DLLs
..\\app
"""
    (runtime / "python313._pth").write_text(content, encoding="ascii", newline="\n")


def _copy_tcl_resources(source_root: Path, runtime: Path) -> None:
    source_lib = source_root / "Library" / "lib"
    target_lib = runtime / "tcl"
    target_lib.mkdir(parents=True, exist_ok=True)
    for name in ("tcl8.6", "tk8.6"):
        source = source_lib / name
        if not source.is_dir():
            raise PackageError(f"找不到 Tkinter 脚本目录：{source}")
        destination = target_lib / name
        shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))


def _metadata_records(source_root: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    owners: dict[str, dict[str, Any]] = {}
    metadata_dir = source_root / "conda-meta"
    if not metadata_dir.is_dir():
        return records, owners
    for path in metadata_dir.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        records.append(record)
        for relative in record.get("files", ()):
            installed_path = (source_root / str(relative).replace("/", os.sep).replace("\\", os.sep)).resolve()
            owners[str(installed_path).casefold()] = record
    return records, owners


def _package_dist(record: dict[str, Any]) -> str:
    return f"{record.get('name', 'unknown')}-{record.get('version', 'unknown')}-{record.get('build', 'unknown')}"


def _license_sources(source_root: Path, records: list[dict[str, Any]], used_sources: list[Path]) -> list[dict[str, str]]:
    packages_root = source_root / "pkgs"
    _, owners = _metadata_records(source_root)
    used: dict[str, dict[str, Any]] = {}
    for path in used_sources:
        owner = owners.get(str(path.resolve()).casefold())
        if owner and owner.get("name"):
            used[str(owner["name"])] = owner

    licenses_dir = DIST_DIR / "licenses"
    licenses_dir.mkdir(parents=True, exist_ok=True)
    notice_rows: list[dict[str, str]] = []

    def copy_license_tree(name: str, version: str, license_name: str, source_dir: Path) -> bool:
        if not source_dir.is_dir():
            return False
        files = [item for item in source_dir.rglob("*") if item.is_file()]
        if not files:
            return False
        safe_name = re.sub(r"[^A-Za-z0-9._+-]+", "_", f"{name}-{version}")
        target_dir = licenses_dir / safe_name
        target_dir.mkdir(parents=True, exist_ok=True)
        for source in files:
            relative = source.relative_to(source_dir)
            destination = target_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        notice_rows.append({"name": name, "version": version, "license": license_name, "directory": safe_name})
        return True

    for record in sorted(used.values(), key=lambda item: str(item.get("name", "")).casefold()):
        name = str(record.get("name", "unknown"))
        version = str(record.get("version", "unknown"))
        license_name = str(record.get("license", "See included license files"))
        dist = _package_dist(record)
        package_license = packages_root / dist / "info" / "licenses"
        copied = copy_license_tree(name, version, license_name, package_license)
        if not copied and name in LICENSE_FALLBACKS:
            candidates = sorted(packages_root.glob(LICENSE_FALLBACKS[name]))
            for candidate in candidates:
                if copy_license_tree(name, version, license_name, candidate / "info" / "licenses"):
                    copied = True
                    break
        if not copied:
            # Python's license is distributed at the environment root; several
            # conda packages rely on it instead of installing info/licenses.
            if name == "python" and (source_root / "LICENSE_PYTHON.txt").is_file():
                continue
            # Some Anaconda package recipes omit info/licenses after install.
            # Keep the package metadata in the notice and add known upstream
            # documents below; do not silently substitute an unrelated license.
            notice_rows.append({"name": name, "version": version, "license": license_name, "directory": "See individual notices"})

    manual_files = (
        (source_root / "LICENSE_PYTHON.txt", licenses_dir / "Python-PSF-2.0" / "LICENSE_PYTHON.txt"),
        (source_root / "EULA.txt", licenses_dir / "Anaconda" / "EULA.txt"),
        (source_root / "Library" / "lib" / "tk8.6" / "license.terms", licenses_dir / "Tk-license.terms"),
    )
    for source, destination in manual_files:
        if source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    xz_docs = source_root / "Library" / "share" / "doc" / "xz"
    xz_target = licenses_dir / "xz-additional-notices"
    if xz_docs.is_dir():
        xz_target.mkdir(parents=True, exist_ok=True)
        for source in xz_docs.glob("COPYING*"):
            if source.is_file():
                shutil.copy2(source, xz_target / source.name)

    notice = [
        "Third-party notices for the bundled portable runtime",
        "",
        "The application payload is supplied separately under dist\\app.",
        "No site-packages, test suite, or analysis-only dependencies are bundled.",
        "See the neighboring license files for the full notices shipped with this runtime.",
        "",
        "Bundled conda components:",
    ]
    for row in notice_rows:
        notice.append(f"- {row['name']} {row['version']} — {row['license']} ({row['directory']})")
    notice.extend(("", "Anaconda distribution terms are included in Anaconda/EULA.txt."))
    (licenses_dir / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(notice) + "\n", encoding="utf-8")
    return notice_rows


def _version_string(python_exe: Path) -> str:
    result = subprocess.run([str(python_exe), "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    text = (result.stdout or result.stderr).strip()
    if result.returncode != 0 or not text.startswith("Python 3.13."):
        raise PackageError(f"需要 Python 3.13；检测结果为：{text or '无法读取版本'}")
    return text


def _compile_launcher(dist: Path) -> None:
    compiler = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
    if not compiler.is_file():
        raise PackageError(f"找不到 .NET Framework C# 编译器：{compiler}")
    if not LAUNCHER_SOURCE.is_file():
        raise PackageError(f"找不到 C# 启动器源码：{LAUNCHER_SOURCE}")
    command = [
        str(compiler), "/nologo", "/target:winexe", "/platform:x64", "/optimize+", "/codepage:65001",
        "/reference:System.Windows.Forms.dll", f"/out:{LAUNCHER_OUTPUT}", str(LAUNCHER_SOURCE),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if result.returncode != 0:
        details = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
        raise PackageError("C# 启动器编译失败。\n" + details)
    if not LAUNCHER_OUTPUT.is_file():
        raise PackageError("C# 编译器没有生成启动器文件。")


def _run_checked(command: list[str], *, cwd: Path, env: dict[str, str], label: str, timeout: int = 90) -> str:
    options: dict[str, Any] = {
        "cwd": str(cwd), "env": env, "capture_output": True, "text": True,
        "encoding": "utf-8", "errors": "replace", "timeout": timeout,
    }
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(command, **options)
    except Exception as exc:
        raise PackageError(f"{label}启动失败：{exc}") from exc
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
    if result.returncode != 0:
        raise PackageError(f"{label}失败（退出代码 {result.returncode}）。\n{output}")
    return output


def _verify_runtime(dist: Path, source_app: Path) -> dict[str, str]:
    runtime = dist / "runtime"
    app_dir = dist / "app"
    python_exe = runtime / "python.exe"
    env = os.environ.copy()
    env["TCL_LIBRARY"] = str(runtime / "tcl" / "tcl8.6")
    env["TK_LIBRARY"] = str(runtime / "tcl" / "tk8.6")
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    import_probe = (
        "import _tkinter, _ctypes, _hashlib, _socket, _bz2, _lzma, pyexpat, select, unicodedata; "
        "import ctypes, datetime, hashlib, json, pathlib, socket, bz2, lzma, tkinter, xml.parsers.expat; "
        "print('portable runtime imports: OK')"
    )
    import_output = _run_checked([str(python_exe), "-B", "-X", "utf8", "-I", "-c", import_probe], cwd=app_dir, env=env, label="运行库导入检查")

    packaged_app = app_dir / "trainer_ui.py"
    app_script = packaged_app if packaged_app.is_file() else source_app
    if not app_script.is_file():
        raise PackageError("找不到 trainer_ui.py；请先把应用源码放到 dist\\app 或工作区根目录。")
    self_output = _run_checked([str(python_exe), "-B", "-X", "utf8", "-I", str(app_script), "--self-test"], cwd=app_dir, env=env, label="应用静态自检")

    with tempfile.TemporaryDirectory(prefix=".runtime_verify_", dir=str(dist)) as output_dir:
        smoke_output = _run_checked(
            [str(python_exe), "-B", "-X", "utf8", "-I", str(app_script), "--smoke", "--smoke-dir", output_dir],
            cwd=app_dir, env=env, label="应用 Tk smoke", timeout=60,
        )
        generated = (Path(output_dir) / "trainer_ui_smoke.json").is_file() and (Path(output_dir) / "trainer_ui_smoke.bmp").is_file()
        if not generated:
            raise PackageError("Tk smoke 返回成功，但没有生成控件 JSON 和主界面截图。\n" + smoke_output)
    return {"runtime_imports": import_output.strip(), "app_self_test": self_output.strip(), "app_smoke": smoke_output.strip()}


def build(source_root: Path = DEFAULT_PYTHON_ROOT) -> dict[str, Any]:
    source_root = source_root.resolve()
    dist = DIST_DIR.resolve()
    project = PROJECT_ROOT.resolve()
    if dist.parent != project or dist.name.casefold() != "dist":
        raise PackageError("输出目录必须是 F:\\dd\\dist。")
    if not source_root.is_dir():
        raise PackageError(f"找不到 Python 环境：{source_root}")
    version = _version_string(source_root / "python.exe")
    pefile = _load_pefile()

    dist.mkdir(parents=True, exist_ok=True)
    (dist / "app").mkdir(parents=True, exist_ok=True)  # Preserve any files the parent has already staged here.
    _clear_owned_outputs(dist)
    runtime = dist / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)

    stdlib_zip_bytes = _write_stdlib_zip(source_root, runtime)
    _write_python_pth(runtime)
    copied_sources, import_manifest = _copy_native_closure(source_root, runtime, pefile)
    _copy_tcl_resources(source_root, runtime)
    license_rows = _license_sources(source_root, [], copied_sources)
    _compile_launcher(dist)

    runtime_bytes = sum(path.stat().st_size for path in runtime.rglob("*") if path.is_file())
    if runtime_bytes > RUNTIME_LIMIT:
        raise PackageError(f"裁剪后的运行库为 {runtime_bytes / 1024 / 1024:.1f} MiB，超过 40 MiB 上限。")

    source_app = PROJECT_ROOT / "trainer_ui.py"
    verification = _verify_runtime(dist, source_app)
    manifest = {
        "python": version,
        "runtime_bytes": runtime_bytes,
        "runtime_mib": round(runtime_bytes / 1024 / 1024, 2),
        "runtime_limit_mib": 40,
        "isolated_python_path": "python313._pth",
        "stdlib_archive": {"name": "python313.zip", "bytes": stdlib_zip_bytes, "site_packages_included": False, "tests_included": False},
        "extension_modules": list(EXTENSION_FILES),
        "native_dependencies": {name: imports for name, imports in sorted(import_manifest.items(), key=lambda item: item[0].casefold())},
        "licenses": license_rows,
        "launcher": LAUNCHER_OUTPUT.name,
        "application_payload": "dist/app/*.py (staged separately by the main agent)",
        "verification": verification,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="裁剪并验证 Darkest Dungeon 修改器的便携 Python/Tk 运行库")
    parser.add_argument("--python-root", type=Path, default=DEFAULT_PYTHON_ROOT, help="含 python.exe、Lib、DLLs、Library 的 Python 根目录")
    args = parser.parse_args(argv)
    try:
        manifest = build(args.python_root)
    except Exception as exc:
        print(f"打包失败：{exc}", file=sys.stderr)
        return 1
    print(f"运行库准备完成：{manifest['runtime_mib']} MiB（上限 40 MiB）")
    print(f"启动器：{DIST_DIR / LAUNCHER_OUTPUT.name}")
    print("已通过：运行库模块导入、应用 --self-test、Tk --smoke。")
    print("没有启动或连接游戏，也没有访问真实存档。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
