"""Copyright (c) 2020 Chaquo Ltd. All rights reserved."""

import _imp
from calendar import timegm
from contextlib import contextmanager, nullcontext
import ctypes
import imp
from importlib import _bootstrap, _bootstrap_external, machinery, metadata, util
from inspect import getmodulename
import io
import os.path
from os.path import basename, dirname, exists, join, normpath, relpath, split, splitext
import pathlib
from pkgutil import get_importer
import platform
import re
from shutil import copyfileobj, rmtree
import site
import sys
from tempfile import mktemp, NamedTemporaryFile
import time
from threading import RLock
from zipfile import ZipFile, ZipInfo
from zipimport import zipimporter

import java.chaquopy
from java._vendor.elftools.elf.elffile import ELFFile
from java.chaquopy_android import AssetFile

from android.os import Build
from com.chaquo.python import Common
from com.chaquo.python.android import AndroidPlatform


def initialize(context, build_json, app_path):
    initialize_importlib(context, build_json, app_path)
    initialize_ctypes()
    initialize_imp()


def initialize_importlib(context, build_json, app_path):
    sys.meta_path[sys.meta_path.index(machinery.PathFinder)] = ChaquopyPathFinder

    # ZIP file extraction uses copyfileobj, whose default buffer size is quite small (#5596).
    assert len(copyfileobj.__defaults__) == 1
    copyfileobj.__defaults__ = (1024 * 1024,)

    global ASSET_PREFIX
    ASSET_PREFIX = join(context.getFilesDir().toString(), Common.ASSET_DIR, "AssetFinder")
    def hook(path):
        return AssetFinder(context, build_json, path)
    sys.path_hooks.insert(0, hook)
    sys.path_hooks[sys.path_hooks.index(zipimporter)] = ChaquopyZipImporter
    sys.path_importer_cache.clear()

    sys.path = [p for p in sys.path if exists(p)]  # Remove nonexistent default paths
    for i, asset_name in enumerate(app_path):
        entry = join(ASSET_PREFIX, asset_name)
        sys.path.insert(i, entry)
        finder = get_importer(entry)
        assert isinstance(finder, AssetFinder), ("Finder for '{}' is {}"
                                                 .format(entry, type(finder).__name__))

        # Extract data files from the root directory. This includes .pth files, which will be
        # read by addsitedir below.
        finder.extract_dir("", recursive=False)

        # Extract data files from top-level directories which aren't Python packages.
        for name in finder.listdir(""):
            if finder.isdir(name) and \
               not is_dist_info(name) and \
               not any(finder.exists(f"{name}/__init__{suffix}") for suffix in LOADERS):
                finder.extract_dir(name)

        # We do this here instead of in AssetFinder.__init__ because code in the .pth files may
        # require the finder to be fully available to the system, which isn't the case until
        # get_importer returns.
        site.addsitedir(finder.extract_root)


def is_dist_info(name):
    return bool(re.search(r"\.(dist|egg)-info$", name))


def initialize_ctypes():
    import ctypes.util
    import sysconfig

    reqs_finder = get_importer(f"{ASSET_PREFIX}/requirements")

    # The standard implementation of find_library requires external tools, so will always fail
    # on Android.
    def find_library_override(name):
        filename = "lib{}.so".format(name)

        # First look in the requirements.
        try:
            filename = reqs_finder.extract_lib(filename)
        except FileNotFoundError:
            pass
        else:
            # The return value will probably be passed to CDLL_init_override below. If the
            # caller loads the library using any other API (e.g. soundfile uses ffi.dlopen),
            # then on 64-bit devices before API level 23 there's a possible race condition
            # between updating LD_LIBRARY_PATH and loading the library, but there's nothing we
            # can do about that.
            with extract_so(reqs_finder, filename) as dlopen_name:
                return dlopen_name

        # For system libraries I can't see any easy way of finding the absolute library
        # filename, but we can at least support the case where the user passes the return value
        # of find_library to CDLL().
        try:
            ctypes.CDLL(filename)
            return filename
        except OSError:
            return None

    ctypes.util.find_library = find_library_override

    def CDLL_init_override(self, name, *args, **kwargs):
        context = nullcontext(name)
        if name:  # CDLL(None) is equivalent to dlopen(NULL).
            try:
                # find_library_override may have returned a basename (see extract_so).
                name = reqs_finder.extract_lib(name)
            except FileNotFoundError:
                pass

            # Some packages (e.g. llvmlite) use CDLL to load libraries from their own
            # directories.
            finder = get_importer(dirname(name))
            if isinstance(finder, AssetFinder):
                context = extract_so(finder, name)

        with context as dlopen_name:
            CDLL_init_original(self, dlopen_name, *args, **kwargs)

    CDLL_init_original = ctypes.CDLL.__init__
    ctypes.CDLL.__init__ = CDLL_init_override

    # The standard library initializes pythonapi to PyDLL(None), which only works on API level
    # 21 or higher.
    ctypes.pythonapi = ctypes.PyDLL(sysconfig.get_config_vars()["LDLIBRARY"])


