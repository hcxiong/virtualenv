import json
import os.path
import subprocess
import textwrap

from virtualenv.builders.base import BaseBuilder
from virtualenv._utils import copyfile, ensure_directory


SITE = """
import sys
import os.path

# We want to make sure that our sys.prefix and sys.exec_prefix match the
# locations in our virtual enviornment.
sys.prefix = "__PREFIX__"
sys.exec_prefix = "__EXEC_PREFIX__"

# We want to record what the "real/base" prefix is of the virtual environment.
sys.base_prefix = "__BASE_PREFIX__"
sys.base_exec_prefix = "__BASE_EXEC_PREFIX__"

# At the point this code is running, the only paths on the sys.path are the
# paths that the interpreter adds itself. These are essentially the locations
# it looks for the various stdlib modules. Since we are inside of a virtual
# environment these will all be relative to the sys.prefix and sys.exec_prefix,
# however we want to change these to be relative to sys.base_prefix and
# sys.base_exec_prefix instead.
new_sys_path = []
for path in sys.path:
    # TODO: Is there a better way to determine this?
    if path.startswith(sys.prefix):
        path = os.path.join(
            sys.base_prefix,
            path[len(sys.prefix) + 1:],
        )
    elif path.startswith(sys.exec_prefix):
        path = os.path.join(
            sys.base_exec_prefix,
            path[len(sys.exec_prefix) + 1:],
        )

    new_sys_path.append(path)
sys.path = new_sys_path

# We want to empty everything that has already been imported from the
# sys.modules so that any additional imports of these modules will import them
# from the base Python and not from the copies inside of the virtual
# environment. This will ensure that our copies will only be used for
# bootstrapping the virtual environment.
for key in list(sys.modules):
    # We don't want to purge these modules because if we do, then things break
    # very badly.
    if key in ["sys", "site", "sitecustomize", "__builtin__", "__main__"]:
        continue

    del sys.modules[key]

# We want to trick the interpreter into thinking that the user specific
# site-packages has been requested to be disabled. We'll do this by mimicing
# that sys.flags.no_user_site has been set to False, however sys.flags is a
# read-only structure so we'll temporarily replace it with one that has the
# same values except for sys.flags.no_user_site which will be set to True.
_real_sys_flags = sys.flags
class AttrDict(dict):
    def __init__(self, *args, **kwargs):
        dict.__init__(self, *args, **kwargs)
    def __getattr__(self, name):
        return self[name]
sys.flags = AttrDict((k, getattr(sys.flags, k)) for k in dir(sys.flags))
sys.flags["no_user_site"] = True

# Next we want to import the *real* site module from the base Python. Actually
# attempting to do an import here will just import this module again, so we'll
# just read the real site module and exec it.
with open("__SITE__") as fp:
    exec(fp.read())

# Finally we'll restore the real sys.flags
sys.flags = _real_sys_flags
"""


class LegacyBuilder(BaseBuilder):

    @classmethod
    def check_available(self, python):
        # TODO: Do we ever want to make this builder *not* available?
        return True

    def _get_base_python_info(self):
        # Get information from the base python that we need in order to create
        # a legacy virtual environment.
        return json.loads(
            subprocess.check_output([
                self.python,
                "-c",
                textwrap.dedent("""
                import json
                import os
                import os.path
                import site
                import sys

                def resolve(path):
                    return os.path.realpath(os.path.abspath(path))

                print(
                    json.dumps({
                        "sys.version_info": tuple(sys.version_info),
                        "sys.executable": resolve(sys.executable),
                        "sys.prefix": resolve(sys.prefix),
                        "sys.exec_prefix": resolve(sys.exec_prefix),
                        "lib": resolve(os.path.dirname(os.__file__)),
                        "site.py": os.path.join(
                            resolve(os.path.dirname(site.__file__)),
                            "site.py",
                        ),
                    })
                )
                """),
            ]).decode("utf8"),
        )

    def create_virtual_environment(self, destination):
        # Get a bunch of information from the base Python.
        base_python = self._get_base_python_info()

        # Create our binaries that we'll use to create the virtual environment
        bin_dir = os.path.join(destination, "bin")
        ensure_directory(bin_dir)
        for i in range(3):
            copyfile(
                base_python["sys.executable"],
                os.path.join(
                    bin_dir,
                    "python{}".format(
                        ".".join(map(str, base_python["sys.version_info"][:i]))
                    ),
                ),
            )

        # Create our lib directory, this is going to hold all of the parts of
        # the standard library that we need in order to ensure that we can
        # successfully bootstrap a Python interpreter.
        lib_dir = os.path.join(
            destination,
            "lib",
            "python{}".format(
                ".".join(map(str, base_python["sys.version_info"][:2]))
            ),
        )
        ensure_directory(lib_dir)

        # Create our site-packages directory, this is the thing that end users
        # really want control over.
        site_packages_dir = os.path.join(lib_dir, "site-packages")
        ensure_directory(site_packages_dir)

        # The Python interpreter uses the os.py module as a sort of sentinel
        # value for where it can locate the rest of it's files. It will first
        # look relative to the bin directory, so we can copy the os.py file
        # from the target Python into our lib directory to trick Python into
        # using our virtual environment's prefix as it's own.
        # Note: At this point we'll have a broken environment, because it will
        # only have the os module but none of the os's modules dependencies or
        # any other module unless they are "special" modules built into the
        # interpreter like the sys module.
        copyfile(
            os.path.join(base_python["lib"], "os.py"),
            os.path.join(lib_dir, "os.py"),
        )

        # The site module has a number of required modules that it needs in
        # order to be successfully imported, so we'll copy each of those module
        # into our virtual environment's lib directory as well. Note that this
        # list also includes the os module, but since we've already copied
        # that we'll go ahead and omit it.
        modules = {
            "posixpath.py", "stat.py", "genericpath.py", "warnings.py",
            "linecache.py", "types.py", "UserDict.py", "_abcoll.py", "abc.py",
            "_weakrefset.py", "copy_reg.py",
        }
        for module in modules:
            copyfile(
                os.path.join(base_python["lib"], module),
                os.path.join(lib_dir, module),
            )

        dst = os.path.join(lib_dir, "site.py")
        with open(dst, "w", encoding="utf8") as dst_fp:
            # Get the data from our source file, and replace our special
            # variables with the computed data.
            data = SITE
            data = data.replace("__PREFIX__", destination)
            data = data.replace("__EXEC_PREFIX__", destination)
            data = data.replace("__BASE_PREFIX__", base_python["sys.prefix"])
            data = data.replace(
                "__BASE_EXEC_PREFIX__", base_python["sys.exec_prefix"],
            )
            data = data.replace("__SITE__", base_python["site.py"])

            # Write the final site.py file to our lib directory
            dst_fp.write(data)