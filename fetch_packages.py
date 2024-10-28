#!/usr/bin/python3
#
# Copyright (c) 2013 Juniper Networks, Inc. All rights reserved.
#
import os
import errno
import re
import argparse
import shutil
import subprocess
import sys
import platform
from time import sleep

try:
    import distro
except ImportError:
    pass

ARGS = dict()
ARGS['filename'] = 'packages.xml'
ARGS['cache_dir'] = '/tmp/cache/' + os.environ['USER'] + '/webui_third_party'
ARGS['node_modules_dir'] = './node_modules'
ARGS['node_modules_tmp_dir'] = ARGS['cache_dir'] + '/' + ARGS['node_modules_dir']
ARGS['verbose'] = False
ARGS['dry_run'] = False
ARGS['site_mirror'] = None

_RETRIES = 5
_TAR_COMMAND = ['tar', '--no-same-owner']
_CACHED_PKG_DISTROS = ('Ubuntu', 'Red Hat', 'CentOS', 'darwin', 'Rocky Linux')

from lxml import objectify


def getFilename(pkg, url):
    element = pkg.find("local-filename")
    if element:
        return str(element)

    (path, filename) = url.rsplit('/', 1)
    m = re.match(r'\w+\?\w+=(.*)', filename)
    if m:
        filename = m.group(1)
    return filename


def setTarCommand():
    if isTarGnuVersion():
        print('GNU tar found. we will skip the no-unknown-keyword warning')
        global _TAR_COMMAND
        _TAR_COMMAND = ['tar', '--no-same-owner', '--warning=no-unknown-keyword']
    else:
        print('No GNU tar. will use default tar utility')


def isTarGnuVersion():
    cmd = subprocess.Popen(['tar', '--version'],
                           stdout=subprocess.PIPE)
    output = cmd.communicate()[0].decode()
    first = output.split('\n')[0]
    if first.lower().find('gnu') != -1:
        return True
    return False


def getTarDestination(tgzfile, compress_flag):
    cmd = subprocess.Popen(_TAR_COMMAND + ['--exclude=.*', '-' + compress_flag + 'tf', tgzfile],
                           stdout=subprocess.PIPE)
    (output, _) = cmd.communicate()
    (first, _) = output.decode().split('\n', 1)
    fields = first.split('/')
    return fields[0]


def getZipDestination(tgzfile):
    cmd = subprocess.Popen(['unzip', '-t', tgzfile],
                           stdout=subprocess.PIPE)
    (output, _) = cmd.communicate()
    lines = output.decode().split('\n')
    for line in lines:
        print(line)
        m = re.search(r'testing:\s+([\w\-\.]+)\/', line)
        if m:
            return m.group(1)
    return None


def getFileDestination(file):
    start = file.rfind('/')
    if start < 0:
        return None
    return file[start + 1:]


def ApplyPatches(pkg):
    stree = pkg.find('patches')
    if stree is None:
        return
    for patch in stree.getchildren():
        cmd = ['patch']
        if patch.get('strip'):
            cmd.append('-p')
            cmd.append(patch.get('strip'))
        if ARGS['verbose']:
            print("Patching %s <%s..." % (' '.join(cmd), str(patch)))
        if not ARGS['dry_run']:
            fp = open(str(patch), 'r')
            proc = subprocess.Popen(cmd, stdin=fp)
            proc.communicate()


def GetOSDistro():
    distrib = ''
    if sys.platform == 'darwin':
        return sys.platform
    else:
        try:
            if hasattr(platform, 'linux_distribution'):
                return platform.linux_distribution()[0]
            else:
                return distro.linux_distribution()[0]
        except Exception:
                pass
    return distrib


def ResolveEmptyDistro(url, md5, pkg):
    md5 = md5.other
    # Remove the $distro from the url and try
    url = url.replace('/$distro', '')
    # Change the pkg format to npm download the dependencies
    if pkg.format == 'npm-cached':
        pkg.format = 'npm'
    return url, md5