def initialize_imp():
    # The standard implementations of imp.find_module and imp.load_module do not use the PEP
    # 302 import system. They are therefore only capable of loading from directory trees and
    # built-in modules, and will ignore both sys.path_hooks and sys.meta_path. To accommodate
    # code which uses these functions, we provide these replacements.
    global find_module_original, load_module_original
    find_module_original = imp.find_module
    load_module_original = imp.load_module
    imp.find_module = find_module_override
    imp.load_module = load_module_override


def find_module_override(base_name, path=None):
    # When calling find_module_original, we can't just replace None with sys.path, because None
    # will also search built-in modules.
    path_original = path

    if path is None:
        path = sys.path
    for entry in path:
        finder = get_importer(entry)
        if hasattr(finder, "prefix"):  # AssetFinder or zipimporter
            real_name = join(finder.prefix, base_name).replace("/", ".")
            loader = finder.find_module(real_name)
            if loader is not None:
                filename = loader.get_filename(real_name)
                if loader.is_package(real_name):
                    file = None
                    pathname = dirname(filename)
                    suffix, mode, mod_type = ("", "", imp.PKG_DIRECTORY)
                else:
                    for suffix, mode, mod_type in imp.get_suffixes():
                        if filename.endswith(suffix):
                            break
                    else:
                        raise ValueError("Couldn't determine type of module '{}' from '{}'"
                                         .format(real_name, filename))

                    # SWIG-generated code such as
                    # tensorflow_core/python/pywrap_tensorflow_internal.py requires the file
                    # object to be not None, so we'll return an object of the correct type.
                    # However, we won't bother to supply the data, because the file may be as
                    # large as 200 MB in the case of tensorflow, which would reduce performance
                    # unnecessarily and maybe even exhaust the device's memory.
                    file = io.BytesIO() if mode == "rb" else io.StringIO()
                    pathname = filename

                if mod_type == imp.C_EXTENSION:
                    # torchvision/extension.py uses imp.find_module to find a non-Python .so
                    # file, which it then loads using CDLL. So we need to extract the file now.
                    finder.extract_if_changed(finder.zip_path(pathname))

                return (file, pathname, (suffix, mode, mod_type))

    return find_module_original(base_name, path_original)


def load_module_override(load_name, file, pathname, description):
    if pathname is not None:
        finder = get_importer(dirname(pathname))
        if hasattr(finder, "prefix"):  # AssetFinder or zipimporter
            entry, base_name = split(pathname)
            real_name = join(finder.prefix, splitext(base_name)[0]).replace("/", ".")
            if hasattr(finder, "find_spec"):
                spec = finder.find_spec(real_name)
                spec.name = load_name
                return _bootstrap._load(spec)
            elif real_name == load_name:
                return finder.find_module(real_name).load_module(real_name)
            else:
                raise ImportError(
                    "{} does not support loading module '{}' under a different name '{}'"
                    .format(type(finder).__name__, real_name, load_name))

    return load_module_original(load_name, file, pathname, description)


