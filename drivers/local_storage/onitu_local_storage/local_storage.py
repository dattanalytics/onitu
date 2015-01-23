import os

from onitu.plug import Plug, DriverError, ServiceError
from onitu.escalator.client import EscalatorClosed
from onitu.utils import IS_WINDOWS, u, log_traceback

if IS_WINDOWS:
    import threading
    import path
    import win32api
    import win32file
    import win32con
    from pywintypes import OVERLAPPED
    from time import time, sleep
    from win32event import (
        CreateEvent,
        WaitForMultipleObjects,
        WAIT_TIMEOUT,
        WAIT_ABANDONED_0,
        WAIT_FAILED,
        WAIT_OBJECT_0)
else:
    import pyinotify

TMP_EXT = '.onitu-tmp'

plug = Plug()

if IS_WINDOWS:
    ignoreNotif = dict()
    FILE_LIST_DIRECTORY = 0x0001

ignore_delete = set()
ignore_move = set()



def to_tmp(filename):
    if IS_WINDOWS:
        return os.path.join(
            os.path.dirname(filename), os.path.basename(filename) + TMP_EXT
        )
    else:
        return os.path.join(
            os.path.dirname(filename), '.' + os.path.basename(filename) +
            TMP_EXT
        )


def walkfiles(root):
    return (
        os.path.join(dirpath, f)
        for dirpath, _, files in os.walk(root) for f in files
    )


def update(metadata, mtime=None):
    try:
        metadata.size = os.path.getsize(metadata.path)

        if not mtime:
            mtime = os.path.getmtime(metadata.path)
        metadata.extra['revision'] = mtime
    except (IOError, OSError) as e:
        raise ServiceError(
            u"Error updating file '{}': {}".format(metadata.path, e)
        )
    else:
        plug.update_file(metadata)


def delete(metadata):
    if metadata.path in ignore_delete:
        ignore_delete.discard(metadata.path)
        return

    plug.delete_file(metadata)


def move(old_metadata, new_filename):
    if old_metadata.path in ignore_move:
        ignore_move.discard(old_metadata.path)
        return

    new_metadata = plug.move_file(old_metadata, new_filename)
    new_metadata.extra['revision'] = os.path.getmtime(new_filename)
    # We update the size in case the file was moved very quickly after a change
    # so the old metadata are not up-to-date
    new_metadata.size = os.path.getsize(new_metadata.path)
    new_metadata.write()


def check_changes(folder):
    expected_files = set()

    expected_files.update(plug.list(folder).keys())
    for filePath in walkfiles(folder.path):
        if os.path.splitext(filePath)[1] == TMP_EXT:
            continue

        filePath = filePath.replace("\\", "/")

        filename = folder.relpath(filePath)

        expected_files.discard(filename)
        metadata = plug.get_metadata(filename, folder)
        revision = metadata.extra.get('revision', 0.)

        try:
            mtime = os.path.getmtime(filePath)
        except (IOError, OSError) as e:
            raise ServiceError(
                u"Error updating file '{}': {}".format(metadata.path, e)
            )

        if mtime > revision:
            update(metadata, mtime)
    for filename in expected_files:
        metadata = plug.get_metadata(filename, folder)
        # If we don't see this file and we're not uptodate, this could
        # mean that we simply never transfered it, so we shouldn't trigger
        # a deletion (cf https://github.com/onitu/onitu/issues/130)
        if plug.name in metadata.uptodate:
            plug.delete_file(metadata)


@plug.handler()
def normalize_path(p):
    normalized = os.path.normpath(os.path.expanduser(p))
    if IS_WINDOWS:
        normalized = normalized.replace("\\", "/")
    if not os.path.isabs(normalized):
        raise DriverError(u"The folder path '{}' is not absolute.".format(p))

    return normalized


@plug.handler()
def get_chunk(metadata, offset, size):

    try:
        with open(metadata.path, 'rb') as f:
            f.seek(offset)
            return f.read(size)
    except (IOError, OSError) as e:
        raise ServiceError(
            u"Error getting file '{}': {}".format(metadata.path, e)
        )