def ResolveDistro(url, md5, ccfile, pkg):
    distrib = GetOSDistro()
    if not distrib:
        url, md5 = ResolveEmptyDistro(url, md5, pkg)
        return (url, md5, ccfile)

    # check if we have the distrib in our cache
    found = False
    for cached_pkg in _CACHED_PKG_DISTROS:
        if cached_pkg in distrib:
            distrib = cached_pkg
            found = True
            break
    if not found:
        url, md5 = ResolveEmptyDistro(url, md5, pkg)
        return (url, md5, ccfile)

    distrib = distrib.lower().replace(" ", "")
    # TODO: add packages for rocky?
    if distrib == "rockylinux":
        distrib = "centos"
    url = url.replace('$distro', distrib)
    md5 = md5[distrib]
    pkg.distrib = distrib
    # Change the ccfile, add distrib before the package name
    idx = ccfile.rfind("/")
    pkgCachePath = ccfile[:idx] + "/" + distrib
    pkg.pkgCachePath = pkgCachePath
    pkg.ccfile = pkgCachePath + "/" + ccfile[idx + 1:]
    ccfile = pkg.ccfile.text
    # Now create the directory
    try:
        os.makedirs(pkgCachePath)
    except OSError:
        pass

    return (url, md5, ccfile)


def DownloadPackage(urls, ccfile, pkg):
    pkg.ccfile = ccfile
    for i in range(_RETRIES):
        for url in urls:
            md5 = pkg.md5
            url = url.text
            if "{{ site_mirror }}" in url:
                if not ARGS['site_mirror']:
                    continue
                url = url.replace("{{ site_mirror }}", ARGS['site_mirror'])

            if url.find('$distro') != -1:
                # Platform specific package download
                url, md5, ccfile = ResolveDistro(url, md5, ccfile, pkg)

            print("URL to download %s" % url)
            # Check if the package already exists
            if os.path.isfile(ccfile):
                md5sum = FindMd5sum(ccfile)
                if md5sum == md5:
                    return pkg
                os.remove(ccfile)

            subprocess.call(['wget', '--no-check-certificate', '-nv', '-O', ccfile, url])
            md5sum = FindMd5sum(ccfile)
            if ARGS['verbose']:
                print("Calculated md5sum: %s" % md5sum)
                print("Expected md5sum: %s" % md5)
            if md5sum == md5:
                return pkg
            os.remove(ccfile)
            sleep(2)

    raise RuntimeError("Package either could't be downloaded of incorrect. "
                       "MD5sum %s, expected(%s). "
                       "Package %s" % (md5sum, md5, ccfile))