# Because so much code requires pkg_resources without declaring setuptools as a dependency, we
# include it in the bootstrap ZIP. We don't include the rest of setuptools, because it's much
# larger and much less likely to be useful. If the user installs setuptools via pip, then that
# copy of pkg_resources will take priority because the requirements ZIP is earlier on sys.path.
#
# pkg_resources is quite large, so this function shouldn't be called until the app needs it.
def initialize_pkg_resources():
    import pkg_resources

    def distribution_finder(finder, entry, only):
        for name in finder.listdir(""):
            if is_dist_info(name):
                yield pkg_resources.Distribution.from_location(entry, name)

    pkg_resources.register_finder(AssetFinder, distribution_finder)
    pkg_resources.working_set = pkg_resources.WorkingSet()

    class AssetProvider(pkg_resources.NullProvider):
        def __init__(self, module):
            super().__init__(module)
            self.finder = self.loader.finder

        def _has(self, path):
            return self.finder.exists(self.finder.zip_path(path))

        def _isdir(self, path):
            return self.finder.isdir(self.finder.zip_path(path))

        def _listdir(self, path):
            return self.finder.listdir(self.finder.zip_path(path))

    pkg_resources.register_loader_type(AssetLoader, AssetProvider)


# Patch zipimporter to provide the new loader API, which is required by dateparser
# (https://stackoverflow.com/questions/63574951). Once the standard zipimporter implements the
# new API, this should be removed.
for name in ["create_module", "exec_module"]:
    assert not hasattr(zipimporter, name), name
    setattr(zipimporter, name, getattr(_bootstrap_external._LoaderBasics, name))

# For consistency with modules which have already been imported by the default zipimporter, we
# retain the following default behaviours:
#   * __file__ will end with ".pyc", not ".py"
#   * co_filename will be taken from the .pycs in the ZIP, which means it'll start with
#    "stdlib/" or "bootstrap/".
class ChaquopyZipImporter(zipimporter):

    def exec_module(self, mod):
        super().exec_module(mod)
        exec_module_trigger(mod)

    def __repr__(self):
        return f'<{type(self).__name__} object "{join(self.archive, self.prefix)}">'


# importlib.metadata is still being actively developed, so instead of depending on any internal
# APIs, provide a self-contained implementation.
class ChaquopyPathFinder(metadata.MetadataPathFinder, machinery.PathFinder):
    @classmethod
    def find_distributions(cls, context=metadata.DistributionFinder.Context()):
        name = (".*" if context.name is None
                # See normalize_name_wheel in build-wheel.py.
                else re.sub(r"[^A-Za-z0-9.]+", '_', context.name))
        pattern = fr"^{name}(-.*)?\.(dist|egg)-info$"

        for entry in context.path:
            path_cls = AssetPath if entry.startswith(ASSET_PREFIX + "/") else pathlib.Path
            entry_path = path_cls(entry)
            if entry_path.is_dir():
                for sub_path in entry_path.iterdir():
                    if re.search(pattern, sub_path.name, re.IGNORECASE):
                        yield metadata.PathDistribution(sub_path)


class AssetPath(pathlib.PosixPath):
    def __new__(cls, *args):
        return cls._from_parts(args)

    # Base class uses _init rather than __init__.
    def _init(self, *args):
        super()._init(*args)
        root_dir = str(self)
        while dirname(root_dir) != ASSET_PREFIX:
            root_dir = dirname(root_dir)
            assert root_dir, str(self)
        self.finder = get_importer(root_dir)
        self.zip_path = self.finder.zip_path(str(self))

    def is_dir(self):
        return self.finder.isdir(self.zip_path)

    def iterdir(self):
        for name in self.finder.listdir(self.zip_path):
            yield AssetPath(join(str(self),  name))

    def open(self, mode="r", buffering=-1, **kwargs):
        if "r" in mode:
            bio = io.BytesIO(self.finder.get_data(self.zip_path))
            if mode == "r":
                return io.TextIOWrapper(bio, **kwargs)
            elif sorted(mode) == ["b", "r"]:
                return bio
        raise ValueError(f"unsupported mode: {mode!r}")