@plug.handler()
def start_upload(metadata):
    tmp_file = to_tmp(metadata.path)
    plug.logger.info("start_upload {} {}".format(tmp_file, metadata.path))
    if IS_WINDOWS:
        ignoreNotif[metadata.path] = False
        sleep(1)
    try:
        try:
            os.makedirs(os.path.dirname(tmp_file))
        except OSError:
            pass
        open(tmp_file, 'wb').close()
        if IS_WINDOWS:
            win32api.SetFileAttributes(
                tmp_file, win32con.FILE_ATTRIBUTE_HIDDEN)
    except IOError as e:
        raise ServiceError(
            u"Error creating file '{}': {}".format(tmp_file, e)
        )


@plug.handler()
def upload_chunk(metadata, offset, chunk):
    tmp_file = to_tmp(metadata.path)
    try:
        with open(tmp_file, 'r+b') as f:
            f.seek(offset)
            f.write(chunk)
    except (IOError, OSError) as e:
        raise ServiceError(
            u"Error writing file '{}': {}".format(tmp_file, e)
        )


@plug.handler()
def end_upload(metadata):
    tmp_file = to_tmp(metadata.path)

    try:
        if IS_WINDOWS:
            # On Windows we can't move a file
            # if dst exists
            try:
                os.unlink(metadata.path)
            except OSError:
                pass
        os.rename(tmp_file, metadata.path)
        mtime = os.path.getmtime(metadata.path)

        if IS_WINDOWS:
            win32api.SetFileAttributes(
                metadata.path, win32con.FILE_ATTRIBUTE_NORMAL)
    except (IOError, OSError) as e:
        raise ServiceError(
            u"Error for file '{}': {}".format(metadata.path, e)
        )

    metadata.extra['revision'] = mtime
    metadata.write()

    if IS_WINDOWS:
        if metadata.path in ignoreNotif and \
           not ignoreNotif[metadata.path]:
            ignoreNotif[metadata.path] = time() + 1

@plug.handler()
def abort_upload(metadata):
    tmp_file = to_tmp(metadata.path)

    if IS_WINDOWS:
        if metadata.filename in ignoreNotif:
            del ignoreNotif[metadata.path]
    try:
        os.unlink(tmp_file)
    except (IOError, OSError) as e:
        raise ServiceError(
            u"Error deleting file '{}': {}".format(tmp_file, e)
        )


@plug.handler()
def delete_file(metadata):
    try:
        ignore_delete.add(metadata.path)
        os.unlink(metadata.path)
    except (IOError, OSError) as e:
        ignore_delete.discard(metadata.path)
        raise ServiceError(
            u"Error deleting file '{}': {}".format(metadata.path, e)
        )


@plug.handler()
def move_file(old_metadata, new_metadata):
    if IS_WINDOWS:
        ignoreNotif[new_metadata.path] = False
        ignoreNotif[old_metadata.path] = False
    try:
        ignore_move.add(old_metadata.path)
        os.renames(old_metadata.path, new_metadata.path)
    except (IOError, OSError) as e:
        if IS_WINDOWS:
            del ignoreNotif[new_metadata.path]
            del ignoreNotif[old_metadata.path]
        ignore_move.discard(old_metadata.path)
        raise ServiceError(
            u"Error moving file '{}': {}".format(old_metadata.path, e)
        )
    if IS_WINDOWS:
        Rtime = time()
        ignoreNotif[new_metadata.path] = Rtime
        ignoreNotif[old_metadata.path] = Rtime
		

