#!/usr/bin/env python

# Unit tests for git-p4

import unittest
import StringIO
import time, tempfile, shutil, shlex, subprocess, os
from gitp4 import P4Sync, P4FileReader, extractSettingsFromNotes, P4Helper, die

class LargeFileWriterDouble:
    def __init__(self):
        self.debug = StringIO.StringIO()

    def write(self, bytes):
        self.debug.write(bytes)
        
    def getvalue(self):
        return self.debug.getvalue()

class P4HelperDouble(P4Helper):
    def __init__(self, changes = [], cmds = {}):
        self.changes = changes
        self.cmds = cmds
        
    def p4ChangesForPaths(self, depotPaths, changeRange):
        unused = depotPaths
        unused = changeRange
        return self.changes
    
    def p4Cmd(self, cmd):
        if cmd in self.cmds:
            return self.cmds[cmd]
        return P4Helper.p4Cmd(cmd)

    def p4CmdList(self, cmd, stdin=None, stdin_mode='w+b'):
        if cmd in self.cmds:
            return self.cmds[cmd]
        return P4Helper.p4CmdList(cmd, stdin, stdin_mode)
        
class P4FileReaderDouble(P4FileReader):
    def __init__(self, files, clientSpecDirs):
        P4FileReader.__init__(self,  [],  [])
        self.reader = None
        self.files = files
        self.index = 0
        self.record = [{ 'code': 'stat',
                        'rev': '20',
                        'time': '1290072101',
                        'action': 'edit',
                        'type': 'text',
                        'depotFile': '//depot/file.py',
                        'change': '33434'}, 
                        { 'code': 'text',
                        'data': 'some text' }]

    def __iter__(self):
        return self

    def next(self):
        # Return a dummy p4 record
        if self.index >= len(self.files):
            raise StopIteration
            
        f = self.files[self.index]
        f['data'] = 'some text'
        self.index += 1
        return f