class AssetFinder:

    def __init__(self, context, build_json, path):
        if not path.startswith(ASSET_PREFIX + "/"):
            raise ImportError(f"not an asset path: '{path}'")
        self.context = context  # Also used in tests.
        self.path = path

        parent_path = dirname(path)
        if parent_path == ASSET_PREFIX:  # Root finder
            self.extract_root = path
            self.prefix = ""
            sp = context.getSharedPreferences(Common.ASSET_DIR, context.MODE_PRIVATE)
            assets_json = build_json.get("assets")

            # To allow modules in both requirements ZIPs to access data files from the other
            # ZIP, we extract both ZIPs to the same directory, and make both ZIPs generate
            # modules whose __file__ and __path__ point to that directory. This is most easily
            # done by accessing both ZIPs through the same finder.
            self.zip_files = []
            for abi in [None, Common.ABI_COMMON, AndroidPlatform.ABI]:
                asset_name = Common.assetZip(basename(self.extract_root), abi)
                try:
                    self.zip_files.append(
                        AssetZipFile(self.context, join(Common.ASSET_DIR, asset_name)))
                except FileNotFoundError:
                    continue

                # See also similar code in AndroidPlatform.java.
                # TODO #5677: multi-process race conditions.
                sp_key = "asset." + asset_name
                new_hash = assets_json.get(asset_name)
                if sp.getString(sp_key, "") != new_hash:
                    if exists(self.extract_root):
                        rmtree(self.extract_root)
                    sp.edit().putString(sp_key, new_hash).apply()

            if not self.zip_files:
                raise FileNotFoundError(path)

            # Affects site.addsitedir which is called above (see site._init_pathinfo).
            os.makedirs(self.extract_root, exist_ok=True)
        else:
            parent = get_importer(parent_path)
            self.extract_root = parent.extract_root
            self.prefix = relpath(path, self.extract_root)
            self.zip_files = parent.zip_files

    def __repr__(self):
        return f"{type(self).__name__}({self.path!r})"

    def find_spec(self, mod_name, target=None):
        spec = None
        loader = self.find_module(mod_name)
        if loader:
            spec = util.spec_from_loader(mod_name, loader)
        else:
            dir_path = join(self.prefix, mod_name.rpartition(".")[2])
            if self.isdir(dir_path):
                # Possible namespace package.
                spec = machinery.ModuleSpec(mod_name, None)
                spec.submodule_search_locations = [join(self.extract_root, dir_path)]
        return spec

    def find_module(self, mod_name):
        # Ignore all but the last word of the name (see FileFinder.find_spec).
        prefix = join(self.prefix, mod_name.rpartition(".")[2])
        # Packages take priority over modules (see FileFinder.find_spec).
        for infix in ["/__init__", ""]:
            for zf in self.zip_files:
                for suffix, loader_cls in LOADERS.items():
                    try:
                        zip_info = zf.getinfo(prefix + infix + suffix)
                    except KeyError:
                        continue
                    if (infix == "/__init__") and ("." not in mod_name):
                        # This is a top-level package: extract all data files within it.
                        self.extract_dir(prefix)
                    return loader_cls(self, mod_name, zip_info)
        return None

    # Called by pkgutil.iter_modules.
    def iter_modules(self, prefix=""):
        for filename in self.listdir(self.prefix):
            zip_path = join(self.prefix, filename)
            if self.isdir(zip_path):
                for sub_filename in self.listdir(zip_path):
                    if getmodulename(sub_filename) == "__init__":
                        yield prefix + filename, True
                        break
            else:
                mod_base_name = getmodulename(filename)
                if mod_base_name and (mod_base_name != "__init__"):
                    yield prefix + mod_base_name, False

    # If this method raises FileNotFoundError, then maybe it's a system library, or one of the
    # libraries loaded by AndroidPlatform.loadNativeLibs. If the library is truly missing,
    # we'll get an exception when we load the file that needs it.
    def extract_lib(self, filename):
        return self.extract_if_changed(f"chaquopy/lib/{filename}")

    def extract_dir(self, zip_dir, recursive=True):
        for filename in self.listdir(zip_dir):
            zip_path = join(zip_dir, filename)
            if self.isdir(zip_path):
                if recursive:
                    self.extract_dir(zip_path)
            elif not (any(filename.endswith(suffix) for suffix in LOADERS) or
                      re.search(r"^lib.*\.so\.", filename)):  # e.g. libgfortran
                self.extract_if_changed(zip_path)

    def extract_if_changed(self, zip_path):
        # Unlike AssetZipFile.extract_if_changed, this method may search multiple ZIP files, so
        # it can't take a ZipInfo argument.
        assert isinstance(zip_path, str)

        for zf in self.zip_files:
            try:
                return zf.extract_if_changed(zip_path, self.extract_root)
            except KeyError:
                pass
        raise FileNotFoundError(zip_path)

    def exists(self, zip_path):
        return any(zf.exists(zip_path) for zf in self.zip_files)

    def isdir(self, zip_path):
        return any(zf.isdir(zip_path) for zf in self.zip_files)

    def listdir(self, zip_path):
        result = [name for zf in self.zip_files if zf.isdir(zip_path)
                  for name in zf.listdir(zip_path)]
        if not result and not self.isdir(zip_path):
            raise (NotADirectoryError if self.exists(zip_path) else FileNotFoundError)(zip_path)
        return result

    def get_data(self, zip_path):
        for zf in self.zip_files:
            try:
                return zf.read(zip_path)
            except KeyError:
                pass
        raise FileNotFoundError(zip_path)

    def zip_path(self, path):
        # If `path` is absolute then `join` will return it unchanged.
        path = join(self.extract_root, path)
        if path == self.extract_root:
            return ""
        if not path.startswith(self.extract_root + "/"):
            raise ValueError(f"{self} can't access '{path}'")
        return path[len(self.extract_root) + 1:]