if IS_WINDOWS:
    def verifDictModifFile(writingDict, Rtime, cleanOld=False):
        for i, j in list(writingDict.items()):
            if cleanOld is False:
                if Rtime - j[0] >= 1.0:
                        try:
                            fd = os.open(i, os.O_RDONLY)
                        except(IOError, OSError):
                            continue
                        else:
                            os.close(fd)
                            metadata = plug.get_metadata(str(u(j[2]))
                                                         .replace("\\", "/"),
                                                         j[1])
                            update(metadata)
                            del writingDict[i]
            else:
                if j > 1 and Rtime - j >= 1:
                        writingDict.pop(i)
        return writingDict

    def moveFrom(metadata):
        fileAction.oldMetadata = metadata
        if metadata.path.endswith(TMP_EXT):
            ignoreNotif[metadata.path[:len(TMP_EXT)]] = \
                False
        elif fileAction.oldMetadata is not None:
            ignoreNotif[metadata.path] = False
            fileAction.moving = True

    def fileAction(dir, filename, action, ignoreNotif, writingDict,
                   transferSet, actions_names, Rtime):
        metadata = plug.get_metadata(u(filename).replace("\\", "/"), dir)
        if metadata is None:
            return
        if (actions_names.get(action) == 'write' or
            actions_names.get(action) == 'create') and \
           metadata.path not in ignoreNotif:
            if os.access(metadata.path, os.R_OK):
                writingDict[metadata.path] = (Rtime, dir, metadata.filename)
        elif actions_names.get(action) == 'delete' and \
                metadata.path not in ignoreNotif:
            delete(metadata)
        elif actions_names.get(action) == 'moveFrom' and \
                metadata.path not in ignoreNotif:
            moveFrom(metadata)
        elif actions_names.get(action) == 'moveTo' and fileAction.moving:
            ignoreNotif[metadata.path] = False
            move(fileAction.oldMetadata, metadata.path)
            fileAction.moving = False
            if fileAction.oldMetadata.filename.endswith(TMP_EXT):
                del ignoreNotif[metadata.path]
            else:
                del ignoreNotif[fileAction.oldMetadata.path]
                ignoreNotif[metadata.path] = Rtime
            fileAction.oldMetadata = None
        if (actions_names.get(action) == 'create' or
            actions_names.get(action) == 'write') and \
                metadata.path not in transferSet and \
                metadata.path in ignoreNotif:
            if ignoreNotif[metadata.path] is not False:
                transferSet.add(metadata.path)

    class folderToWatch(object):
        def __init__(self, folder, overlapped, buffer):
            self.folder = folder
            self.handler = win32file.CreateFile(
                folder.path,
                FILE_LIST_DIRECTORY,
                win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                None,
                win32con.OPEN_EXISTING,
                win32con.FILE_FLAG_BACKUP_SEMANTICS |
                win32con.FILE_FLAG_OVERLAPPED,
                None
            )
            self.overlapped = overlapped
            self.buffer = buffer
            self.overlapped.hEvent = CreateEvent(None, 0, 0, None)

    def win32watcherThread(root, file_lock):
        dirList = []
        for folder in plug.folders_to_watch:
            dirList.append(folderToWatch(folder, OVERLAPPED(),
                           win32file.AllocateReadBuffer(10000)))
        actions_names = {
            1: 'create',
            2: 'delete',
            3: 'write',
            4: 'moveFrom',
            5: 'moveTo'
        }
        global ignoreNotif
        fileAction.moving = False
        old_path = ""
        writingDict = dict()

        while True:
            for folder in dirList:
                win32file.ReadDirectoryChangesW(
                    folder.handler,
                    folder.buffer,
                    True,
                    win32con.FILE_NOTIFY_CHANGE_FILE_NAME |
                    win32con.FILE_NOTIFY_CHANGE_DIR_NAME |
                    win32con.FILE_NOTIFY_CHANGE_ATTRIBUTES |
                    win32con.FILE_NOTIFY_CHANGE_SIZE |
                    win32con.FILE_NOTIFY_CHANGE_LAST_WRITE |
                    win32con.FILE_NOTIFY_CHANGE_SECURITY,
                    folder.overlapped
                )
            rc = WAIT_TIMEOUT
            while rc == WAIT_TIMEOUT or rc == WAIT_FAILED or \
                    rc == WAIT_ABANDONED_0:
                rc = WaitForMultipleObjects([folder.overlapped.hEvent
                                             for folder in dirList],
                                            0, 200)
                if rc == WAIT_TIMEOUT:
                    writingDict = verifDictModifFile(writingDict, time())
                    ignoreNotif = verifDictModifFile(ignoreNotif, time(), True)
            dir = dirList[rc - WAIT_OBJECT_0]
            data = win32file.GetOverlappedResult(dir.handler, dir.overlapped,
                                                 True)
            events = win32file.FILE_NOTIFY_INFORMATION(dir.buffer, data)
            Rtime = time()
            transferSet = set()
            for action, file_ in events:
                abs_path = path.path(dir.folder.path + "\\" + file_)
                if actions_names[action] == 'moveFrom':
                    old_path = file_
                if actions_names[action] != 'write'\
                        and actions_names[action] != 'create'\
                        and abs_path.isdir() and os.access(abs_path, os.R_OK)\
                        and len(os.listdir(abs_path)) != 0:
                    for file in abs_path.walkfiles():
                        try:
                            with file_lock:
                                file = dir.folder.relpath(file)
                                old_file = file
                                for i in range(0, old_path.count("\\") + 1):
                                    ret = old_file.partition("\\")
                                    if ret[1] == "":
                                        old_file = ret[0]
                                        break
                                    old_file = ret[2]
                                if actions_names[action] == 'moveTo':
                                    fileParam = u(old_path + "/" + old_file)\
                                                .replace("\\", "/")
                                    metadata = plug.get_metadata(fileParam,
                                                                 dir.folder)
                                    if metadata is None:
                                        continue
                                    moveFrom(metadata)
                                fileAction(dir.folder, file, action,
                                           ignoreNotif, writingDict,
                                           transferSet, actions_names, Rtime)
                        except EscalatorClosed:
                            return

                if (abs_path.isdir() or abs_path.ext == TMP_EXT or
                    (os.path.exists(abs_path) and
                    (not (win32api.GetFileAttributes(abs_path)
                          & win32con.FILE_ATTRIBUTE_NORMAL) and
                     not (win32api.GetFileAttributes(abs_path)
                          & win32con.FILE_ATTRIBUTE_ARCHIVE)))) and \
                        not actions_names[action] == 'delete':
                    continue
                try:
                    with file_lock:
                        fileAction(dir.folder, file_, action, ignoreNotif,
                                   writingDict, transferSet, actions_names,
                                   Rtime)
                except EscalatorClosed:
                    return
            try:
                writingDict = verifDictModifFile(writingDict, Rtime)
                ignoreNotif = verifDictModifFile(ignoreNotif, Rtime, True)
            except EscalatorClosed:
                return

    def watch_changes(folder):
        file_lock = threading.Lock()
        notifier = threading.Thread(target=win32watcherThread,
                                    args=(folder.path, file_lock))
        notifier.setDaemon(True)
        notifier.start()
