#!/usr/bin/env python
#
# git-p4.py -- A tool for bidirectional operation between a Perforce depot and git.
#
# Author: Simon Hausmann <simon@lst.de>
# Copyright: 2007 Simon Hausmann <simon@lst.de>
#            2007 Trolltech ASA
# License: MIT <http://www.opensource.org/licenses/mit-license.php>
#

import optparse, sys, os, marshal, subprocess, shlex
import tempfile, os.path, time, platform
import urllib
import re
import cStringIO

#from sets import Set

verbose = False 

def die(msg):
    if verbose:
        raise Exception(msg)
    else:
        sys.stderr.write(msg + "\n")
        sys.exit(1)

class LargeFileWriter:
    """Wrapper for a file, to get around a windows bug when writing large amounts of data.
    
    When writing to the wrapped file object, writes are broken up so that only 10MB are written at
    a time. Otherwise, there is IO exception on Windows.
    """
    def __init__(self, filedesc, debug = None):
        self.read = filedesc.read
        self.flush = filedesc.flush
        self.close = filedesc.close
        self.filedesc = filedesc
        self.debug = debug

    def write(self, text):
        chunk = 10*1024*1024 # I don't know how high we can go before the bug is triggered, so we
        #only write 10MB at a time.
        while len(text) > chunk:
            self.filedesc.write(text[:chunk])
            self.filedesc.flush()
            text= text[chunk:]

        if len(text):    
            self.filedesc.write(text[:chunk])
            self.filedesc.flush()
            if self.debug is not None:
                self.debug.write(text[:chunk])

class P4Helper:
    """ Encapsulates P4 methods so that they can be replaced for testing purposes
    """
    
    def p4_build_cmd(self, cmd):
        """Build a suitable p4 command line.

        This consolidates building and returning a p4 command line into one
        location. It means that hooking into the environment, or other configuration
        can be done more easily.
        """
        real_cmd = "p4 "

        user = gitConfig("git-p4.user")
        if len(user) > 0:
            real_cmd += "-u %s " % user

        password = gitConfig("git-p4.password")
        if len(password) > 0:
            real_cmd += "-P %s " % password

        port = gitConfig("git-p4.port")
        if len(port) > 0:
            real_cmd += "-p %s " % port

        host = gitConfig("git-p4.host")
        if len(host) > 0:
            real_cmd += "-H %s " % host

        client = gitConfig("git-p4.client")
        if len(client) > 0:
            real_cmd += "-c %s " % client

        real_cmd += "-d \"%s\" %s" % (os.getcwd(), cmd)
        if verbose:
            print "THE COMMAND IS '" + real_cmd + "'"
        return real_cmd

    def p4_write_pipe(self, c, str):
        real_cmd = self.p4_build_cmd(c)
        return write_pipe(real_cmd, str)
        
    def p4_read_write_pipe(self, c, str, ignore_error=False):
        real_cmd = self.p4_build_cmd(c)
        return read_write_pipe(real_cmd, str,  ignore_error)

    def isP4Exec(self, kind):
        """Determine if a Perforce 'kind' should have execute permission

        'p4 help filetypes' gives a list of the types.  If it starts with 'x',
        or x follows one of a few letters.  Otherwise, if there is an 'x' after
        a plus sign, it is also executable"""
        return (re.search(r"(^[cku]?x)|\+.*x", kind) != None)

    def p4_read_pipe(self, c, ignore_error=False):
        real_cmd = self.p4_build_cmd(c)
        return read_pipe(real_cmd, ignore_error)

    def p4_read_pipe_lines(self, c):
        """Specifically invoke p4 on the command supplied. """
        real_cmd = self.p4_build_cmd(c)
        return read_pipe_lines(real_cmd)

    def p4_system(self, cmd):
        """Specifically invoke p4 as the system command. """
        real_cmd = self.p4_build_cmd(cmd)
        return system(real_cmd)

    def setP4ExecBit(self, file, mode):
        # Reopens an already open file and changes the execute bit to match
        # the execute bit setting in the passed in mode.

        p4Type = "+x"

        if not isModeExec(mode):
            p4Type = self.getP4OpenedType(file)
            p4Type = re.sub('^([cku]?)x(.*)', '\\1\\2', p4Type)
            p4Type = re.sub('(.*?\+.*?)x(.*?)', '\\1\\2', p4Type)
            if p4Type[-1] == "+":
                p4Type = p4Type[0:-1]

        self.p4_system("reopen -t %s \"%s\"" % (p4Type, escapeStringP4(file)))

    def getP4OpenedType(self, file):
        # Returns the perforce file type for the given file.
        result = self.p4_read_pipe("opened \"%s\"" % escapeStringP4only(file))
        match = re.match(".*\((.+)\)\r?", result)
        if match:
            return match.group(1)
        else:
            die("Could not determine file type for %s (result: '%s')" % (file, result))

    def p4CmdListOpen(self, cmd, stdin=None, stdin_mode='w+b'):
        cmd = self.p4_build_cmd("-G %s" % (cmd))
        if verbose:
            sys.stderr.write("Opening pipe: %s\n" % cmd)

        # Use a temporary file to avoid deadlocks without
        # subprocess.communicate(), which would put another copy
        # of stdout into memory.
        stdin_file = None
        if stdin is not None:
            stdin_file = tempfile.TemporaryFile(prefix='p4-stdin', mode=stdin_mode)
            stdin_file.write(stdin)
            stdin_file.flush()
            stdin_file.seek(0)

        return subprocess.Popen(cmd, shell=True,
                              stdin=stdin_file,
                              stdout=subprocess.PIPE)

    def p4CmdList(self, cmd, stdin=None, stdin_mode='w+b'):

        p4 = self.p4CmdListOpen(cmd, stdin, stdin_mode)    
        result = []
        try:
            while True:
                entry = marshal.load(p4.stdout)
                
                if entry['code'] == 'error':
                    sys.stderr.write("p4 returned an error: %s\n" % entry['data'])
                    sys.exit(1)
                
                result.append(entry)
        except EOFError:
            pass
        exitCode = p4.wait()
        if exitCode != 0:
            entry = {}
            entry["p4ExitCode"] = exitCode
            result.append(entry)

        return result

    def p4Cmd(self, cmd):
        cmdList = self.p4CmdList(cmd)
        result = {}
        for entry in cmdList:
            result.update(entry)
        return result;

    def p4Where(self, depotPath):
        if not depotPath.endswith("/"):
            depotPath += "/"
        depotPath = depotPath + "..."
        outputList = self.p4CmdList("where %s" % depotPath)
        output = None
        for entry in outputList:
            print repr(entry)
            print "depotPath:" + depotPath
            if "depotFile" in entry:
                if entry["depotFile"] == depotPath:
                    output = entry
                    print "found1"
                    break
            elif "data" in entry:
                data = entry.get("data")
                space = data.find(" ")
                if data[:space] == depotPath:
                    output = entry
                    print "found2"
                    break
            else:
                print "not found"
        if output == None:
            return ""
        if output["code"] == "error":
            return ""
        clientPath = ""
        if "path" in output:
            clientPath = output.get("path")
        elif "data" in output:
            data = output.get("data")
            lastSpace = data.rfind(" ")
            clientPath = data[lastSpace + 1:]

        if clientPath.endswith("..."):
            clientPath = clientPath[:-3]
        return clientPath

    def p4ChangesForPaths(self, depotPaths, changeRange):
        assert depotPaths
        output = self.p4_read_pipe_lines("changes " + ' '.join (['"%s..."%s' % (p, changeRange)
                                                            for p in depotPaths]))

        changes = []
        for line in output:
            changeNum = line.split(" ")[1]
            changes.append(int(changeNum))

        changes.sort()
        return changes
        
    def integrateFile(self, diff, changelist=""):
        src, dest = diff['src'], diff['dst']
        self.p4_system("integrate %s -Dt \"%s\" \"%s\"" % (changelist, src, escapeStringP4(dest)))
        self.p4_system("edit %s \"%s\"" % (changelist, escapeStringP4(dest)))
        os.unlink(dest)
        return dest

def escapeStringP4ForAdd(str):
    return escapeDollarSign(str)

def escapeStringP4only(str):
    # Escape characters that have a special meaning in p4 (without normal escaping)
    return re.sub(r"@", r"%40", re.sub(r"#", r"%23", re.sub(r"\*", r"%2A", re.sub(r"%", r"%25", str))))

def escapeStringP4(str):
    # Escape characters that have a special meaning in p4 (plus the normal escaping)
    return escapeStringP4only(escapeDollarSign(str))

def escapeString(str):
    # Escape dollar sign and star characters in string
    # Note: we don't need to escape parens, space and backslash if the string is enclosed in quotes
    return re.sub(r"\*", r"\\*", escapeDollarSign(str))

def escapeDollarSign(str):
    # Escape dollar sign
    # On Windows, don't escape dollar sign
    if (platform.system() == "Windows"):
        return str
    return re.sub(r"\$", r"\\$", str)

def getRefsPrefix(importIntoRemotes):
    if importIntoRemotes:
         return "refs/remotes/p4/"
    else:
        return "refs/heads/p4/"

def chdir(dir):
    if os.name == 'nt':
        os.environ['PWD']=dir
    os.chdir(dir)

def write_pipe(c, str):
    if verbose:
        sys.stderr.write('Writing pipe: %s\n' % c)

    pipe = os.popen(c, 'w')
    val = pipe.write(str)
    if pipe.close():
        die('Command failed: %s' % c)

    return val