# To create a concrete loader class, inherit this class followed by a FileLoader subclass.
class AssetLoader:
    def __init__(self, finder, fullname, zip_info):
        self.finder = finder
        self.zip_info = zip_info
        super().__init__(fullname, join(finder.extract_root, zip_info.filename))

    def __repr__(self):
        return f"{type(self).__name__}({self.name!r}, {self.path!r})"

    # Override to disable the fullname check. This is necessary for module renaming via imp.
    def get_filename(self, fullname):
        return self.path

    def get_data(self, path):
        if exists(path):
            # For __pycache__ directories created by SourceAssetLoader, and data files created
            # by extract_dir.
            with open(path, "rb") as f:
                return f.read()
        return self.finder.get_data(self.finder.zip_path(path))

    def exec_module(self, mod):
        super().exec_module(mod)
        exec_module_trigger(mod)

    def get_resource_reader(self, mod_name):
        return self if self.is_package(mod_name) else None

    def open_resource(self, name):
        return io.BytesIO(self.get_data(self.res_abs_path(name)))

    def resource_path(self, name):
        path = self.res_abs_path(name)
        if exists(path):
            # For __pycache__ directories created by SourceAssetLoader, and data files created
            # by extract_dir.
            return path
        else:
            # importlib.resources.path will call open_resource and create a temporary file.
            raise FileNotFoundError()

    def is_resource(self, name):
        zip_path = self.finder.zip_path(self.res_abs_path(name))
        return self.finder.exists(zip_path) and not self.finder.isdir(zip_path)

    def contents(self):
        return self.finder.listdir(self.finder.zip_path(dirname(self.path)))

    def res_abs_path(self, name):
        return join(dirname(self.path), name)


def exec_module_trigger(mod):
    if mod.__name__ == "pkg_resources":
        initialize_pkg_resources()
    elif mod.__name__ == "numpy":
        java.chaquopy.numpy = mod  # See conversion.pxi.


# The SourceFileLoader base class will automatically create and use _pycache__ directories.
class SourceAssetLoader(AssetLoader, machinery.SourceFileLoader):
    def path_stats(self, path):
        return {"mtime": timegm(self.zip_info.date_time),
                "size": self.zip_info.file_size}


# In case user code depends on the original source filename, we make sure it's used everywhere.
class SourcelessAssetLoader(AssetLoader, machinery.SourcelessFileLoader):
    def exec_module(self, mod):
        assert self.path.endswith(".pyc"), self.path
        mod.__file__ = self.path[:-1]
        return super().exec_module(mod)

    def get_code(self, fullname):
        code = super().get_code(fullname)
        _imp._fix_co_filename(code, self.path[:-1])
        return code


class ExtensionAssetLoader(AssetLoader, machinery.ExtensionFileLoader):
    def create_module(self, spec):
        with extract_so(self.finder, self.path) as spec.origin:
            mod = super().create_module(spec)
        mod.__file__ = self.path  # In case user code depends on the original filename.
        return mod