else:
    class Watcher(pyinotify.ProcessEvent):
        def __init__(self, folder, *args, **kwargs):
            pyinotify.ProcessEvent.__init__(self, *args, **kwargs)

            self.folder = folder

        def process_IN_CLOSE_WRITE(self, event):
            self.process_event(event.pathname, update)

        def process_IN_DELETE(self, event):
            self.process_event(event.pathname, delete)

        def process_IN_MOVED_TO(self, event):
            if event.dir:
                for new in walkfiles(event.pathname):
                    if hasattr(event, 'src_pathname'):
                        old = new.replace(event.pathname, event.src_pathname)
                        self.process_event(old, move, u(new))
                    else:
                        self.process_event(new, update)
            else:
                if hasattr(event, 'src_pathname'):
                    self.process_event(
                        event.src_pathname, move, u(event.pathname)
                    )
                else:
                    self.process_event(event.pathname, update)

        def process_event(self, filename, callback, *args):
            filename = os.path.relpath(u(filename), self.folder.path)

            if os.path.splitext(filename)[1] == TMP_EXT:
                return

            try:
                metadata = plug.get_metadata(filename, self.folder)
                callback(metadata, *args)
            except EscalatorClosed:
                pass
            except OSError as e:
                plug.logger.warning("Error when dealing with FS event: {}", e)
            except (DriverError, ServiceError) as e:
                plug.logger.warning(str(e))
            except Exception:
                log_traceback(plug.logger)

    def watch_changes(folder):
        manager = pyinotify.WatchManager()
        notifier = pyinotify.ThreadedNotifier(manager, Watcher(folder))
        notifier.daemon = True
        notifier.start()

        mask = (pyinotify.IN_CREATE | pyinotify.IN_CLOSE_WRITE |
                pyinotify.IN_DELETE | pyinotify.IN_MOVED_TO |
                pyinotify.IN_MOVED_FROM)
        manager.add_watch(folder.path, mask, rec=True, auto_add=True)


def start():
    for folder in plug.folders_to_watch:
        try:
            os.makedirs(folder.path)
        except OSError:
            # can be raised if the folder already exists
            pass

        if not os.path.exists(folder.path):
            raise DriverError(
                u"Could not create the folder '{}'.".format(folder.path)
            )

        watch_changes(folder)
        check_changes(folder)

    plug.listen()
