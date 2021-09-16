# Released under the MIT License. See LICENSE for details.
#
"""Utilities for wrangling Android SDK bits."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from efro.error import CleanError

if TYPE_CHECKING:
    from typing import List


def _parse_lprop_file(local_properties_path: str) -> str:
    with open(local_properties_path, encoding='utf-8') as infile:
        lines = infile.read().splitlines()
    sdk_dir_lines = [l for l in lines if 'sdk.dir=' in l]
    if len(sdk_dir_lines) != 1:
        raise Exception("Couldn't find sdk dir in local.properties")
    sdk_dir = sdk_dir_lines[0].split('=')[1].strip()
    if not os.path.isdir(sdk_dir):
        raise Exception(f'Sdk dir from local.properties not found: {sdk_dir}.')
    return sdk_dir


def _gen_lprop_file(local_properties_path: str) -> str:

    os.makedirs(os.path.dirname(local_properties_path), exist_ok=True)

    # Ok, we've got no local.properties file; attempt to find
    # android sdk in standard locations and create a default
    # one if we can.
    found = False
    sdk_dir = None

    # First off, if they have ANDROID_SDK_ROOT set, use that.
    envvar = os.environ.get('ANDROID_SDK_ROOT')
    if envvar is not None:
        if os.path.isdir(envvar):
            sdk_dir = envvar
            found = True

    # Otherwise try some standard locations.
    if not found:
        home = os.getenv('HOME')
        assert home is not None
        test_paths = [home + '/Library/Android/sdk']
        for sdk_dir in test_paths:
            if os.path.exists(sdk_dir):
                found = True
                break
    if not found:
        print('WOULD CHECK', os.environ.get('ANDROID_SDK_ROOT'))
    assert sdk_dir is not None
    if not found:
        if not os.path.exists(sdk_dir):
            print(
                'ERROR: Android sdk not found; install '
                'the android sdk and try again',
                file=sys.stderr)
            sys.exit(255)
    config = ('\n# This file was automatically generated by ' +
              os.path.abspath(sys.argv[0]) + '\n'
              '# Feel free to override these paths if you have your android'
              ' sdk elsewhere\n'
              '\n'
              'sdk.dir=' + sdk_dir + '\n')
    with open(local_properties_path, 'w', encoding='utf-8') as outfile:
        outfile.write(config)
    print('Generating local.properties file (found Android SDK at "' +
          sdk_dir + '")',
          file=sys.stderr)
    return sdk_dir


def run(projroot: str, args: List[str]) -> None:
    """Main script entry point."""
    # pylint: disable=too-many-branches
    # pylint: disable=too-many-locals

    if len(args) != 1:
        raise CleanError('Expected 1 arg')

    command = args[0]

    valid_args = ['check', 'get-sdk-path', 'get-ndk-path', 'get-adb-path']
    if command not in valid_args:
        print('INVALID ARG; expected one of', valid_args, file=sys.stderr)
        sys.exit(255)

    # In all cases we make sure there's a local.properties in our android
    # dir that contains valid sdk path.  If not, we attempt to create it.
    local_properties_path = os.path.join(projroot, 'ballisticacore-android',
                                         'local.properties')
    if os.path.isfile(local_properties_path):
        sdk_dir = _parse_lprop_file(local_properties_path)
    else:
        sdk_dir = _gen_lprop_file(local_properties_path)

    # Sanity check; look for a few things in the sdk that we expect to
    # be there.
    if not os.path.isfile(sdk_dir + '/platform-tools/adb'):
        raise Exception('ERROR: android sdk at "' + sdk_dir +
                        '" does not seem valid')

    # Sanity check: if they've got ANDROID_HOME set, make sure it lines up with
    # what we're pointing at.
    android_home = os.getenv('ANDROID_HOME')
    if android_home is not None:
        if android_home != sdk_dir:
            print('ERROR: sdk dir mismatch; ANDROID_HOME is "' + android_home +
                  '" but local.properties set to "' + sdk_dir + '"',
                  file=sys.stderr)
            sys.exit(255)

    if command == 'get-sdk-path':
        print(sdk_dir)

    # We no longer add the ndk path to local.properties (doing so is obsolete)
    # but we still want to support returning the ndk path, as some things such
    # as external python builds still ask for this. So now we just pull it from
    # the project gradle file where we set it explicitly.
    if command == 'get-ndk-path':
        gradlepath = Path(projroot, 'ballisticacore-android/build.gradle')
        with gradlepath.open(encoding='utf-8') as infile:
            lines = [
                l for l in infile.readlines()
                if l.strip().startswith('ext.ndk_version = ')
            ]
        if len(lines) != 1:
            raise RuntimeError(
                f'Expected exactly one ndk_version line in build.gradle;'
                f' found {len(lines)}')
        ver = lines[0].strip().replace("'", '').replace('"', '').split()[-1]
        path = os.path.join(sdk_dir, 'ndk', ver)
        if not os.path.isdir(path):
            raise Exception(f'NDK listed in gradle not found: {path}')
        print(path)

    if command == 'get-adb-path':
        import subprocess
        adbpath = Path(sdk_dir, 'platform-tools/adb')
        if not os.path.exists(adbpath):
            raise Exception(f'ADB not found at expected path {adbpath}')

        # Ok, we've got a valid adb path.
        # Now, for extra credit, let's see if 'which adb' points to the
        # same one and simply return 'adb' if so. This makes our make
        # output nice and readable (and hopefully won't cause problems)
        result = subprocess.run('which adb',
                                shell=True,
                                capture_output=True,
                                check=False)
        if result.returncode == 0:
            wpath = result.stdout.decode().strip()
            if wpath == str(adbpath):
                print('adb')
                return
        print(adbpath)