def read_write_pipe(c,  str,  ignore_error=False):
    if verbose:
        sys.stderr.write('Writing/Reading pipe: %s\n' % c)

    popen = subprocess.Popen(shlex.split(c), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    val = popen.communicate(str)[0]
    if popen.returncode and not ignore_error:
        die('Command failed: %s' % c)

    return val

def read_pipe(c, ignore_error=False):
    if verbose:
        sys.stderr.write('Reading pipe: %s\n' % c)

    if ignore_error:
        popen = subprocess.Popen(shlex.split(c), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    else:
        popen = subprocess.Popen(shlex.split(c), stdout=subprocess.PIPE)
    val = popen.communicate()[0]
    if popen.returncode and not ignore_error:
        die('Command failed: %s' % c)
    if popen.returncode and verbose:
        print "read_pipe error (ignoring!): %s" % c
    return val

def read_pipe_lines(c):
    if verbose:
        sys.stderr.write('Reading pipe: %s\n' % c)
    ## todo: check return status
    pipe = os.popen(c, 'rb')
    val = pipe.readlines()
    if pipe.close():
        die('Command failed: %s' % c)

    return val

def system(cmd):
    if verbose:
        sys.stderr.write("executing %s\n" % cmd)
    if os.system(cmd) != 0:
        die("command failed: %s" % cmd)

def diffTreePattern():
    # This is a simple generator for the diff tree regex pattern. This could be
    # a class variable if this and parseDiffTreeEntry were a part of a class.
    pattern = re.compile(':(\d+) (\d+) (\w+) (\w+) ([A-Z])(\d+)?\t(.*?)((\t(.*))|$)')
    while True:
        yield pattern

def parseDiffTreeEntry(entry):
    """Parses a single diff tree entry into its component elements.

    See git-diff-tree(1) manpage for details about the format of the diff
    output. This method returns a dictionary with the following elements:

    src_mode - The mode of the source file
    dst_mode - The mode of the destination file
    src_sha1 - The sha1 for the source file
    dst_sha1 - The sha1 fr the destination file
    status - The one letter status of the diff (i.e. 'A', 'M', 'D', etc)
    status_score - The score for the status (applicable for 'C' and 'R'
                   statuses). This is None if there is no score.
    src - The path for the source file.
    dst - The path for the destination file. This is only present for
          copy or renames. If it is not present, this is None.

    If the pattern is not matched, None is returned."""

    match = diffTreePattern().next().match(entry)
    if match:
        return {
            'src_mode': match.group(1),
            'dst_mode': match.group(2),
            'src_sha1': match.group(3),
            'dst_sha1': match.group(4),
            'status': match.group(5),
            'status_score': match.group(6),
            'src': match.group(7),
            'dst': match.group(10)
        }
    return None

def isModeExec(mode):
    # Returns True if the given git mode represents an executable file,
    # otherwise False.
    return mode[-3:] == "755"

def isModeExecChanged(src_mode, dst_mode):
    return isModeExec(src_mode) != isModeExec(dst_mode)

# Some commands (eg. p4 print the entire repository) can result in gigabytes of
# data. In order to handle this without running out of memory, we have to
# process these items in batches of maybe 100 at a time. The P4CmdReader class
# handles this chunking of data.
class P4CmdReader:
    def __init__(self, cmd, stdin):
        self.MAX_CHUNKS = 100
        self.p4 = P4Helper().p4CmdListOpen(cmd, stdin)

    def __iter__(self):
        return self

    def next(self):
        try:
            return marshal.load(self.p4.stdout)
        except IOError, e:
            print "IOError while reading marshaller: %s" % repr(e)
            print e.strerror
            raise
        except EOFError:
            raise StopIteration

def currentGitBranch():
    return read_pipe("git symbolic-ref -q HEAD")[len('refs/heads/'):].strip()

def isValidGitDir(path):
    if (os.path.exists(path + "/HEAD")
        and os.path.exists(path + "/refs") and os.path.exists(path + "/objects")):
        return True;
    return False

def parseRevision(ref):
    if gitBranchExists(ref):
        return read_pipe("git rev-parse %s" % ref).strip()
    else:
        return ""

def extractLogMessageFromGitCommit(commit):
    logMessage = ""

    ## fixme: title is first line of commit, not 1st paragraph.
    foundTitle = False
    for log in read_pipe_lines("git cat-file commit %s" % commit):
       if not foundTitle:
           if len(log) == 1:
               foundTitle = True
           continue

       logMessage += log
    return logMessage

def extractSettingsFromNotes(commit):
    values = {}
    if verbose:
        print "extract settings..."
        print commit
    note = read_pipe("git notes --ref=git-p4 show %s" % commit, True)
    if verbose:
        print note
    m = re.search (r"^ *\[(.*)\]$", note)
    if not m:
        return values

    assignments = m.group(1).split (':')
    for a in assignments:
        vals = a.split ('=')
        key = vals[0].strip()
        val = ('='.join (vals[1:])).strip()
        if val.endswith ('\"') and val.startswith('"'):
            val = val[1:-1]

        values[key] = val

    paths = values.get("depot-paths")
    if not paths:
        paths = values.get("depot-path")
    if paths:
        values['depot-paths'] = paths.split(',')
    return values
    
    
def gitBranchExists(branch):
    proc = subprocess.Popen(["git", "rev-parse", branch],
                            stderr=subprocess.PIPE, stdout=subprocess.PIPE);
    return proc.wait() == 0;

_gitConfig = {}
def gitConfig(key):
    if not _gitConfig.has_key(key):
        _gitConfig[key] = read_pipe("git config %s" % key, ignore_error=True).strip()
    return _gitConfig[key]

def gitConfigList(key):
    if not _gitConfig.has_key(key):
        _gitConfig[key] = read_pipe("git config --get-all %s" % key, ignore_error=True).strip().split(os.linesep)
    return _gitConfig[key]

def p4BranchesInGit(branchesAreInRemotes = True):
    branches = {}

    cmdline = "git rev-parse --symbolic "
    if branchesAreInRemotes:
        cmdline += " --remotes"
    else:
        cmdline += " --branches"

    for line in read_pipe_lines(cmdline):
        line = line.strip()

        ## only import to p4/
        if not line.startswith('p4/') or line == "p4/HEAD":
            continue
        branch = line

        # strip off p4
        branch = re.sub ("^p4/", "", line)

        # branches["master"] = parseRevision("p4/master")
        branches[branch] = parseRevision(line) 
    return branches

def findUpstreamBranchPoint(head = "HEAD"):
    branches = p4BranchesInGit()
    # map from depot-path to branch name
    branchByDepotPath = {}
    for branch in branches.keys():
        tip = branches[branch]
        settings = extractLastSettingsFromNotes(tip)
        if settings.has_key("depot-paths"):
            paths = ",".join(settings["depot-paths"])
            if branchByDepotPath.has_key(paths):
                print "Warning: depot-path %s already covered by git branch %s" % (paths, branch)
            branchByDepotPath[paths] = "remotes/p4/" + branch

    settings = extractLastSettingsFromNotes(head)
    if settings.has_key("depot-paths"):
        paths = ",".join(settings["depot-paths"])
        if branchByDepotPath.has_key(paths):
            return [branchByDepotPath[paths], settings]
    return ["", settings]

def extractLastSettingsFromNotes(head):
    settings = None
    parent = 0
    while parent < 65535:
        commit = head + "~%s" % parent
        settings = extractSettingsFromNotes(commit)
        if settings.has_key("depot-paths"):
            return settings

        parent = parent + 1

    return settings

def createOrUpdateBranchesFromOrigin(localRefPrefix = "refs/remotes/p4/", silent=True):
    if not silent:
        print ("Creating/updating branch(es) in %s based on origin branch(es)"
               % localRefPrefix)

    originPrefix = "origin/p4/"

    for line in read_pipe_lines("git rev-parse --symbolic --remotes"):
        line = line.strip()
        if (not line.startswith(originPrefix)) or line.endswith("HEAD"):
            continue

        headName = line[len(originPrefix):]
        remoteHead = localRefPrefix + headName
        originHead = line

        original = extractSettingsFromNotes(originHead)
        if (not original.has_key('depot-paths')
            or not original.has_key('change')):
            continue

        update = False
        if not gitBranchExists(remoteHead):
            if verbose:
                print "creating %s" % remoteHead
            update = True
        else:
            settings = extractSettingsFromNotes(remoteHead)
            if settings.has_key('change') > 0:
                if settings['depot-paths'] == original['depot-paths']:
                    originP4Change = int(original['change'])
                    p4Change = int(settings['change'])
                    if originP4Change > p4Change:
                        print ("%s (%s) is newer than %s (%s). "
                               "Updating p4 branch from origin."
                               % (originHead, originP4Change,
                                  remoteHead, p4Change))
                        update = True
                else:
                    print ("Ignoring: %s was imported from %s while "
                           "%s was imported from %s"
                           % (originHead, ','.join(original['depot-paths']),
                              remoteHead, ','.join(settings['depot-paths'])))

        if update:
            system("git update-ref %s %s" % (remoteHead, originHead))

def originP4BranchesExist():
        return gitBranchExists("origin") or gitBranchExists("origin/p4") or gitBranchExists("origin/p4/master")

class Command:
    def __init__(self):
        self.usage = "usage: %prog [options]"
        self.needsGit = True
        self.p4 = P4Helper()

class P4Debug(Command):
    def __init__(self):
        Command.__init__(self)
        self.options = [
            optparse.make_option("--verbose", dest="verbose", action="store_true",
                                 default=False),
            ]
        self.description = "A tool to debug the output of p4 -G."
        self.needsGit = False
        self.verbose = False

    def run(self, args):
        j = 0
        for output in self.p4.p4CmdList(" ".join(args)):
            print 'Element: %d' % j
            j += 1
            print output
        return True

class P4RollBack(Command):
    def __init__(self):
        Command.__init__(self)
        self.options = [
            optparse.make_option("--verbose", dest="verbose", action="store_true"),
            optparse.make_option("--local", dest="rollbackLocalBranches", action="store_true")
        ]
        self.description = "A tool to debug the multi-branch import. Don't use :)"
        self.verbose = False
        self.rollbackLocalBranches = False

    def run(self, args):
        if len(args) != 1:
            return False
        maxChange = int(args[0])

        if "p4ExitCode" in self.p4.p4Cmd("changes -m 1"):
            die("Problems executing p4");

        if self.rollbackLocalBranches:
            refPrefix = "refs/heads/"
            lines = read_pipe_lines("git rev-parse --symbolic --branches")
        else:
            refPrefix = "refs/remotes/"
            lines = read_pipe_lines("git rev-parse --symbolic --remotes")

        for line in lines:
            if self.rollbackLocalBranches or (line.startswith("p4/") and line != "p4/HEAD\n"):
                line = line.strip()
                ref = refPrefix + line
                settings = extractSettingsFromNotes(ref)

                depotPaths = settings['depot-paths']
                change = settings['change']

                changed = False

                if len(self.p4.p4Cmd("changes -m 1 "  + ' '.join (['%s...@%s' % (p, maxChange)
                                                           for p in depotPaths]))) == 0:
                    print "Branch %s did not exist at change %s, deleting." % (ref, maxChange)
                    system("git update-ref -d %s `git rev-parse %s`" % (ref, ref))
                    continue

                while change and int(change) > maxChange:
                    changed = True
                    if self.verbose:
                        print "%s is at %s ; rewinding towards %s" % (ref, change, maxChange)
                    system("git update-ref %s \"%s^\"" % (ref, ref))
                    settings = extractSettingsFromNotes(ref)


                    depotPaths = settings['depot-paths']
                    change = settings['change']

                if changed:
                    print "%s rewound to %s" % (ref, change)

        return True

class P4FileReader:
    Bytes = 0
    LastFile = ''
    LastBytes = 0
    def __init__(self, files, clientSpecDirs):
        # Initialize P4FileReader object with a list of files to read. This
        # takes into account the clientSpecDirs passed in.
        # Each element of files is a dictionary with the following
        # elements:
        #
        # path: The complete path to the file in the depot, starting from "//"
        # action: The action to take. Eg: 'delete' 'purge'

        # list of files to commit.
        self.filesForCommit = []

        # list of files to read, filtered according to the client spec.
        self.filesToRead = []

        # mapping from path to file record.
        self.pathMap = {}

        self.filesRead = 0

        self.filterClientSpec( files, clientSpecDirs )

        self.reader = P4CmdReader('-x - print',
                             stdin='\n'.join(['%s#%s' % (f['path'], f['rev'])
                                              for f in self.filesToRead]) )

        # leftover record from previous time next() was called.
        self.leftover = None

    def filterClientSpec( self, files, clientSpecDirs ):
        # sets filesForCommit and filesToRead, filtered according to the client spec.
        for f in files:
            includeFile = False
            excludeFile = False
            if len(clientSpecDirs):
                for val in clientSpecDirs:
                    if f['path'].startswith(val[0]):
                        if val[1] > 0:
                            includeFile = True
                        else:
                            excludeFile = True
            else:
                includeFile = True

            if includeFile and not excludeFile:
                self.filesForCommit.append(f)
                self.pathMap[f["path"]] = f
                self.pathMap[f["targetPath"]] = f
                if f['action'] not in ('delete', 'purge', 'move/delete'):
                    self.filesToRead.append(f)

    def printStatus(self, filename):
        if filename == self.LastFile and self.Bytes - self.LastBytes < 100*1024: return
        self.LastFile = filename
        self.LastBytes = self.Bytes
        if len(filename) <= 60:
            line = filename + " " * (63-len(filename))
        else:
            line = "..." + filename[len(filename)-60:]

        print "%s | %.1f MB (%d/%d files)\r" % (line,
            float(self.Bytes) / (1024*1024), self.filesRead + 1,
            len(self.filesToRead)),

    def __iter__(self):
        return self

    def next(self):
        # Return a record containing a file to commit.
        #
        # Perforce outputs a number of records for each file. The first one
        # contains basic information such as the change list and filename. This
        # is followed by a number of records containing only "code" and "data"
        # fields, which are the file data broken apart.

        # while perforce keeps giving us files we didn't ask for,
        # (Shouldn't ever happen, but handle it anyway)
        while 1:
            textBuffer = cStringIO.StringIO()

            if self.leftover:
                header = self.leftover
                self.leftover = None
            else:
                try:
                    header = self.reader.next()
                except StopIteration:
                    print "" # newline for status information
                    raise

            # now we have the header record.
            if not header.has_key('depotFile'):
                die("p4 print fails with: %s\n" % repr(header))

            self.printStatus(header['depotFile'])

            for record in self.reader:
                if record['code'] in ( 'text', 'unicode', 'binary', 'utf16' ):
                    # encountered subsequent data chunk. Append to file data.
                    textBuffer.write( record['data'] )
                    self.Bytes += len(record['data'])
                    del record['data']
                    self.printStatus(header['depotFile'])
                else:
                    # encountered the next header.
                    # store for processing next time.
                    self.leftover = record
                    break

            if header['type'].startswith('utf16'):
                # Don't even try to convert utf16. Ask p4 to write the file directly.
                # on windows, NamedTemporaryFile creates a file that is locked so we can't actually print -o to it.
                if os.name == 'nt':
                    # create a temp file but don't delete it when it's closed
                    tmpFile = tempfile.NamedTemporaryFile(delete=False)
                    tmpFile.close()
                else:
                    tmpFile = tempfile.NamedTemporaryFile()

                P4Helper().p4_system("print -o \"%s\" \"%s\"" % (tmpFile.name, escapeStringP4(header['depotFile'])))
                text = open(tmpFile.name).read()
                tmpFile.close()
                # TODO for windows, figure out how we can delete this file -- git-bash creates the files as readonly
            else:
                text = textBuffer.getvalue()
            textBuffer.close()

            if header['type'] in ('text+ko', 'unicode+ko', 'binary+ko', 'utf16+ko'):
                text = re.sub(r'(?i)\$(Id|Header):[^$]*\$',r'$\1$', text)
            elif header['type'] in ('text+k', 'ktext', 'kxtext', 'unicode+k', 'binary+k', 'utf16+k'):
                text = re.sub(r'\$(Id|Header|Author|Date|DateTime|Change|File|Revision):[^$\n]*\$',r'$\1$', text)

            depotFile = None
            filePath = header['depotFile']
            if filePath in self.pathMap:
                depotFile = self.pathMap[filePath]
                depotFile['data'] = text
                self.filesRead += 1
                return depotFile
            else:
                # perforce gave us something we didn't ask for?
                print "Bad path: %s" % filePath
                continue
            

class P4Submit(Command):
    def __init__(self):
        Command.__init__(self)
        self.options = [
                optparse.make_option("--verbose", dest="verbose", action="store_true"),
                optparse.make_option("--origin", dest="origin"),
                optparse.make_option("--auto", dest="interactive", action="store_false", help="Automatically submit changelists without requiring editing"),
                optparse.make_option("-M", dest="detectRename", action="store_true", help="detect renames"),
                optparse.make_option("-C", dest="detectCopy", action="store_true", help="detect copies"),
                optparse.make_option("--import-local", dest="importIntoRemotes", action="store_false",
                                     help="Import into refs/heads/ , not refs/remotes"),
        ]
        self.description = "Submit changes from git to the perforce depot."
        self.usage += " [name of git branch to submit into perforce depot]"
        self.interactive = True
        self.origin = ""
        self.detectRename = False
        if gitConfig("git-p4.detectRename") == "true":
            self.detectRename = True
        self.detectCopy = False
        if gitConfig("git-p4.detectCopy") == "true":
            self.detectCopy = True
        self.verbose = False
        self.isWindows = (platform.system() == "Windows")
        self.updateP4Refs = True
        self.importIntoRemotes = True
        if gitConfig("git-p4.importIntoRemotes") == "false":
            self.importIntoRemotes = False
        self.abort = False

    def check(self):
        if len(self.p4.p4CmdList("opened ...")) > 0:
            die("You have files opened with perforce! Close them before starting the sync.")

    # replaces everything between 'Description:' and the next P4 submit template field with the
    # commit message
    def prepareLogMessage(self, template, message):
        result = ""

        inDescriptionSection = False

        for line in template.split("\n"):
            if line.startswith("#"):
                result += line + "\n"
                continue

            if inDescriptionSection:
                if line.startswith("Files:") or line.startswith("Jobs:"):
                    inDescriptionSection = False
                else:
                    continue
            else:
                if line.startswith("Description:"):
                    inDescriptionSection = True
                    line += "\n"
                    for messageLine in message.split("\n"):
                        line += "\t" + messageLine + "\n"

            result += line + "\n"

        return result

    def prepareSubmitTemplate(self,  clnumber=""):
        # remove lines in the Files section that show changes to files outside the depot path we're committing into
        template = ""
        inFilesSection = False
        for line in self.p4.p4_read_pipe_lines("change -o %s" % clnumber):
            if line.endswith("\r\n"):
                line = line[:-2] + "\n"
            if inFilesSection:
                if line.startswith("\t"):
                    # path starts and ends with a tab
                    path = line[1:]
                    lastTab = path.rfind("\t")
                    if lastTab != -1:
                        path = path[:lastTab]
                        if not path.startswith(self.depotPath):
                            continue
                else:
                    inFilesSection = False
            else:
                if line.startswith("Files:"):
                    inFilesSection = True

            template += line

        return template

    def applyPatch(self, id):
        diffcmd = "git format-patch -k --stdout \"%s^\"..\"%s\"" % (id, id)
        patchcmd = diffcmd + " | git apply "
        tryPatchCmd = patchcmd + "--check --ignore-whitespace --ignore-space-change -"
        applyPatchCmd = patchcmd + "--check --apply --ignore-whitespace --ignore-space-change -"

        if os.system(tryPatchCmd) != 0:
            print "Unfortunately applying the change failed!"
            print "What do you want to do?"
            response = "x"
            while response != "s" and response != "a" and response != "w":
                response = raw_input("[s]kip this patch / [a]pply the patch forcibly "
                                     "and with .rej files / [w]rite the patch to a file (patch.txt) ")
            if response == "s":
                print "Skipping! Good luck with the next patches..."
                return False
            elif response == "a":
                os.system(applyPatchCmd)
                if len(self.filesToAdd) > 0:
                    print "You may also want to call p4 add on the following files:"
                    print " ".join(self.filesToAdd)
                if len(self.filesToDelete):
                    print "The following files should be scheduled for deletion with p4 delete:"
                    print " ".join(self.filesToDelete)
                die("Please resolve and submit the conflict manually and "
                    + "continue afterwards with git-p4 submit --continue")
            elif response == "w":
                system(diffcmd + " > patch.txt")
                print "Patch saved to patch.txt in %s !" % self.clientPath
                die("Please resolve and submit the conflict manually and "
                    "continue afterwards with git-p4 submit --continue")

        system(applyPatchCmd)
        return True

    def integrateFile(self, diff, changelist=""):
        dest = self.p4.integrateFile(diff, changelist)
        if isModeExecChanged(diff['src_mode'], diff['dst_mode']):
            self.filesToChangeExecBit[dest] = diff['dst_mode']
        return dest

    def submitCommit(self, submitTemplate):
        changelist = 0
        output = self.p4.p4_read_write_pipe("submit -i", submitTemplate)
        print(output)
        match = re.search("Change ([0-9]+) submitted", output)
        if match:
            changelist = match.group(1)
        else:
            die("Couldn't get changelist number from 'submit'")
        return changelist

    def revertCommit(self):
        for f in self.editedFiles:
            self.p4.p4_system("revert \"%s\"" % escapeStringP4(f));
        for f in self.filesToAdd:
            self.p4.p4_system("revert \"%s\"" % escapeStringP4(f));
            system("rm %s" % escapeString(f))

    def setExecutableBits(self):
        # Set/clear executable bits
        for f in self.filesToChangeExecBit.keys():
            mode = self.filesToChangeExecBit[f]
            self.p4.setP4ExecBit(os.path.join(self.clientPath, f), mode)

    def manualSubmitMessage(self, fileName):
        print ("Perforce submit template written as %s. Please review/edit and then use p4 submit -i < %s to submit directly!"
               % (fileName, fileName))

    def submit(self, template, logMessage,  diff):
        # submit files to p4. Return changelist number, or 0 if not submitted
        changelist = 0
        chdir(self.clientPath)
        submitTemplate = self.prepareLogMessage(template, logMessage)
        if os.environ.has_key("P4DIFF"):
            del(os.environ["P4DIFF"])
        separatorLine = "######## everything below this line is just the diff #######"

        [handle, fileName] = tempfile.mkstemp()
        tmpFile = os.fdopen(handle, "w+")
        tmpFileContent = submitTemplate + separatorLine + "\n" + diff
        if self.isWindows:
            tmpFileContent = tmpFileContent.replace("\n", "\r\n")
        tmpFile.write(tmpFileContent)
        tmpFile.close()
        if self.interactive:
            mtime = os.stat(fileName).st_mtime
            defaultEditor = "vi"
            if self.isWindows:
                defaultEditor = "notepad"
            if os.environ.has_key("P4EDITOR"):
                editor = os.environ.get("P4EDITOR")
            else:
                editor = os.environ.get("EDITOR", defaultEditor);
            system(editor + " " + fileName)

            response = "y"
            if os.stat(fileName).st_mtime <= mtime:
                response = "x"
                while response != "y" and response != "a":
                    print ""
                    response = raw_input("Submit template unchanged. Submit anyway? [y]es, [a]bort ")
                if response == "a":
                    self.abort = True;
        else:
            response = "y"

        if response == "y":
            # rewrite the file with everything below separatorLine stripped
            tmpFile = open(fileName, "r")
            message = tmpFile.read()
            tmpFile.close()
            submitTemplate = message[:message.index(separatorLine)]
            changelist = self.submitCommit(submitTemplate)
        else:
            self.revertCommit()

        os.remove(fileName)
        return changelist

    def addOrDeleteFiles(self, changelist=""):
        for f in self.filesToAdd:
            # don't do p4 escaping when adding files (http://www.perforce.com/perforce/doc.current/manuals/cmdref/o.fspecs.html)
            # In fact, we only need to escape the dollar sign
            self.p4.p4_system("add -f %s \"%s\"" % (changelist, escapeStringP4ForAdd(f)))
            if f in self.editedFiles:
                self.p4.p4_system("edit %s \"%s\"" % (changelist, escapeStringP4(f)))
        for f in self.filesToDelete:
            self.p4.p4_system("revert %s \"%s\"" % (changelist, escapeStringP4(f)))
            self.p4.p4_system("delete %s \"%s\"" % (changelist, escapeStringP4(f)))

    def addFilesToChangelist(self, id, diffOpts):
        self.filesToAdd = set()
        self.filesToDelete = set()
        self.editedFiles = set()
        self.filesToChangeExecBit = {}
        self.getChangedFiles(diffOpts, id)
        for f in self.editedFiles:
            self.p4.p4_system("edit \"%s\"" % escapeStringP4(f))

    def getChangedFiles(self, diffOpts, id):
        diff = read_pipe_lines("git diff-tree -r %s \"%s^\" \"%s\"" % (diffOpts, id, id))
        for line in diff:
            diff = parseDiffTreeEntry(line)
            modifier = diff['status']
            path = diff['src']
            if modifier == "M":
                if isModeExecChanged(diff['src_mode'], diff['dst_mode']):
                    self.filesToChangeExecBit[path] = diff['dst_mode']
                self.editedFiles.add(path)
            elif modifier == "A":
                self.filesToAdd.add(path)
                self.filesToChangeExecBit[path] = diff['dst_mode']
                if path in self.filesToDelete:
                    self.filesToDelete.remove(path)
            elif modifier == "D":
                self.filesToDelete.add(path)
                if path in self.filesToAdd:
                    self.filesToAdd.remove(path)
                if path in self.editedFiles:
                    self.editedFiles.remove(path)
            elif modifier == "R":
                self.editedFiles.add(self.integrateFile(diff))
                self.filesToDelete.add(diff['src'])
            elif modifier == "C":
                self.editedFiles.add(self.integrateFile(diff))
            else:
                # the following types are unsupported:
                # T (changed type, i.e. regular file, symlink, submodule, ...), U (unmerged),
                # X (Unknown), B (pairing broken)
                die("unknown modifier %s for %s" % (modifier, path))

    def applyCommit(self, id):
        print "Applying %s" % (read_pipe("git log --max-count=1 --pretty=oneline %s" % id))
        diffOpts = ("", "-M")[self.detectRename]
        diffOpts = (diffOpts, "-C")[self.detectCopy]
        self.addFilesToChangelist(id, diffOpts)

        if not self.applyPatch(id):
            self.revertCommit()

        self.addOrDeleteFiles()
        
        # Set/clear executable bits
        self.setExecutableBits()

        logMessage = extractLogMessageFromGitCommit(id)
        logMessage = logMessage.strip()

        #diff = p4_read_pipe("diff -du ...")
        #perforce's diff -du ... breaks if one of the files has been deleted. This is a p4 bug not a git-p4 bug
        diff = "\n".join( read_pipe_lines("git diff \"%s^\" \"%s\"" % (id, id)) )
        template = self.prepareSubmitTemplate()
        changelist = self.submit(template, logMessage, diff)
        
        # Add note
        if changelist > 0:
            cmd = 'git notes --ref=git-p4 add -m "[depot-paths = \\"%s\\": change = %s]" %s' % (self.depotPath, changelist, id)
            system(cmd)

    def applyCommits(self):
        while len(self.commits) > 0 and self.abort == False:
            commit = self.commits[0]
            self.commits = self.commits[1:]
            self.applyCommit(commit)

    def sync(self, settings):
        unused = settings
        self.p4.p4_system("sync ...")

    def run(self, args):
#        if len(args) == 0:
            # note: this part doesn't work so well.
            # so forcing it to be "master" if not otherwise explained.
#            self.master = "master"
#            self.master = currentGitBranch()
#            if len(self.master) == 0 or not gitBranchExists("refs/heads/%s" % self.master):
#                die("Detecting current git branch failed!")
#        elif len(args) == 1:
#            self.master = args[0]
#        else:
#            return False
        self.master = "master"

        allowSubmit = gitConfig("git-p4.allowSubmit")
        if len(allowSubmit) > 0 and not self.master in allowSubmit.split(","):
            die("%s is not in git-p4.allowSubmit" % self.master)

        [upstream, settings] = findUpstreamBranchPoint()
        self.depotPath = settings['depot-paths'][0]
        if len(self.origin) == 0:
            self.origin = upstream

        if self.verbose:
            print "Origin branch is " + self.origin

        if len(self.depotPath) == 0:
            print "Internal error: cannot locate perforce depot path from existing branches"
            sys.exit(128)

        self.clientPath = self.p4.p4Where(self.depotPath)

        if self.isWindows:
        	self.clientPath = self.clientPath.replace("\\", "/")

        if len(self.clientPath) == 0:
            print "Error: Cannot locate perforce checkout of %s in client view" % self.depotPath
            sys.exit(128)

        print "Perforce checkout for depot path %s located at %s" % (self.depotPath, self.clientPath)
        self.oldWorkingDirectory = os.getcwd()

        chdir(self.clientPath)
        print "Syncronizing p4 checkout..."
        self.sync(settings)

        self.check()

        self.commits = []
        for line in read_pipe_lines("git rev-list --no-merges %s..%s" % (self.origin, self.master)):
            self.commits.append(line.strip())
        self.commits.reverse()

        if self.verbose:
            print "Commits to apply: %s" % self.commits

        self.applyCommits()
        
        if self.abort == False:
            if self.updateP4Refs:
                system("git update-ref %s%s %s" % (getRefsPrefix(self.importIntoRemotes), self.master, self.master))

            if len(self.commits) == 0:
                print "All changes applied!"
                chdir(self.oldWorkingDirectory)

        return True

class P4Sync(Command):
    delete_actions = ( "delete", "move/delete", "purge" )
    merge_actions = ( "branch", "integrate" )
    
    def __init__(self):
        Command.__init__(self)
        self.options = [
                optparse.make_option("--branch", dest="branch"),
                optparse.make_option("--detect-branches", dest="detectBranches", action="store_true"),
                optparse.make_option("--changesfile", dest="changesFile"),
                optparse.make_option("--silent", dest="silent", action="store_true"),
                optparse.make_option("--detect-labels", dest="detectLabels", action="store_true"),
                optparse.make_option("--verbose", dest="verbose", action="store_true"),
                optparse.make_option("--debug", dest="debug", action="store_true"),
                optparse.make_option("--restart-import", dest="restartImport", action="store_true"),
                optparse.make_option("--import-local", dest="importIntoRemotes", action="store_false",
                                     help="Import into refs/heads/ , not refs/remotes"),
                optparse.make_option("--max-changes", dest="maxChanges"),
                optparse.make_option("--keep-path", dest="keepRepoPath", action='store_true',
                                     help="Keep entire BRANCH/DIR/SUBDIR prefix during import"),
                optparse.make_option("--use-client-spec", dest="useClientSpec", action='store_true',
                                     help="Only sync files that are included in the Perforce Client Spec"),
                optparse.make_option("--file-dump", dest="fileDump", action='store_true',
                                     help="Save file git-p4-dump instead of passing data through to git fast-import. Useful for large repositories."),
                optparse.make_option("--no-getuserlist", dest="getUserList", action='store_false',
                                     help="Don't get the list of users from Perforce if a commit author can't be found. Trust cached userlist instead."),
                optparse.make_option("--fuzzy-tags", dest="fuzzyTags", action='store_true',
                                     help="Create tag even if number of files doesn't match as long as all revisions of files in tag match."),
                optparse.make_option("--tree-filter", dest="treeFilter", action='store',
                                     help="Filter to apply to file names"),
                optparse.make_option("--msg-filter", dest="msgFilter", action='store',
                                     help="Filter to apply to commit message"),
                optparse.make_option("--content-filter", dest="contentFilter", action='store',
                                     help="Filter to apply to file content"),
        ]
        self.description = """Imports from Perforce into a git repository.\n
    example:
    //depot/my/project/ -- to import the current head
    //depot/my/project/@all -- to import everything
    //depot/my/project/@1,6 -- to import only from revision 1 to 6

    (a ... is not needed in the path p4 specification, it's added implicitly)"""

        self.usage += " //depot/path[@revRange]"
        self.silent = False
        self.createdBranches = set()
        self.branch = ""
        self.detectBranches = False
        self.detectLabels = False
        self.changesFile = ""
        self.syncWithOrigin = True
        self.verbose = False
        self.restartImport = False
        self.importIntoRemotes = True
        if gitConfig("git-p4.importIntoRemotes") == "false":
            self.importIntoRemotes = False
        self.maxChanges = ""
        self.isWindows = (platform.system() == "Windows")
        self.keepRepoPath = False
        self.depotPaths = None
        self.p4BranchesInGit = []
        self.cloneExclude = []
        self.useClientSpec = False
        self.fileDump = False
        self.clientSpecDirs = []
        self.markCounter = 1
        self.p4FileReader = P4FileReader
        self.debug = False
        self.depotPaths = []
        self.changeRange = ""
        self.initialParent = ""
        self.initialNoteParent = ""
        self.previousDepotPaths = []
        self.getUserList = True
        self.fuzzyTags = False
        self.changeListCommits = {} # changelist numbers and corresponding marks
        self.treeFilter = ""
        self.msgFilter = ""
        self.contentFilter = ""
        self.contentFilterDir = ""

        self.knownBranches = {}
        self.initialParents = {}

        self.lastLabelChange = 0 # changelist# of last processed label
        self.lastLabelFiles = [] # files included in last processed label
        
        if gitConfig("git-p4.syncFromOrigin") == "false":
            self.syncWithOrigin = False

    def extractFilesFromCommit(self, commit):
        self.cloneExclude = [re.sub(r"\.\.\.$", "", path)
                             for path in self.cloneExclude]
        tmpFiles = []
        filesString = ''
        fnum = 0
        while commit.has_key("depotFile%s" % fnum):
            path = commit["depotFile%s" % fnum]

            if [p for p in self.cloneExclude
                if path.startswith (p)]:
                found = False
            else:
                found = [p for p in self.depotPaths
                         if path.startswith (p)]

            if found:
                filesString += path + '\n'
                f = {}
                f["path"] = path
                f["rev"] = commit["rev%s" % fnum]
                f["action"] = commit["action%s" % fnum]
                f["type"] = commit["type%s" % fnum]
                tmpFiles.append(f)
            fnum += 1

        filteredFiles = self.applyFilter(self.treeFilter, filesString).split('\n')

        files = []
        i = 0
        for file in filteredFiles:
            if file:
                f = tmpFiles[i]
                f["targetPath"] = file
                files.append(f)
            i += 1
        return files

    def stripRepoPath(self, path, prefixes):
        if self.keepRepoPath:
            prefixes = [re.sub("^(//[^/]+/).*", r'\1', prefixes[0])]

        for p in prefixes:
            if path.startswith(p):
                path = path[len(p):]

        # paths come in percent-encoded for @,%,#,* (url-style)
        path = urllib.unquote(path)

        return path

    def splitFilesIntoBranches(self, commit):
        files = []
        filesString = ''
        fnum = 0
        while commit.has_key("depotFile%s" % fnum):
            path =  commit["depotFile%s" % fnum]
            found = [p for p in self.depotPaths
                     if path.startswith (p)]

            if found:
                f = {}
                f["path"] = path
                f["rev"] = commit["rev%s" % fnum]
                f["action"] = commit["action%s" % fnum]
                f["type"] = commit["type%s" % fnum]
                files.append(f)
                filesString += path + '\n'
            fnum += 1

        filteredFiles = self.applyFilter(self.treeFilter, filesString).split('\n')

        branches = {}
        i = 0
        for targetPath in filteredFiles:
            if not targetPath:
                continue
            f = files[i]
            f["targetPath"] = targetPath
            i += 1

            relPath = self.stripRepoPath(targetPath, self.depotPaths)

            for branch in self.knownBranches.keys():

                # add a trailing slash so that a commit into qt/4.2foo doesn't end up in qt/4.2
                if relPath.startswith(branch + "/"):
                    if branch not in branches:
                        branches[branch] = []
                    branches[branch].append(f)
                    break

        return branches

    ## Should move this out, doesn't use SELF.
    def readP4Files(self, files):
        filesForCommit = []
        filesToRead = []

        # filter files by clientspec. Also, don't get the contents of files
        # which we are to delete or purge.
        for f in files:
            includeFile = False
            excludeFile = False
            for val in self.clientSpecDirs:
                if f['path'].startswith(val[0]):
                    if val[1] > 0:
                        includeFile = True
                    else:
                        excludeFile = True

            if includeFile and not excludeFile:
                filesForCommit.append(f)
                if f['action'] not in self.delete_actions:
                    filesToRead.append(f)

        filedata = []
        if len(filesToRead) > 0:
            filedata = self.p4.p4CmdList('-x - print',
                                 stdin='\n'.join(['%s#%s' % (f['path'], f['rev'])
                                                  for f in filesToRead]),
                                 stdin_mode='w+')

            if "p4ExitCode" in filedata[0]:
                die("Problems executing p4. Error: [%d]."
                    % (filedata[0]['p4ExitCode']));

        # Perforce outputs a number of records for each file. The first one
        # contains basic information such as the change list and filename. This
        # is followed by a number of records containing only "code" and "data"
        # fields, which are the file data broken apart.
        j = 0;
        contents = {}
        # for each file,
        while j < len(filedata):
            stat = filedata[j]
            j += 1
            text = ''
            # if it's not the last file and it's code type is text, unicode, or
            # binary,
            while j < len(filedata) and filedata[j]['code'] in ('text', 'unicode', 'binary', 'utf16'):
                # add the contents of the file to text.
                # repeat for all other files.
                text += filedata[j]['data']
                del filedata[j]['data']
                j += 1

            if not stat.has_key('depotFile'):
                sys.stderr.write("p4 print fails with: %s\n" % repr(stat))
                continue

            if stat['type'] in ('text+ko', 'unicode+ko', 'binary+ko', 'utf16+ko'):
                text = re.sub(r'(?i)\$(Id|Header):[^$]*\$',r'$\1$', text)
            elif stat['type'] in ('text+k', 'ktext', 'kxtext', 'unicode+k', 'binary+k', 'utf16+k'):
                text = re.sub(r'\$(Id|Header|Author|Date|DateTime|Change|File|Revision):[^$\n]*\$',r'$\1$', text)

            contents[stat['depotFile']] = text

        for f in filesForCommit:
            path = f['path']
            if contents.has_key(path):
                f['data'] = contents[path]

        return filesForCommit

    def applyFilter(self, filter, path, cwd=os.getcwd(), env=os.environ.copy()):
        if not filter:
            return path

        # The tree filter is a script that gets the path of one or more files (in p4 notation, e.g. //depot/branch/file.txt)
        # on stdin. It can output a modified path on stdout, or return an empty string to ignore this file.
        # The msg filter is a script that gets the commit message on stdin and returns
        # a modified commit message.
        # The content filter gets called for text files and is passed the name of the file on stdin and can modify that.
        filterProc = subprocess.Popen(filter, stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.STDOUT, shell=True, env=env, cwd=cwd)
        output = filterProc.communicate(path)[0]
        return output.rstrip()

    def applyContentFilter(self, filter, path, data):
        if not filter:
            return data

        fileName = "%s/%s" % (self.contentFilterDir, os.path.basename(path))
        tmpfile = open(fileName, "w+")
        try:
            tmpfile.write(data)
            tmpfile.close()
            env = os.environ.copy()
            env["GIT_DIR"] = self.contentFilterDir
            self.applyFilter(filter, fileName, self.contentFilterDir, env)
            handle = open(fileName, "r")
            data = handle.read()
            handle.close
            os.remove(fileName)
            return data
        except:
            try:
                os.remove(fileName)
            except:
                pass
            return None

    def commit(self, details, files, branch, branchPrefixes, parent = "", noteParent = "", filterBranchPrefixes = True):
        # If the commit doesn't have any files (e.g. because all files got filtered out)
        # we ignore the commit. Note that we still get a merge commit because p4 reports
        # all files that were involved in the merge, so len(files) > 0.
        if len(files) == 0:
            print "no files to commit -- ignoring"
            return

        epoch = details["time"]
        author = details["user"]
        change = int(details["change"])

        if self.verbose:
            print "commit into %s" % branch

        if filterBranchPrefixes:
            # need to apply tree filter to branchPrefixes as well!
            filteredBranchPrefixes = []
            for p in branchPrefixes:
                filteredBranchPrefixes.append(self.applyFilter(self.treeFilter, p))
        else:
            filteredBranchPrefixes = branchPrefixes

        # start with reading files; if that fails, we should not
        # create a commit.
        new_files = []
        for f in files:
            if [p for p in filteredBranchPrefixes if f["targetPath"].startswith(p)]:
                new_files.append (f)
            else:
                sys.stderr.write("Ignoring file outside of prefix: %s (mapped to %s)\n" % (f["path"], f["targetPath"]))

        isMergeCommit = self.detectBranches and self.isMergeCommit(new_files)
        if isMergeCommit:
            # make fast-import flush all changes to disk and update the refs using the checkpoint
            # command so that we can try to find the branch parent in the git history
            self.gitStream.write("checkpoint\n\n");
            self.gitStream.flush();

        self.gitStream.write("commit %s\n" % branch)
        self.gitStream.write("mark :%s\n" % self.markCounter)
        committer = ""
        if author not in self.users and self.getUserList:
            self.getUserMapFromPerforceServer()
        if author in self.users:
            committer = "%s %s %s" % (self.users[author], epoch, self.tz)
        else:
            committer = "%s <a@b> %s %s" % (author, epoch, self.tz)

        self.gitStream.write("committer %s\n" % committer)

        description = self.applyFilter(self.msgFilter, details["desc"])
        self.gitStream.write("data <<EOT\n")
        self.gitStream.write(description)
        self.gitStream.write("\nEOT\n\n")
        if parent:
            if self.verbose:
                print "parent %s" % parent
            self.gitStream.write("from %s\n" % parent)

        if isMergeCommit:
            (parentChange, parentBranch) = self.getMergeParentCommit(new_files, change)
            # if parentBranch is None we probably have to deal with an integration where the source was ignored
            if parentBranch:
                if self.changeListCommits.has_key(parentBranch) and self.changeListCommits[parentBranch].has_key(parentChange):
                    self.gitStream.write("merge :%s\n" % self.changeListCommits[parentBranch][parentChange])
                else:
                    # need to get change number from git repo - it was imported before
                    commit = self.getGitCommitFromChange(parentBranch, parentChange)
                    if commit == None:
                        sys.stderr.write("Can't find second parent change for merge for changelist @%s (calculated @%s)\n" % (change, parentChange))
                    else:
                        self.gitStream.write("merge %s\n" % commit)

        for f in self.p4FileReader( new_files, self.clientSpecDirs ):
            if f["type"] == "apple":
                print "\nfile %s is a strange apple file that forks. Ignoring!" % f['path']
                continue

            relPath = self.stripRepoPath(f["targetPath"], branchPrefixes)

            if f['action'] in self.delete_actions:
                self.gitStream.write("D %s\n" % relPath)
            else:
                data = f['data']
                del f['data']

                mode = "644"
                if self.p4.isP4Exec(f["type"]):
                    mode = "755"
                elif f["type"] == "symlink":
                    mode = "120000"
                    # p4 print on a symlink contains "target\n", so strip it off
                    data = data[:-1]

                if f["type"].endswith("binary") and not any(f["path"].lower().endswith(x) for x in ('.jpg','.jpeg','.gif','.png','.bmp','.ico','.tif','tiff')):
                    data = ""

                if self.isWindows and f["type"].endswith("text"):
                    data = data.replace("\r\n", "\n")

                if data != None:
                    preFilterDataLen = len(data)
                else:
                    preFilterDataLen = 0

                # apply content filter
                if f["type"].endswith("text"):
                    data = self.applyContentFilter(self.contentFilter, relPath, data)

                if data == None:
                    errorFile = open("git-p4-errors", "a")
                    errorFile.write("\n# ### WARNING: data is None for file %s, type %s.\n" % (relPath, f["type"]))
                    errorFile.write("# Prior to running filter length was: %s. Changelist #%s\n" % (preFilterDataLen, details["change"]))
                    errorFile.write("# If this happens it might mean that p4 couldn't find the file content or that the file was stored with wrong file type in p4.\n")
                    errorFile.write("# Try and run: p4 print \"%s#%s\"\n\n" % (f["path"], f["rev"]))
                    errorFile.close()
                else:
                    self.gitStream.write("M %s inline %s\n" % (mode, relPath))
                    self.gitStream.write("data %s\n" % len(data))
                    self.gitStream.write(data)
                    self.gitStream.write("\n")

        for f in new_files:
            includeFile = False
            excludeFile = False
            relPath = self.stripRepoPath(f["targetPath"], branchPrefixes)
            if len(self.clientSpecDirs):
                for val in self.clientSpecDirs:
                    if f["targetPath"].startswith(val[0]):
                        if val[1] > 0:
                            includeFile = True
                        else:
                            excludeFile = True
            else:
                includeFile = True

            if includeFile and not excludeFile:
                if f['action'] in self.delete_actions:
                    self.gitStream.write("D %s\n" % relPath)

        self.gitStream.write("\n")
        
        #commit refs/notes/git-p4
        #mark :2
        #committer <someuser@example.com> 1289238991 +0100
        #data 21
        #Note added by git-p4
        #N inline :1
        #data <<EOT
        #[depot-paths = "//depot/": change = 33255]
        #EOT
        self.gitStream.write("commit refs/notes/git-p4\n")
        self.gitStream.write("mark :%s\n" % (self.markCounter + 1))
        self.gitStream.write("committer %s\n" % committer)
        self.gitStream.write("data 28\n")
        self.gitStream.write("Note added by git-p4 import\n")
        if len(noteParent) > 0:
            if self.verbose:
                print "note parent %s" % noteParent
            self.gitStream.write("from %s\n" % noteParent)
        self.gitStream.write("N inline :%s\n" % self.markCounter)
        self.gitStream.write("data <<EOT\n[depot-paths = \"%s\": change = %s"
                             % (','.join (branchPrefixes), details["change"]))
        if len(details['options']) > 0:
            self.gitStream.write(": options = %s" % details['options'])
        self.gitStream.write("]\nEOT\n\n")

        localBranch = branch[len(getRefsPrefix(self.importIntoRemotes)):]
        if not self.changeListCommits.has_key(localBranch):
            self.changeListCommits[localBranch] = {}
        self.changeListCommits[localBranch][change] = self.markCounter
        self.markCounter += 2

        if not self.detectBranches:
            self.commitLabel(details, branch, change)

    def getFilesForLabel(self, label, change):
        if change == self.lastLabelChange:
            return self.lastLabelFiles

        labelDetails = label[0]
        print "Getting files for label %s (change %s)" % (labelDetails["label"], change)

        files = self.p4.p4CmdList("files " +  ' '.join (['"%s@%s"' % (p, change)
                                            for p in labelDetails["Views"]]))

        filteredFiles = self.applyFilter(self.treeFilter, '\n'.join([info["depotFile"] for info in files])).split('\n')

        labelFiles = []
        for fileToCheck in filteredFiles:
            # Check if file (with applied filter and stripped //depot/) matches our branch
            for depot in self.depotPaths:
                if fileToCheck.startswith(depot):
                    fileToCheck = fileToCheck[len(depot):]
                    break;
            labelFiles.append(fileToCheck)

        self.lastLabelChange = change
        self.lastLabelFiles = labelFiles
        return labelFiles

    def commitLabel(self, details, branch, change):
        if self.labels.has_key(change):
            epoch = details["time"]
            author = details["user"]
            localBranch = branch[len(getRefsPrefix(self.importIntoRemotes)):]

            label = self.labels[change]
            labelDetails = label[0]
            labelRevisions = label[1]

            files = self.getFilesForLabel(label, change)
            cleanedFiles = []
            for fileToCheck in files:
                # Check if file (with applied filter and stripped //depot/) matches our branch
                if fileToCheck.startswith(localBranch):
                    cleanedFiles.append(fileToCheck)

            if len(cleanedFiles) == 0:
                # Label has no files on our branch
                return

            if self.verbose:
                print "Change %s for branch %s is labeled %s" % (change, localBranch, labelDetails)

            if len(cleanedFiles) == len(labelRevisions) or self.fuzzyTags:

                if cleanedFiles == labelRevisions or self.fuzzyTags:
                    if self.detectBranches:
                        self.gitStream.write("tag tag_%s_%s\n" % (localBranch, labelDetails["label"]))
                    else:
                        self.gitStream.write("tag tag_%s\n" % labelDetails["label"])
                    self.gitStream.write("from %s\n" % branch)

                    owner = labelDetails["Owner"]
                    tagger = ""
                    if author in self.users:
                        tagger = "%s %s %s" % (self.users[owner], epoch, self.tz)
                    else:
                        tagger = "%s <%s> %s %s" % (owner, owner, epoch, self.tz)
                    self.gitStream.write("tagger %s\n" % tagger)
                    self.gitStream.write("data <<EOT\n")
                    self.gitStream.write(labelDetails["Description"])
                    self.gitStream.write("\nEOT\n\n")

                else:
                    if not self.silent:
                        print ("Tag %s does not match with change %s: files do not match."
                               % (labelDetails["label"], change))

            else:
                if not self.silent:
                    print ("Tag %s does not match with change %s: file count is different (%s vs. %s from label)."
                           % (labelDetails["label"], change, len(cleanedFiles), len(labelRevisions)))

    def isMergeCommit(self, files):
        # we consider a changelist to be a merge if the majority of files have a
        # merge or integrate action.
        i = 0
        for info in files:
            if info["action"] in self.merge_actions:
                i = i + 1
        return i > len(files) / 2

    def getGitCommitFromChange(self, branch, change):
        # Returns the commit where change was imported into
        settings = None
        parent = 0
        while parent < 65535:
            commit = "p4/" + branch + "~%s" % parent
            settings = extractSettingsFromNotes(commit)
            if not settings:
                return None
            if settings.has_key("change") and int(settings["change"]) == change:
                return commit
            parent = parent + 1
        return None

    def getMergeParentCommit(self, files, changeNo):
        # find and return the highest changelist number that this merge is based on
        branches = self.createdBranches
        highestParentChange = 0
        parentBranch = None
        for info in files:
            filelog = self.p4.p4CmdList("filelog -i -h -m 2 \"%s@%s\"" % (info['path'], changeNo))
            if len(filelog) >= 2 and filelog[1].has_key("change0"):
                newChange = int(filelog[1]["change0"])
                tmpBranch = filelog[1]["depotFile"]
            elif len(filelog) >= 1 and filelog[0].has_key("how0,0") and filelog[0]["how0,0"] == "merge from":
                tmpBranch = filelog[0]["file0,0"]
                parentFileLog = self.p4.p4CmdList("filelog -i -h -m 2 \"%s@%s\"" % (tmpBranch, changeNo))
                newChange = int(parentFileLog[0]["change0"])
            else:
                continue
            # apply filter
            tmpBranch = self.applyFilter(self.treeFilter, tmpBranch)
            # strip of //depot/ from the beginning
            for depot in self.depotPaths:
                if tmpBranch.startswith(depot):
                    tmpBranch = tmpBranch[len(depot):]
                    break;
            for branch in branches:
                if tmpBranch.startswith(branch):
                    if branch != parentBranch and parentBranch:
                        sys.stderr.write("File integrations coming from different branches are not supported (have %s, now %s)" % (parentBranch, branch))
                    else:
                        parentBranch = branch;
                    break;
            if newChange > highestParentChange:
                highestParentChange = newChange
        return (highestParentChange, parentBranch)

    def getUserCacheFilename(self):
        home = os.environ.get("HOME", os.environ.get("USERPROFILE"))
        return home + "/.gitp4-usercache.txt"

    def getUserMapFromPerforceServer(self):
        if self.userMapFromPerforceServer:
            return
        self.users = {}

        for output in self.p4.p4CmdList("users"):
            if not output.has_key("User"):
                continue
            self.users[output["User"]] = output["FullName"] + " <" + output["Email"] + ">"


        s = ''
        for (key, val) in self.users.items():
            s += "%s\t%s\n" % (key, val)

        open(self.getUserCacheFilename(), "wb").write(s)
        self.userMapFromPerforceServer = True

    def loadUserMapFromCache(self):
        self.users = {}
        self.userMapFromPerforceServer = False
        try:
            cache = open(self.getUserCacheFilename(), "rb")
            lines = cache.readlines()
            cache.close()
            for line in lines:
                entry = line.strip().split("\t")
                self.users[entry[0]] = entry[1]
        except IOError:
            if self.verbose:
                print "IO Error processing line %s" % line
            if self.getUserList:
                self.getUserMapFromPerforceServer()
        except IndexError:
            if self.verbose:
                print "Index Error processing line %s" % line
            if self.getUserList:
                self.getUserMapFromPerforceServer()

    def getLabels(self):
        self.labels = {}

        l = self.p4.p4CmdList("labels %s..." % ' '.join (self.depotPaths))
        if len(l) > 0 and not self.silent:
            print "Finding files belonging to labels in %s" % `self.depotPaths`

        for output in l:
            if output['code'] == 'error':
                sys.stderr.write("p4 returned an error: %s\n"
                                 % output['data'])
                sys.exit(1)
            label = output["label"]
            details = self.p4.p4Cmd("label -o \"%s\"" % label)
            viewIdx = 0
            views = []
            while details.has_key("View%s" % viewIdx):
                views.append(details["View%s" % viewIdx])
                viewIdx = viewIdx + 1
                output["Views"] = views

            revisions = {}
            newestChange = 0
            if self.verbose:
                print "Querying files for label %s" % label
            for f in self.p4.p4CmdList("files "
                                  +  ' '.join (['"%s...@%s"' % (p, label)
                                                for p in self.depotPaths])):
                revisions[f["depotFile"]] = f["rev"]
                change = int(f["change"])
                if change > newestChange:
                    newestChange = change

            self.labels[newestChange] = [output, revisions]

        if self.verbose:
            print "Label changes: %s" % self.labels.keys()

    def guessProjectName(self):
        for p in self.depotPaths:
            if p.endswith("/"):
                p = p[:-1]
            p = p[p.strip().rfind("/") + 1:]
            if not p.endswith("/"):
               p += "/"
            return p

    def getBranchMapping(self):
        lostAndFoundBranches = set()
        sourceBranches = {}

        user = gitConfig("git-p4.branchUser")
        if len(user) > 0:
            command = "branches -u %s" % user
        else:
            command = "branches"

        for info in self.p4.p4CmdList(command):
            details = self.p4.p4Cmd("branch -o \"%s\"" % info["branch"])
            viewIdx = 0
            while details.has_key("View%s" % viewIdx):
                paths = details["View%s" % viewIdx].split(" ")
                viewIdx = viewIdx + 1
                # require standard //depot/foo/... //depot/bar/... mapping (or at least *.*)
                if len(paths) != 2 or not ( paths[0].endswith("/...") or paths[1].endswith("/...") or paths[0].endswith("/*.*") or paths[1].endswith("/*.*") ):
                    continue
                source = paths[0]
                destination = paths[1]
                ## HACK
                if source.startswith(self.depotPaths[0]) and destination.startswith(self.depotPaths[0]):
                    source = source[len(self.depotPaths[0]):-4]
                    destination = destination[len(self.depotPaths[0]):-4]

                    if destination in self.knownBranches:
                        if not self.silent:
                            print "p4 branch %s defines a mapping from %s to %s" % (info["branch"], source, destination)
                            print "but there exists another mapping from %s to %s already!" % (self.knownBranches[destination], destination)
                        continue

                    self.knownBranches[destination] = source
                    sourceBranches[source] = destination

                    lostAndFoundBranches.discard(destination)

                    if source not in self.knownBranches:
                        lostAndFoundBranches.add(source)

        # Perforce does not strictly require branches to be defined, so we also
        # check git config for a branch list.
        #
        # Example of branch definition in git config file:
        # [git-p4]
        #   branchList=main:branchA
        #   branchList=main:branchB
        #   branchList=branchA:branchC
        configBranches = gitConfigList("git-p4.branchList")
        for branch in configBranches:
            if branch:
                (source, destination) = branch.split(":")
                self.knownBranches[destination] = source

                lostAndFoundBranches.discard(destination)

                if source not in self.knownBranches:
                    lostAndFoundBranches.add(source)

        for branch in lostAndFoundBranches:
            self.knownBranches[branch] = branch

        # don't allow nested branches like foo/bla and foo as two separate branches.
        for branch in sourceBranches.keys():
            tmp = re.sub("^([^/]+)/.*", r'\1', branch)
            if tmp in sourceBranches and tmp != branch:
                del self.knownBranches[sourceBranches[branch]]
                del sourceBranches[branch]
        for branch in self.knownBranches.keys():
            tmp = re.sub("^([^/]+)/.*", r'\1', branch)
            if tmp in self.knownBranches and tmp != branch:
                del self.knownBranches[branch]

    def getBranchMappingFromGitBranches(self):
        branches = p4BranchesInGit(self.importIntoRemotes)
        for branch in branches.keys():
            if branch == "master":
                branch = "main"
            else:
                branch = branch[len(self.projectName):]
            self.knownBranches[branch] = branch

    def listExistingP4GitBranches(self):
        # branches holds mapping from name to commit
        branches = p4BranchesInGit(self.importIntoRemotes)
        self.p4BranchesInGit = branches.keys()
        for branch in branches.keys():
            self.initialParents[self.refPrefix + branch] = branches[branch]

    def updateOptionDict(self, d):
        option_keys = {}
        if self.keepRepoPath:
            option_keys['keepRepoPath'] = 1

        d["options"] = ' '.join(sorted(option_keys.keys()))

    def readOptions(self, d):
        self.keepRepoPath = (d.has_key('options')
                             and ('keepRepoPath' in d['options']))

    def gitRefForBranch(self, branch):
        if branch == "main":
            return self.refPrefix + "master"

        if len(branch) <= 0:
            return branch

        return self.refPrefix + self.projectName + branch

    def gitCommitByP4Change(self, ref, change):
        if self.verbose:
            print "looking in ref " + ref + " for change %s using bisect..." % change

        earliestCommit = ""
        latestCommit = parseRevision(ref)

        while True:
            if self.verbose:
                print "trying: earliest %s latest %s" % (earliestCommit, latestCommit)
            nextCommit = read_pipe("git rev-list --bisect %s %s" % (latestCommit, earliestCommit)).strip()
            if len(nextCommit) == 0:
                if self.verbose:
                    print "argh"
                return ""
            settings = extractSettingsFromNotes(nextCommit)
            currentChange = int(settings['change'])
            if self.verbose:
                print "current change %s" % currentChange

            if currentChange == change:
                if self.verbose:
                    print "found %s" % nextCommit
                return nextCommit

            if currentChange < change:
                earliestCommit = "^%s" % nextCommit
            else:
                latestCommit = "%s" % nextCommit

        return ""

    def importNewBranch(self, branch, maxChange):
        # make fast-import flush all changes to disk and update the refs using the checkpoint
        # command so that we can try to find the branch parent in the git history
        self.gitStream.write("checkpoint\n\n");
        self.gitStream.flush();
        branchPrefix = self.depotPaths[0] + branch + "/"
        commitRange = "@1,%s" % maxChange
        if self.verbose:
            print "!!!!prefix" + branchPrefix
        changes = self.p4.p4ChangesForPaths([branchPrefix], commitRange)
        if len(changes) <= 0:
            return False
        firstChange = changes[0]
        #print "first change in branch: %s" % firstChange
        sourceBranch = self.knownBranches[branch]
        sourceDepotPath = self.depotPaths[0] + sourceBranch
        sourceRef = self.gitRefForBranch(sourceBranch)
        #print "source " + sourceBranch

        sourceChanges = self.p4.p4Cmd("changes -m 1 %s/...@1,%s" % (sourceDepotPath, firstChange))
        if sourceChanges.has_key("change"):
            branchParentChange = int(sourceChanges["change"])
            #print "branch parent: %s" % branchParentChange
            gitParent = self.gitCommitByP4Change(sourceRef, branchParentChange)
            if len(gitParent) > 0:
                self.initialParents[self.gitRefForBranch(branch)] = gitParent
                #print "parent git commit: %s" % gitParent

        self.importChanges(changes)
        return True

    def importChanges(self, changes, restartImport = False):
        cnt = 0
        for change in changes:
            description = self.p4.p4Cmd("describe -s %s" % change)
            self.updateOptionDict(description)

            cnt = cnt + 1
            if not self.silent:
                sys.stdout.write("\rImporting revision %s (%s%%)\n" % (change, cnt * 100 / len(changes)))
                sys.stdout.flush()

            if self.detectBranches:
                branches = self.splitFilesIntoBranches(description)
                for branch in branches.keys():
                    ## HACK  --hwn
                    branchPrefix = self.depotPaths[0] + branch + "/"

                    parent = ""

                    filesForCommit = branches[branch]

                    if self.verbose:
                        print "branch is %s" % branch

                    self.updatedBranches.add(branch)

                    if branch not in self.createdBranches:
                        self.createdBranches.add(branch)
                        parent = self.knownBranches[branch]
                        if parent == branch:
                            parent = ""
                        else:
                            fullBranch = self.projectName + branch
                            if fullBranch not in self.p4BranchesInGit:
                                if not self.silent:
                                    print("\n    Importing new branch %s" % fullBranch);
                                if self.importNewBranch(branch, change - 1) or parent not in self.createdBranches:
                                    parent = ""
                                    self.p4BranchesInGit.append(fullBranch)
                                if not self.silent:
                                    print("\n    Resuming with change %s" % change);

                            # We don't want to set a parent for a new branch since that would result in a tree
                            # that starts out with the parent branch. We want to start with an empty tree since
                            # we list all files that should go into the branch.
                            parent = ""

                    branch = self.gitRefForBranch(branch)
                    parent = self.gitRefForBranch(parent)

                    if self.verbose:
                        print "looking for initial parent for %s; current parent is %s" % (branch, parent)

                    if len(parent) == 0 and branch in self.initialParents:
                        parent = self.initialParents[branch]
                        del self.initialParents[branch]

                    self.branch = branch
                    self.commit(description, filesForCommit, branch, [branchPrefix], parent, self.initialNoteParent, False)
                    self.initialNoteParent = ""

                # Add labels for this commit. A label may affect a branch even though the current
                # change doesn't touch any files in that branch
                p4Branches = p4BranchesInGit(self.importIntoRemotes)
                for branch in p4Branches:
                    self.commitLabel(description, self.gitRefForBranch(branch), change)
                for branch in self.createdBranches:
                    if not branch in p4Branches:
                        self.commitLabel(description, self.gitRefForBranch(branch), change)
            else:
                files = self.extractFilesFromCommit(description)
                if (cnt == 1 and restartImport):
                    parent = "%s^0" % self.branch
                    noteParent = "refs/notes/git-p4^0"
                    self.commit(description, files, self.branch, self.depotPaths,
                                parent, noteParent)
                else:
                    self.commit(description, files, self.branch, self.depotPaths,
                                self.initialParent, self.initialNoteParent)
                self.initialParent = ""
                self.initialNoteParent = ""

    def importHeadRevision(self, revision):
        print "Doing initial import of %s from revision %s into %s" % (' '.join(self.depotPaths), revision, self.branch)

        details = {}
        details["user"] = "git perforce import user"
        details["desc"] = ("Initial import of %s from the state at revision %s"
                           % (' '.join(self.depotPaths), revision))
        details["change"] = revision
        newestRevision = 0

        fileCnt = 0
        for info in self.p4.p4CmdList("files "
                              +  ' '.join(["%s...%s"
                                           % (p, revision)
                                           for p in self.depotPaths])):

            if 'code' in info and info['code'] == 'error':
                sys.stderr.write("p4 returned an error: %s\n"
                                 % info['data'])
                if info['data'].find("must refer to client") >= 0:
                    sys.stderr.write("This particular p4 error is misleading.\n")
                    sys.stderr.write("Perhaps the depot path was misspelled.\n");
                    sys.stderr.write("Depot path:  %s\n" % " ".join(self.depotPaths))
                sys.exit(1)
            if 'p4ExitCode' in info:
                sys.stderr.write("p4 exitcode: %s\n" % info['p4ExitCode'])
                sys.exit(1)


            change = int(info["change"])
            if change > newestRevision:
                newestRevision = change

            if info["action"] in self.delete_actions:
                # don't increase the file cnt, otherwise details["depotFile123"] will have gaps!
                #fileCnt = fileCnt + 1
                continue

            for prop in ["depotFile", "rev", "action", "type" ]:
                details["%s%s" % (prop, fileCnt)] = info[prop]

            fileCnt = fileCnt + 1

        details["change"] = newestRevision

        # Use time from top-most change so that all git-p4 clones of
        # the same p4 repo have the same commit SHA1s.
        res = self.p4.p4CmdList("describe -s %d" % newestRevision)
        newestTime = None
        for r in res:
            if r.has_key('time'):
                newestTime = int(r['time'])
        if newestTime is None:
            die("\"describe -s\" on newest change %d did not give a time")
        details["time"] = newestTime

        self.updateOptionDict(details)
        self.commit(details, self.extractFilesFromCommit(details), self.branch, self.depotPaths)


    def getClientSpec(self):
        # fill in self.clientSpecDirs, with a map from folder names to an
        # integer. If the integer is positive, the folder name maps to its
        # length. If the integer is negative, the folder name maps to its
        # negative length, and was explicitly excluded.
        specList = self.p4.p4CmdList( "client -o" )
        temp = {}
        for entry in specList:
            for k,v in entry.iteritems():
                if k.startswith("View"):
                    if v.startswith('"'):
                        start = 1
                    else:
                        start = 0
                    index = v.find("...")
                    v = v[start:index]
                    if v.startswith("-"):
                        v = v[1:]
                        temp[v] = -len(v)
                    else:
                        temp[v] = len(v)
        self.clientSpecDirs = temp.items()
        self.clientSpecDirs.sort( lambda x, y: abs( y[1] ) - abs( x[1] ) )

    def CalculateLastImportedP4ChangeList(self):
        p4Change = 0
        for branch in self.p4BranchesInGit:
            settings = extractSettingsFromNotes(self.refPrefix + branch)
            if self.verbose:
                print "settings:"
                print self.refPrefix + branch
                print settings
            self.readOptions(settings)
            if (settings.has_key('depot-paths')
                and settings.has_key ('change')):
                change = int(settings['change']) + 1
                p4Change = max(p4Change, change)

                depotPaths = sorted(settings['depot-paths'])
                if self.previousDepotPaths == []:
                    self.previousDepotPaths = depotPaths
                else:
                    paths = []
                    for (prev, cur) in zip(self.previousDepotPaths, depotPaths):
                        for i in range(0, min(len(cur), len(prev))):
                            if cur[i] <> prev[i]:
                                i = i - 1
                                break

                        paths.append (cur[:i + 1])

                    self.previousDepotPaths = paths

        if p4Change > 0:
            self.depotPaths = sorted(self.previousDepotPaths)
            self.changeRange = "@%s,#head" % p4Change
            if not self.silent and not self.detectBranches:
                print "Performing incremental import into %s git branch" % self.branch
        
    def adjustDepotPaths(self):
        revision = ""
        newPaths = []
        for p in self.depotPaths:
            if p.find("@") != -1:
                atIdx = p.index("@")
                self.changeRange = p[atIdx:]
                if self.changeRange == "@all":
                    self.changeRange = ""
                elif self.changeRange == "@1":
                    # this is the very first changelist, so we'll import as
                    # a regular change
                    revision = ""
                elif ',' not in self.changeRange:
                    revision = self.changeRange
                    self.changeRange = ""
                p = p[:atIdx]
            elif p.find("#") != -1:
                hashIdx = p.index("#")
                revision = p[hashIdx:]
                p = p[:hashIdx]
            elif self.previousDepotPaths == []:
                revision = "#head"

            p = re.sub ("\.\.\.$", "", p)
            if not p.endswith("/"):
                p += "/"

            newPaths.append(p)

        self.depotPaths = newPaths
        return revision

    def detectP4Branches(self):
        self.projectName = ""

        if self.hasOrigin:
            self.getBranchMappingFromGitBranches()
        else:
            self.getBranchMapping()
        if self.verbose:
            print "p4-git branches: %s" % self.p4BranchesInGit
            print "initial parents: %s" % self.initialParents
        for b in self.p4BranchesInGit:
            if b != "master":

                ## FIXME
                b = b[len(self.projectName):]
            self.createdBranches.add(b)

    def cleanup(self):
        if self.contentFilterDir:
            system("rm -rf %s" % self.contentFilterDir)

    def run(self, args):
        if self.debug:
            self.verbose = True

        if self.contentFilter:
            # create temporary directory if we have a content filter
            self.contentFilterDir = tempfile.mkdtemp()
            env = os.environ.copy()
            env["GIT_DIR"] = self.contentFilterDir
            subprocess.Popen("git init .", cwd=self.contentFilterDir, shell=True, env=env)
            subprocess.Popen('git config core.whitespace "blank-at-eol,space-before-tab,indent-with-non-tab,blank-at-eof,trailing-space,cr-at-eol,tabwidth=4"', cwd=self.contentFilterDir, shell=True, env=env)
            
        # map from branch depot path to parent branch
        self.hasOrigin = originP4BranchesExist()
        if not self.syncWithOrigin:
            self.hasOrigin = False

        self.refPrefix = getRefsPrefix(self.importIntoRemotes)

        if self.syncWithOrigin and self.hasOrigin:
            if not self.silent:
                print "Syncing with origin first by calling git fetch origin"
            system("git fetch origin")

        if len(self.branch) == 0:
            if self.verbose:
                print "len(self.branch) == 0"
            self.branch = self.refPrefix + "master"
            if gitBranchExists("refs/heads/p4") and self.importIntoRemotes:
                system("git update-ref %s refs/heads/p4" % self.branch)
                system("git branch -D p4");
            # create it /after/ importing, when master exists
            if not gitBranchExists(self.refPrefix + "HEAD") and self.importIntoRemotes and gitBranchExists(self.branch):
                system("git symbolic-ref %sHEAD %s" % (self.refPrefix, self.branch))

        if self.useClientSpec or gitConfig("git-p4.useclientspec") == "true":
            self.getClientSpec()

        # TODO: should always look at previous commits,
        # merge with previous imports, if possible.
        if args == [] or self.detectBranches:
            if self.hasOrigin:
                createOrUpdateBranchesFromOrigin(self.refPrefix, self.silent)
            self.listExistingP4GitBranches()

            if len(self.p4BranchesInGit) > 1:
                if not self.silent:
                    print "Importing from/into multiple branches"
                self.detectBranches = True

            if self.verbose:
                print "branches: %s" % self.p4BranchesInGit
            self.CalculateLastImportedP4ChangeList()

        if not self.detectBranches:
            self.initialParent = parseRevision(self.branch)

        self.initialNoteParent = parseRevision("refs/notes/git-p4")

        if not self.branch.startswith("refs/"):
            self.branch = "refs/heads/" + self.branch

        if len(args) == 0 and self.depotPaths:
            if self.verbose:
                print "Depot paths: \n%s" % '\n'.join(self.depotPaths)
        else:
            if self.depotPaths and self.depotPaths != args and not self.detectBranches:
                print ("previous import used depot path %s and now %s was specified. "
                       "This doesn't work!" % (' '.join (self.depotPaths),
                                               ' '.join (args)))
                sys.exit(1)

            self.depotPaths = sorted(args)

        if self.verbose:
            print "self.depotPaths: "
            print self.depotPaths

        self.users = {}

        revision = self.adjustDepotPaths()

        self.loadUserMapFromCache()
        self.labels = {}
        if self.detectLabels:
            self.getLabels();

        if self.detectBranches:
            self.detectP4Branches()
        
        self.tz = "%+03d%02d" % (- time.timezone / 3600, ((- time.timezone % 3600) / 60))

        if self.fileDump:
            self.gitStream = open("git-p4-dump", "wb")
        else:
            if self.debug:
                tmpfile = "%s/p4import" % tempfile.gettempdir()
                print "Writing input for 'git fast-import' to %s\n" % tmpfile
                debugDumpFile = open(tmpfile, "w")
            else:
                debugDumpFile = None

            fastImportCmd = ["git", "fast-import"]
            if not self.verbose:
                fastImportCmd.append("--quiet")
            importProcess = subprocess.Popen(fastImportCmd,
                                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            self.gitStream = LargeFileWriter(importProcess.stdin, debugDumpFile)

        try:
            if revision:
                self.importHeadRevision(revision)
            else:
                changes = []

                if len(self.changesFile) > 0:
                    output = open(self.changesFile).readlines()
                    changeSet = set()
                    for line in output:
                        changeSet.add(int(line))

                    for change in changeSet:
                        changes.append(change)

                    changes.sort()
                else:
                    print "Getting p4 changes for %s...%s" % (', '.join(self.depotPaths),
                                                                  self.changeRange)
                    changes = self.p4.p4ChangesForPaths(self.depotPaths, self.changeRange)
                    if self.verbose:
                        print "Found %i changes" % len(changes)
                    if len(self.maxChanges) > 0:
                        changes = changes[:min(int(self.maxChanges), len(changes))]

                if len(changes) == 0:
                    if not self.silent:
                        print "No changes to import!"
                    return True

                if not self.silent and not self.detectBranches:
                    print "Import destination: %s" % self.branch

                self.updatedBranches = set()
                # http://www.kerneltrap.com/mailarchive/git/2009/7/7/6203
                # To restart an import, you need to use the from command in the
                # first commit of that session, e.g. to restart an import on
                # refs/heads/master use:
                #
                #  from refs/heads/master^0
                self.importChanges(changes, self.restartImport)

                if not self.silent:
                    print ""
                    if len(self.updatedBranches) > 0:
                        sys.stdout.write("Updated branches: ")
                        for b in self.updatedBranches:
                            sys.stdout.write("%s " % b)
                        sys.stdout.write("\n")

            self.gitStream.flush()

        except IOError:
            self.cleanup()
            die("fast-import failed")

        if self.fileDump:
            print "Finished processing. Data may be manually utilized now (e.g. sent to git fast-import)"
        else:
            if debugDumpFile:
                debugDumpFile.close()

            importProcess.communicate()

            if importProcess.returncode != 0:
                self.cleanup()
                die("fast-import failed")

        self.cleanup()
        return True

class P4Rebase(P4Sync):
    def __init__(self):
        P4Sync.__init__(self)
        self.options += [
        ]
        self.description = ("Fetches the latest revision from perforce and "
                            + "rebases the current work (branch) against it")

    def run(self, args):
        P4Sync.run(self, args)

        return self.rebase()

    def rebase(self):
        if os.system("git update-index --refresh") != 0:
            die("Some files in your working directory are modified and different than what is in your index. You can use git update-index <filename> to bring the index up-to-date or stash away all your changes with git stash.");
        if len(read_pipe("git diff-index HEAD --")) > 0:
            die("You have uncommited changes. Please commit them before rebasing or stash them away with git stash.");
        [upstream, settings] = findUpstreamBranchPoint()
        if len(upstream) == 0:
            die("Cannot find upstream branchpoint for rebase")

        # the branchpoint may be p4/foo~3, so strip off the parent
        upstream = re.sub("~[0-9]+$", "", upstream)

        print "Rebasing the current branch onto %s" % upstream
        oldHead = read_pipe("git rev-parse HEAD").strip()
        system("git rebase %s" % upstream)
        system("git diff-tree --stat --summary -M %s HEAD" % oldHead)
        return True

class P4Clone(P4Sync):
    def __init__(self):
        P4Sync.__init__(self)
        self.description = "Creates a new git repository and imports from Perforce into it"
        self.usage = "usage: %prog [options] //depot/path[@revRange]"
        self.options += [
            optparse.make_option("--destination", dest="cloneDestination",
                                 action='store', default=None,
                                 help="where to leave result of the clone"),
            optparse.make_option("-/", dest="cloneExclude",
                                 action="append", type="string",
                                 help="exclude depot path"),
            optparse.make_option("--bare", dest="cloneBare",
                                 action="store_true", default=False)
        ]
        self.cloneDestination = None
        self.needsGit = False
        self.cloneBare = False

    # This is required for the "append" cloneExclude action
    def ensure_value(self, attr, value):
        if not hasattr(self, attr) or getattr(self, attr) is None:
            setattr(self, attr, value)
        return getattr(self, attr)

    def defaultDestination(self, args):
        ## TODO: use common prefix of args?
        depotPath = args[0]
        depotDir = re.sub("(@[^@]*)$", "", depotPath)
        depotDir = re.sub("(#[^#]*)$", "", depotDir)
        depotDir = re.sub(r"\.\.\.$", "", depotDir)
        depotDir = re.sub(r"/$", "", depotDir)
        return os.path.split(depotDir)[1]

    def run(self, args):
        if len(args) < 1:
            return False

        if self.keepRepoPath and not self.cloneDestination:
            sys.stderr.write("Must specify destination for --keep-path\n")
            sys.exit(1)

        depotPaths = args

        if not self.cloneDestination and len(depotPaths) > 1:
            self.cloneDestination = depotPaths[-1]
            depotPaths = depotPaths[:-1]

        self.cloneExclude = ["/"+p for p in self.cloneExclude]
        for p in depotPaths:
            if not p.startswith("//"):
                return False

        if not self.cloneDestination:
            self.cloneDestination = self.defaultDestination(args)

        print "Importing from %s into %s" % (', '.join(depotPaths), self.cloneDestination)
        if not os.path.exists(self.cloneDestination):
            os.makedirs(self.cloneDestination)
        chdir(self.cloneDestination)
        init_cmd = [ "git", "init" ]
        if self.cloneBare:
            init_cmd.append("--bare")
        subprocess.check_call(init_cmd)
        system("git config notes.rewrite.amend true")
        system("git config notes.rewrite.rebase true")
        system("git config --add notes.rewriteRef refs/notes/*")
        system("git config --add notes.displayRef refs/notes/git-p4")
        self.gitdir = os.getcwd() + "/.git"
        if not P4Sync.run(self, depotPaths):
            return False
        if self.branch != "master":
            masterbranch = getRefsPrefix(self.importIntoRemotes) + "master"
            branchname = "master"
            if self.detectBranches:
                masterbranch = self.branch
                branchname = self.branch[self.branch.strip().rfind("/") + 1:]
            if gitBranchExists(masterbranch):
                system("git branch %s %s" % (branchname, masterbranch))
                if not self.cloneBare:
                    system("git checkout -f %s" % branchname)
            else:
                print "Could not detect main branch. No checkout/master branch created."

        return True

class P4Branches(Command):
    def __init__(self):
        Command.__init__(self)
        self.options = [ ]
        self.description = ("Shows the git branches that hold imports and their "
                            + "corresponding perforce depot paths")
        self.verbose = False

    def run(self, args):
        unused = args
        if originP4BranchesExist():
            createOrUpdateBranchesFromOrigin()

        cmdline = "git rev-parse --symbolic "
        cmdline += " --remotes"

        for line in read_pipe_lines(cmdline):
            line = line.strip()

            if not line.startswith('p4/') or line == "p4/HEAD":
                continue
            branch = line

            settings = extractSettingsFromNotes("refs/remotes/%s" % branch)

            print "%s <= %s (%s)" % (branch, ",".join(settings["depot-paths"]), settings["change"])
        return True

class P4Shelve(P4Submit):
    def __init__(self):
        P4Submit.__init__(self)
        self.options += [
            optparse.make_option("-c", dest="clnumber",
                                 action='store', default="",
                                 help="existing changelist to use for shelving"), 
            optparse.make_option("-d",
                                 dest="deleteShelve", action="store_true",
                                 help="delete previously shelved files")
        ]
        self.description = "Shelve changes from git to the perforce depot."
        self.clnumber = ""
        self.existingClNumber = ""
        self.deleteShelve = False
        self.updateP4Refs = False
        
    def integrateFile(self, diff, changelist=""):
        changelist = "-c %s" % self.clnumber
        P4Submit.integrateFile(self, diff, changelist)

    def submitCommit(self, submitTemplate):
        self.p4.p4_write_pipe("shelve -r -i", submitTemplate)
        self.p4.p4_system("revert -c %s \"%s...\"" % (self.clnumber, escapeStringP4(self.depotPath)))
        print("")
        print("Shelved files in Perforce changelist # %s" % self.clnumber)
        return self.clnumber

    def revertCommit(self):
        P4Submit.revertCommit(self)
        if self.existingClNumber == "":
            self.p4.p4_system("change -d %s" % self.clnumber)

    def manualSubmitMessage(self, fileName):
        print ("Perforce shelve template written as %s. Please review/edit and then use p4 shelve -c %s -i < %s to shelve directly!"
               % (fileName, self.clnumber, fileName))

    def applyCommits(self):
        if len(self.commits) == 0:
            die("Fatal error: No commits to shelve")
        firstCommit = self.commits[0]
        lastCommit = self.commits[len(self.commits) - 1]
        if firstCommit == lastCommit:
            firstCommit = lastCommit + "^"

        logMessage = ""
        for commit in self.commits:
            print "Shelving %s" % (read_pipe("git log --max-count=1 --pretty=oneline %s" % commit))
            logMessage += extractLogMessageFromGitCommit(commit)

        logMessage = logMessage.strip()

        if self.clnumber == "":
            # Create new changelist
            template = self.prepareSubmitTemplate()
            submitTemplate = self.prepareLogMessage(template, logMessage)
            self.clnumber = self.p4.p4_read_write_pipe("change -i", submitTemplate).split(' ')[1]
        else:
            self.existingClNumber = self.clnumber

        # Add files to changelist
        diffOpts = ("", "-M")[self.detectRename]
        diffOpts = (diffOpts, "-C")[self.detectCopy]

        self.filesToAdd = set()
        self.filesToDelete = set()
        self.editedFiles = set()
        self.filesToChangeExecBit = {}
        for commit in self.commits:
            self.getChangedFiles(diffOpts, commit)
        for editedFile in self.editedFiles:
            if editedFile not in self.filesToAdd:
                self.p4.p4_system("edit -c %s \"%s\"" % (self.clnumber, escapeStringP4(editedFile)))

        for commit in self.commits:
            if not self.applyPatch(commit):
                self.revertCommit()
                break

        self.addOrDeleteFiles("-c %s" % self.clnumber)

        # Set/clear executable bits
        self.setExecutableBits()

        diff = "\n".join( read_pipe_lines("git diff \"%s\" \"%s\"" % (firstCommit, lastCommit)) )
        template = self.prepareSubmitTemplate(self.clnumber)
        self.submit(template, logMessage, diff)

    def sync(self, settings):
        change = settings["change"]
        self.p4.p4_system("sync ...@%s" % change)

    def run(self, args):
        if self.deleteShelve:
            if self.clnumber == "":
                die("Fatal error: need to specify changelist number to delete shelved files")

            self.p4.p4_system("shelve -d -c %s" % self.clnumber)
            self.p4.p4_system("change -d %s" % self.clnumber)
            return True

        return P4Submit.run(self, args)

class HelpFormatter(optparse.IndentedHelpFormatter):
    def __init__(self):
        optparse.IndentedHelpFormatter.__init__(self)

    def format_description(self, description):
        if description:
            return description + "\n"
        else:
            return ""

def printUsage(cmds):
    print "usage: %s <command> [options]" % sys.argv[0]
    print ""
    print "valid commands: %s" % ", ".join(cmds)
    print ""
    print "Try %s <command> --help for command specific help." % sys.argv[0]
    print ""

commands = {
    "debug" : P4Debug,
    "submit" : P4Submit,
    "commit" : P4Submit,
    "sync" : P4Sync,
    "rebase" : P4Rebase,
    "clone" : P4Clone,
    "rollback" : P4RollBack,
    "branches" : P4Branches, 
    "shelve"   : P4Shelve
}


def main():
    if len(sys.argv[1:]) == 0:
        printUsage(commands.keys())
        sys.exit(2)

    cmd = ""
    cmdName = sys.argv[1]
    try:
        klass = commands[cmdName]
        cmd = klass()
    except KeyError:
        print "unknown command %s" % cmdName
        print ""
        printUsage(commands.keys())
        sys.exit(2)

    options = cmd.options
    cmd.gitdir = os.environ.get("GIT_DIR", None)

    args = sys.argv[2:]

    if len(options) > 0:
        options.append(optparse.make_option("--git-dir", dest="gitdir"))

        parser = optparse.OptionParser(cmd.usage.replace("%prog", "%prog " + cmdName),
                                       options,
                                       description = cmd.description,
                                       formatter = HelpFormatter())

        (cmd, args) = parser.parse_args(sys.argv[2:], cmd);
    global verbose
    verbose = cmd.verbose
    if cmd.needsGit:
        if cmd.gitdir == None:
            cmd.gitdir = os.path.abspath(".git")
            if not isValidGitDir(cmd.gitdir):
                cmd.gitdir = read_pipe("git rev-parse --git-dir").strip()
                if os.path.exists(cmd.gitdir):
                    cdup = read_pipe("git rev-parse --show-cdup").strip()
                    if len(cdup) > 0:
                        chdir(cdup);

        if not isValidGitDir(cmd.gitdir):
            if isValidGitDir(cmd.gitdir + "/.git"):
                cmd.gitdir += "/.git"
            else:
                die("fatal: cannot locate git repository at %s" % cmd.gitdir)

        os.environ["GIT_DIR"] = cmd.gitdir

    if not cmd.run(args):
        parser.print_help()


if __name__ == '__main__':
    main()
