import os
import hashlib

from io import BytesIO

from tests.utils import driver
from onitu_webdav.wd import get_WEBDAV_client, create_dirs
from onitu.utils import get_random_string


class Driver(driver.Driver):
    def __init__(self, *args, **options):
        options['hostname'] = os.getenv(
            "ONITU_WEBDAV_HOSTNAME", "http://localhost"
        )
        options['username'] = os.getenv("ONITU_WEBDAV_USERNAME", "")
        options['password'] = os.getenv("ONITU_WEBDAV_PASSWORD", "")
        options['changes_timer'] = os.getenv("ONITU_WEBDAV_CHANGES_TIMER", 10)

        self._root = u"/" + get_random_string(10)

        hostname = options['hostname']
        username = options['username']
        password = options['password']

        super(Driver, self).__init__('webdav', *args, **options)

        self.webd = get_WEBDAV_client(hostname, username, password)

        create_dirs(self.webd, self._root)

    @property
    def root(self):
        return self._root

    def close(self):
        self.rmdir(self.root)

    def mkdir(self, subdirs):
        create_dirs(self.webd, subdirs)

    def rmdir(self, path):
        self.webd.clean(path)

    def write(self, filename, content):
        create_dirs(self.webd, os.path.dirname(filename))
        buff = BytesIO(content)
        self.webd.upload_from(buff, filename)

    def generate(self, filename, size):
        self.write(filename, os.urandom(size))

    def exists(self, filename):
        try:
            self.webd.info(filename)
        except:
            return False
        return True

    def unlink(self, filename):
        self.clean(filename)

    def rename(self, source, target):
        self.webd.move(remote_path_from=source, remote_path_to=target)

    def checksum(self, filename):
        buff = BytesIO()
        self.webd.download_to(buff, filename)
        data = buff.getvalue()
        md5 = hashlib.md5(data).hexdigest()
        return md5