class TestSubmit(unittest.TestCase):
    
    def test_WriteFastImport(self):
        tempdir = tempfile.mkdtemp()
        os.chdir(tempdir)

        try:
            details = {'status': 'submitted', 
                'code': 'stat', 
                'depotFile0': '//depot/file.py', 
                'action0': 'edit', 
                'fileSize0': '110958', 
                'options': '', 
                'client': 'someclient', 
                'user': 'someuser', 
                'time': '1289238991', 
                'rev0': '10', 
                'desc': 'Test\n', 
                'type0': 'text', 
                'change': '33255', 
                'digest0': 'BDA001AC8DE4B3B0484FE8252FEE73E8'}
            files = [{'action': 'edit', 'path': '//depot/file.py', 'rev': '10', 'type': 'text'}]
            branch = 'refs/remotes/p4/master'
            branchPrefixes = ['//depot/']
            parent = '3f641bec8f633e294a954d1a1d13b32e61232699'
            sync = P4Sync()
            sync.gitStream = LargeFileWriterDouble()
            sync.tz = "%+03d%02d" % (- time.timezone / 3600, ((- time.timezone % 3600) / 60))
            sync.users = {}
            sync.users['someuser'] = '<someuser@example.com>'
            sync.p4FileReader = P4FileReaderDouble
            sync.labels = {}
            
            sync.commit(details, files, branch, branchPrefixes, parent)
            actual = sync.gitStream.getvalue()
            self.assertEqual('''commit refs/remotes/p4/master
mark :1
committer <someuser@example.com> 1289238991 %s
data <<EOT
Test

EOT

from 3f641bec8f633e294a954d1a1d13b32e61232699
M 644 inline file.py
data 9
some text

commit refs/notes/git-p4
mark :2
committer <someuser@example.com> 1289238991 %s
data 28
Note added by git-p4 import
N inline :1
data <<EOT
[depot-paths = "//depot/": change = 33255]
EOT

''' % (sync.tz, sync.tz),  actual)
        finally:
            shutil.rmtree(tempdir,  True)

    def test_ExtractSettingsFromNote(self):
        tempdir = tempfile.mkdtemp()
        os.chdir(tempdir)

        try:
            # fast-import git repo
            subprocess.Popen(["git", "init", "--quiet"])
            importProcess = subprocess.Popen(["git", "fast-import", "--quiet"],
                                         stdin=subprocess.PIPE);
            importProcess.stdin.write('''commit refs/remotes/p4/master
mark :1
committer <someuser@example.com> 1289238991 +0100
data <<EOT
Test

EOT

M 644 inline file.py
data 9
some text

commit refs/notes/git-p4
mark :2
committer <someuser@example.com> 1289238991 +0100
data 21
Note added by git-p4
N inline :1
data <<EOT
[depot-paths = "//depot/": change = 33255]
EOT

''')
            importProcess.stdin.close()
            if importProcess.wait() != 0:
                die("fast-import failed")

            popen = subprocess.Popen(shlex.split("git rev-parse refs/remotes/p4/master"), stdout=subprocess.PIPE)
            val = popen.communicate()[0]
            if popen.returncode:
                die('Extracting revision failed')
            rev = val.strip()
            
            settings = extractSettingsFromNotes('refs/remotes/p4/master')
            self.assertEqual(['//depot/'], settings['depot-paths'])
            self.assertEqual(33255, int(settings['change']))
        finally:
            shutil.rmtree(tempdir,  True)

    def test_ExtractSettingsFromNoteWithExtraNote(self):
        tempdir = tempfile.mkdtemp()
        os.chdir(tempdir)

        try:
            # fast-import git repo
            subprocess.Popen(["git", "init", "--quiet"])
            importProcess = subprocess.Popen(["git", "fast-import", "--quiet"],
                                         stdin=subprocess.PIPE);
            importProcess.stdin.write('''commit refs/remotes/p4/master
mark :1
committer <someuser@example.com> 1289238991 +0100
data <<EOT
Test

EOT

M 644 inline file.py
data 9
some text

commit refs/notes/git-p4
mark :2
committer <someuser@example.com> 1289238991 +0100
data 21
Note added by git-p4
N inline :1
data <<EOT
[depot-paths = "//depot/": change = 33255]
EOT

commit refs/notes/commits
mark :3
committer <someuser@example.com> 1289238991 +0100
data 11
Manual note
N inline :1
data <<EOT
This is a manually added note
EOT
''')
            importProcess.stdin.close()
            if importProcess.wait() != 0:
                die("fast-import failed")

            popen = subprocess.Popen(shlex.split("git rev-parse refs/remotes/p4/master"), stdout=subprocess.PIPE)
            val = popen.communicate()[0]
            if popen.returncode:
                die('Extracting revision failed')
            rev = val.strip()
            
            settings = extractSettingsFromNotes('refs/remotes/p4/master')
            self.assertEqual(['//depot/'], settings['depot-paths'])
            self.assertEqual(33255, int(settings['change']))
        finally:
            shutil.rmtree(tempdir,  True)

    # Test extractSettingsFromNotes method when commit doesn't have a note yet
    def test_ExtractSettingsNoNote(self):
        tempdir = tempfile.mkdtemp()
        os.chdir(tempdir)

        try:
            # fast-import git repo
            subprocess.Popen(["git", "init", "--quiet"])
            importProcess = subprocess.Popen(["git", "fast-import", "--quiet"],
                                         stdin=subprocess.PIPE);
            importProcess.stdin.write('''commit refs/remotes/p4/master
mark :1
committer <someuser@example.com> 1289238991 +0100
data <<EOT
Test

EOT

M 644 inline file.py
data 9
some text
''')
            importProcess.stdin.close()
            if importProcess.wait() != 0:
                die("fast-import failed")

            #popen = subprocess.Popen(shlex.split("git rev-parse refs/remotes/p4/master"), stdout=subprocess.PIPE)
            #val = popen.communicate()[0]
            #if popen.returncode:
            #    die('Extracting revision failed')
            #rev = val.strip()
            
            settings = extractSettingsFromNotes('refs/remotes/p4/master')
            self.assertFalse(settings.has_key("depot-paths"))
        finally:
            shutil.rmtree(tempdir,  True)

