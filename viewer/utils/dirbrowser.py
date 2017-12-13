import os


class DirBrowser(object):

    def realpath(self, path):
        p = os.path.normpath(os.path.join(self.path, path))
        if not p.startswith(self.path):
            p = self.path
        return p

    def relativepath(self, path):
        p = os.path.normpath(os.path.join(self.path, path))
        if not p.startswith(self.path):
            p = self.path
        return os.path.relpath(p, self.path)

    def isdir(self, path):
        p = os.path.normpath(os.path.join(self.path, path))
        if not p.startswith(self.path):
            p = self.path
        return os.path.isdir(p)

    def files(self, path=''):
        p = os.path.normpath(os.path.join(self.path, path))
        if not p.startswith(self.path):
            p = self.path
        dir_list = os.listdir(p)
        files = sorted([
            (f, os.path.isdir(os.path.join(p, f)))
            for f in dir_list
        ])
        if not self.path == p:
            files.insert(0, ('..', os.path.isdir(os.path.join(p, '..'))))
        return files

    def file(self, path):
        p = os.path.normpath(os.path.join(self.path, path))
        if not p.startswith(self.path):
            p = self.path
        return p

    def __init__(self, path):
        self.path = os.path.normpath(path)
