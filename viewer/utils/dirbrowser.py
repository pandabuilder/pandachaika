import os


class DirBrowser(object):

    def __init__(self, path: str) -> None:
        self.path = os.path.normpath(path)

    def realpath(self, path: str) -> str:
        p = os.path.normpath(os.path.join(self.path, path))
        if not p.startswith(self.path):
            p = self.path
        return p

    def relativepath(self, path: str) -> str:
        p = os.path.normpath(os.path.join(self.path, path))
        if not p.startswith(self.path):
            p = self.path
        return str(os.path.relpath(p, self.path))

    def isdir(self, path: str) -> bool:
        p = os.path.normpath(os.path.join(self.path, path))
        if not p.startswith(self.path):
            p = self.path
        return os.path.isdir(p)

    def files(self, path: str = '') -> list[tuple[str, bool]]:
        p = os.path.normpath(os.path.join(self.path, path))
        if not p.startswith(self.path):
            p = self.path
        dir_list = os.listdir(p)
        files = sorted([
            (f, os.path.isdir(os.path.join(p, f)))
            for f in dir_list
        ], key=lambda x: (not x[1], x[0]))
        if not self.path == p:
            files.insert(0, ('..', os.path.isdir(os.path.join(p, '..'))))
        return files

    def file(self, path: str) -> str:
        p = os.path.normpath(os.path.join(self.path, path))
        if not p.startswith(self.path):
            p = self.path
        return p