class TestSync(unittest.TestCase):
    
    # tests syncing with a p4 change when the git repo (with older p4 changes) was already imported
    def test_SyncWithExistingRepo(self):
        tempdir = tempfile.mkdtemp()
        os.chdir(tempdir)

        try:
            # fast-import git repo
            subprocess.call(["git", "init", "--quiet"])
            importProcess = subprocess.Popen(["git", "fast-import", "--quiet"],
                                         stdin=subprocess.PIPE);
            importProcess.stdin.write('''commit refs/remotes/p4/master
mark :1
committer <someuser@example.com> 1289238991 +0100
data <<EOT
Test

EOT

M 644 inline file.py
data 9
some text

commit refs/notes/git-p4
mark :2
committer <someuser@example.com> 1289238991 +0100
data 21
Note added by git-p4
N inline :1
data <<EOT
[depot-paths = "//depot/": change = 33255]
EOT

''')
            importProcess.stdin.close()
            if importProcess.wait() != 0:
                die("fast-import failed")

            details = {'status': 'submitted', 
                'code': 'stat', 
                'depotFile0': '//depot/file2.py', 
                'action0': 'add', 
                'fileSize0': '110958', 
                'options': '', 
                'client': 'someclient', 
                'user': 'someuser', 
                'time': '1289238991', 
                'rev0': '10', 
                'desc': 'Test\n', 
                'type0': 'text', 
                'change': '33256', 
                'digest0': 'BDA001AC8DE4B3B0484FE8252FEE73E8'}
            users = [{'code': 'stat', 'Update': '1179412893', 'Access': '1179413508', 
                     'User': 'someuser', 'FullName': 'Firstname Lastname', 'Email': 'firstname.lastname@example.org'}]

            files = [{'action': 'add', 'path': '//depot/file2.py', 'rev': '1', 'type': 'text'}]
            branch = 'refs/remotes/p4/master'
            branchPrefixes = ['//depot/']
            sync = P4Sync()
            sync.p4 = P4HelperDouble(['33256'], {'describe 33256': details, 'users': users })
            sync.p4FileReader = P4FileReaderDouble
            
            # Execute method
            sync.run([])
            
            # verify results
            settings = extractSettingsFromNotes('refs/remotes/p4/master')
            self.assertEqual(['//depot/'], settings['depot-paths'])
            self.assertEqual(33256, int(settings['change']))

        finally:
            shutil.rmtree(tempdir,  True)

    def test_SyncWithBranchMerge(self):
        tempdir = tempfile.mkdtemp()
        os.chdir(tempdir)

        try:
            # fast-import git repo
            # we have:
            # A --- B   master
            #   \-- C   branch1
            # and we pretent to import merge master->branch1
            subprocess.call(["git", "init", "--quiet"])
            importProcess = subprocess.Popen(["git", "fast-import", "--quiet"],
                                         stdin=subprocess.PIPE);
            importProcess.stdin.write('''commit refs/remotes/p4/master
mark :1
committer <someuser@example.com> 1289238991 +0100
data <<EOT
Test

EOT

M 100644 inline file.txt
data <<EOT
Line 1

EOT

reset refs/notes/git-p4
commit refs/notes/git-p4
mark :2
committer <someuser@example.com> 1289238991 +0100
data 21
Note added by git-p4
N inline :1
data <<EOT
[depot-paths = "//depot/": change = 33255]
EOT

commit refs/remotes/p4/branch1
mark :3
committer <someuser@example.com> 1289238991 +0100
data <<EOT
New branch branch1

EOT

from :1

commit refs/notes/git-p4
mark :4
committer <someuser@example.com> 1289238991 +0100
data 21
Note added by git-p4
from :2
N inline :3
data <<EOT
[depot-paths = "//depot/": change = 33256]
EOT

commit refs/remotes/p4/master
mark :5
committer <someuser@example.com> 1289238991 +0100
data <<EOT
Second commit

EOT

from :1
M 100644 inline file.txt
data <<EOT
Line 1
Line 2

EOT

reset refs/notes/git-p4
commit refs/notes/git-p4
mark :6
committer <someuser@example.com> 1289238991 +0100
data 21
Note added by git-p4
from :4
N inline :5
data <<EOT
[depot-paths = "//depot/": change = 33257]
EOT

''')
            importProcess.stdin.close()
            if importProcess.wait() != 0:
                die("fast-import failed")

            details = {'status': 'submitted', 
                'code': 'stat', 
                'depotFile0': '//depot/file2.py', 
                'action0': 'add', 
                'fileSize0': '110958', 
                'options': '', 
                'client': 'someclient', 
                'user': 'someuser', 
                'time': '1289238991', 
                'rev0': '10', 
                'desc': 'Test\n', 
                'type0': 'text', 
                'change': '33256', 
                'digest0': 'BDA001AC8DE4B3B0484FE8252FEE73E8'}
            users = [{'code': 'stat', 'Update': '1179412893', 'Access': '1179413508', 
                     'User': 'someuser', 'FullName': 'Firstname Lastname', 'Email': 'firstname.lastname@example.org'}]

            files = [{'action': 'add', 'path': '//depot/file2.py', 'rev': '1', 'type': 'text'}]
            branch = 'refs/remotes/p4/master'
            branchPrefixes = ['//depot/']
            sync = P4Sync()
            sync.p4 = P4HelperDouble(['33256'], {'describe 33256': details, 'users': users })
            sync.p4FileReader = P4FileReaderDouble
            
            # Execute method
            sync.run([])
            
            # verify results
            settings = extractSettingsFromNotes('refs/remotes/p4/master')
            self.assertEqual(['//depot/'], settings['depot-paths'])
            self.assertEqual(33256, int(settings['change']))

        finally:
            shutil.rmtree(tempdir,  True)

if __name__ == '__main__':
    unittest.main()