def ProcessPackage(pkg):
    print("Processing %s ..." % (pkg['name']))
    urls = [u for u in pkg['urls'].iterchildren()]
    filename = getFilename(pkg, urls[0].text)
    ccfile = ARGS['cache_dir'] + '/' + filename
    if pkg.format == 'npm-cached':
        try:
            shutil.rmtree(ARGS['node_modules_dir'] + '/' + pkg['name'])
        except OSError:
            pass
        try:
            os.makedirs(ARGS['node_modules_dir'])
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                pass
            else:
                print('mkdirs of ' + ARGS['node_modules_dir'] + ' failed.. Exiting..')
                return
        # ccfile = ARGS['node_modules_dir'] + '/' + filename
    pkg = DownloadPackage(urls, ccfile, pkg)
    installArguments = pkg.find('install-arguments')

    #
    # Determine the name of the directory created by the package.
    # unpack-directory means that we 'cd' to the given directory before
    # unpacking.
    #
    ccfile = pkg.ccfile.text
    dest = None
    unpackdir = pkg.find('unpack-directory')
    if unpackdir:
        dest = str(unpackdir)
    else:
        if pkg.format == 'tgz':
            dest = getTarDestination(ccfile, 'z')
        elif pkg.format == 'npm-cached':
            dest = ARGS['node_modules_dir'] + '/' + getTarDestination(ccfile, 'z')
        elif pkg.format == 'tbz':
            dest = getTarDestination(ccfile, 'j')
        elif pkg.format == 'zip':
            dest = getZipDestination(ccfile)
        elif pkg.format == 'npm':
            dest = getTarDestination(ccfile, 'z')
        elif pkg.format == 'file':
            dest = getFileDestination(ccfile)

    #
    # clean directory before unpacking and applying patches
    #
    rename = pkg.find('rename')
    if rename and pkg.format == 'npm-cached':
        rename = ARGS['node_modules_dir'] + '/' + str(rename)
    if rename and os.path.isdir(str(rename)) and not ARGS['dry_run']:
        shutil.rmtree(str(rename))
    elif dest and os.path.isdir(dest):
        if ARGS['verbose']:
            print("Clean directory %s" % dest)
        if not ARGS['dry_run']:
            shutil.rmtree(dest)

    if unpackdir:
        try:
            os.makedirs(str(unpackdir))
        except OSError:
            pass

    cmd = None
    if pkg.format == 'tgz':
        cmd = _TAR_COMMAND + ['-zxf', ccfile]
    elif pkg.format == 'tbz':
        cmd = _TAR_COMMAND + ['-jxf', ccfile]
    elif pkg.format == 'zip':
        cmd = ['unzip', '-oq', ccfile]
    elif pkg.format == 'npm':
        newDir = ARGS['cache_dir']
        if 'distrib' in pkg:
            newDir = newDir + pkg.distrib
        cmd = ['npm', 'install', ccfile, '--prefix', newDir]
        if installArguments:
            cmd.append(str(installArguments))
    elif pkg.format == 'file':
        cmd = ['cp', '-af', ccfile, dest]
    elif pkg.format == 'npm-cached':
        cmd = _TAR_COMMAND + ['-zxf', ccfile, '-C', ARGS['node_modules_dir']]
    else:
        print('Unexpected format: %s' % (pkg.format))
        return

    print('Issuing command: %s' % (cmd))

    if not ARGS['dry_run']:
        cd = None
        if unpackdir:
            cd = str(unpackdir)
        if pkg.format == 'npm':
            try:
                os.makedirs(ARGS['node_modules_dir'])
                os.makedirs(newDir)
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    pass
                else:
                    print('mkdirs of ' + ARGS['node_modules_dir'] + ' ' + newDir + ' failed.. Exiting..')
                    return

            npmCmd = ['cp', '-af', newDir + "/" + ARGS['node_modules_dir'] + '/' + pkg['name'],
                      './node_modules/']
            if os.path.exists(newDir + '/' + pkg['name']):
                cmd = npmCmd
            else:
                try:
                    p = subprocess.Popen(cmd, cwd=cd)
                    ret = p.wait()
                    if ret != 0:
                        sys.exit('Terminating: ProcessPackage with return code: %d' % ret)
                    cmd = npmCmd
                except OSError:
                    print(' '.join(cmd) + ' could not be executed, bailing out!')
                    return

        p = subprocess.Popen(cmd, cwd=cd)
        ret = p.wait()
        if ret != 0:
            sys.exit('Terminating: ProcessPackage with return code: %d' % ret)

    if rename and dest:
        os.rename(dest, str(rename))

    ApplyPatches(pkg)


def FindMd5sum(anyfile):
    if sys.platform == 'darwin':
        cmd = ['md5', '-r']
    else:
        cmd = ['md5sum']
    cmd.append(anyfile)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.PIPE)
    stdout, stderr = proc.communicate()
    md5sum = stdout.split()[0]
    return md5sum.decode()


def parse_args():
    global ARGS

    for key in ARGS:
        env_val = os.getenv(key.upper())
        if env_val:
            ARGS[key] = env_val

    parser = argparse.ArgumentParser()
    parser.add_argument("-f", "--file", dest="filename", default=ARGS['filename'],
                        help="read data from FILENAME")
    parser.add_argument("--cache-dir", default=ARGS['cache_dir'])
    parser.add_argument("--node-modules-dir", default=ARGS['node_modules_dir'])
    parser.add_argument("--node-modules-tmp-dir", default=ARGS['node_modules_tmp_dir'])
    parser.add_argument("--verbose", default=ARGS['verbose'], action='store_true')
    parser.add_argument("--dry-run", default=ARGS['dry_run'], action='store_true')
    parser.add_argument("--site-mirror", dest="site_mirror", required=False, default=ARGS['site_mirror'])
    ARGS = vars(parser.parse_args())
    print("INFO: input args are " + str(ARGS))


def main():
    parse_args()
    os.chdir(os.path.dirname(os.path.realpath(__file__)))
    try:
        os.makedirs(ARGS['cache_dir'])
    except OSError:
        pass

    # Check which version of tar is used and skip warning messages.
    setTarCommand()

    tree = objectify.parse(ARGS['filename'])
    root = tree.getroot()
    for item in root.iterchildren():
        if item.tag == 'package':
            ProcessPackage(item)


if __name__ == '__main__':
    main()