# On 32-bit ABIs before API level 23, the dynamic linker ignores DT_SONAME and identifies
# libraries using their basename. So when asked to load a library with the same basename as one
# already loaded, it will return the existing library
# (https://android.googlesource.com/platform/bionic/+/master/android-changes-for-ndk-developers.md#correct-soname_path-handling-available-in-api-level-23)
#
# We can work around this by loading through a uniquely-named symlink. However, we only do
# that when we actually encounter a duplicate name, because there's at least one package
# (tensorflow) where one Python module has a DT_NEEDED entry for another one, which on API
# level 22 and older will only work if the other module has already been loaded from its
# original filename.
#
# On 64-bit ABIs we use the same workaround for a different reason: see extract_so.
so_basenames_loaded = {}

# Detect basename clashes with bootstrap modules. For example, both the standard
# library and scikit-learn have an extension module called _random.
if Build.VERSION.SDK_INT < 23:
    for mod in sys.modules.values():
        filename = getattr(mod, "__file__", None)
        if isinstance(filename, str) and filename.endswith(".so"):
            existing = so_basenames_loaded.setdefault(basename(filename), filename)
            assert existing == filename, f"basename clash between {existing} and {filename}"

def symlink_if_needed(path):
    if Build.VERSION.SDK_INT < 23:
        # We used to generate load_name from the zip_path, but that would cause a clash if the
        # first library (loaded directly) is in a directory, and the second one (loaded through
        # a symlink) is in the ZIP file root. So now we just add a numeric suffix.
        load_name = original_name = basename(path)
        i = 0
        with extract_so_lock:
            while (load_name in so_basenames_loaded and
                   so_basenames_loaded[load_name] != path):  # In case of reloads.
                i += 1
                load_name = f"{original_name}.{i}"
            so_basenames_loaded[load_name] = path

        if load_name != original_name:
            path = join(dirname(path), load_name)
            atomic_symlink(original_name, path)

    return path


extract_so_lock = RLock()
needed_loaded = {}

@contextmanager
def extract_so(finder, path):
    path = finder.extract_if_changed(finder.zip_path(path))
    path = symlink_if_needed(path)
    load_needed(finder, path)

    # On 64-bit ABIs before API level 23, the dynamic linker ignores DT_SONAME and identifies
    # libraries using the full path passed to dlopen
    # (https://github.com/aosp-mirror/platform_bionic/commit/489e498434f53269c44e3c13039eb630e86e1fd9).
    # This allows it to load multiple libraries with the same basename. Unfortunately, it also
    # means that DT_NEEDED entries can only be resolved using libraries which are either
    # currently on LD_LIBRARY_PATH, or were loaded via their basenames (which means they must
    # have been on LD_LIBRARY_PATH when they were loaded). Also, the field that stores the
    # library name is 128 characters, which isn't long enough for many absolute paths.
    #
    # Since we're already working around the basename clash problem, we'll simulate the 32-bit
    # behavior by putting the library's dirname into LD_LIBRARY_PATH using an undocumented
    # libdl function, and then loading it through its basename.
    if Build.VERSION.SDK_INT < 23 and platform.architecture()[0] == "64bit":
        # We need to include the app's lib directory, because our libraries there were
        # loaded via System.loadLibrary, which passes absolute paths to dlopen (#5563).
        llp = ":".join([dirname(path),
                        finder.context.getApplicationInfo().nativeLibraryDir])
        with extract_so_lock:
            ctypes.CDLL("libdl.so").android_update_LD_LIBRARY_PATH(llp.encode())
            yield basename(path)
    else:
        yield path


# CDLL will cause a recursive call back to extract_so, so there's no need for any additional
# recursion here. If we return to executables in the future, we can implement a separate
# recursive extraction on top of get_needed.
def load_needed(finder, path):
    with extract_so_lock:
        for soname in get_needed(path):
            if soname not in needed_loaded:
                try:
                    needed_filename = finder.extract_lib(soname)
                except FileNotFoundError:
                    needed_loaded[soname] = None
                else:
                    # Before API 23, the only dlopen mode was RTLD_GLOBAL, and RTLD_LOCAL was
                    # ignored. From API 23, RTLD_LOCAL is available and used by default, just like
                    # in Linux (#5323). We use RTLD_GLOBAL, so that the library's symbols are
                    # available to subsequently-loaded libraries.
                    #
                    # It doesn't look like the library is closed when the CDLL object is garbage
                    # collected, but this isn't documented, so keep a reference for safety.
                    needed_loaded[soname] = ctypes.CDLL(needed_filename, ctypes.RTLD_GLOBAL)


def get_needed(path):
    with open(path, "rb") as file:
        ef = ELFFile(file)
        dynamic = ef.get_section_by_name(".dynamic")
        if dynamic:
            return [tag.needed for tag in dynamic.iter_tags()
                    if tag.entry.d_tag == "DT_NEEDED"]
        else:
            return []


# This may be added to the standard library in a future version of Python
# (https://bugs.python.org/issue36656).
def atomic_symlink(target, link):
    while True:
        tmp_link = mktemp(dir=dirname(link), prefix=basename(link) + ".")
        try:
            os.symlink(target, tmp_link)
            break
        except FileExistsError:
            pass

    os.replace(tmp_link, link)


LOADERS = {
    ".py": SourceAssetLoader,
    ".pyc": SourcelessAssetLoader,
    ".so": ExtensionAssetLoader,
}


class AssetZipFile(ZipFile):
    def __init__(self, context, path, *args, **kwargs):
        super().__init__(AssetFile(context, path), *args, **kwargs)

        self.dir_index = {"": set()}  # Provide empty listing for root even if ZIP is empty.
        for name in self.namelist():
            # If `name` ends with a slash, it represents a directory. However, not all ZIP
            # files contain these entries.
            parts = name.split("/")
            while parts:
                parent = "/".join(parts[:-1])
                if parent in self.dir_index:
                    self.dir_index[parent].add(parts[-1])
                    break
                else:
                    base_name = parts.pop()
                    self.dir_index[parent] = set([base_name] if base_name else [])
        self.dir_index = {k: sorted(v) for k, v in self.dir_index.items()}

    # Based on ZipFile.extract, but fixed to be safe in the presence of multiple threads
    # creating the same file or directory.
    def extract(self, member, target_dir):
        if not isinstance(member, ZipInfo):
            member = self.getinfo(member)

        out_filename = normpath(join(target_dir, member.filename))
        out_dirname = dirname(out_filename)
        if out_dirname:
            os.makedirs(out_dirname, exist_ok=True)

        if member.is_dir():
            os.makedirs(out_filename, exist_ok=True)
        else:
            with self.open(member) as source_file, \
                 NamedTemporaryFile(delete=False, dir=out_dirname,
                                    prefix=basename(out_filename) + ".") as tmp_file:
                copyfileobj(source_file, tmp_file)
            os.replace(tmp_file.name, out_filename)

        return out_filename

    # ZipFile.extract does not set any metadata (https://bugs.python.org/issue32170), so we set
    # the timestamp after extraction is complete. That way, if the app gets killed in the
    # middle of an extraction, the timestamps won't match and we'll know we need to extract the
    # file again.
    #
    # The Gradle plugin sets all ZIP timestamps to 1980 for reproducibility, so we can't rely
    # on them to tell us which files have changed after an app update. Instead,
    # AssetFinder.__init__ just removes the whole extract_root if any of its ZIPs have changed.
    def extract_if_changed(self, member, target_dir):
        if not isinstance(member, ZipInfo):
            member = self.getinfo(member)

        need_extract = True
        out_filename = join(target_dir, member.filename)
        if exists(out_filename):
            existing_stat = os.stat(out_filename)
            need_extract = (existing_stat.st_size != member.file_size or
                            existing_stat.st_mtime != timegm(member.date_time))

        if need_extract:
            extracted_filename = self.extract(member, target_dir)
            assert extracted_filename == out_filename, (extracted_filename, out_filename)
            os.utime(out_filename, (time.time(), timegm(member.date_time)))
        return out_filename

    def exists(self, path):
        if self.isdir(path):
            return True
        try:
            self.getinfo(path)
            return True
        except KeyError:
            return False

    def isdir(self, path):
        return path.rstrip("/") in self.dir_index

    def listdir(self, path):
        return self.dir_index[path.rstrip("/")]
