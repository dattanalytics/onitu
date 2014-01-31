import os.path
from os import unlink

from utils.launcher import Launcher
from utils.entries import Entries
from utils.loop import BooleanLoop, CounterLoop, TimeoutError
from utils.files import generate, checksum
from utils.tempdirs import TempDirs

launcher = None
dirs = TempDirs()
rep1, rep2 = dirs.create(), dirs.create()
json_file = 'test_copy.json'


def setup_module(module):
    global launcher
    entries = Entries()
    entries.add('local_storage', 'rep1', {'root': rep1})
    entries.add('local_storage', 'rep2', {'root': rep2})
    entries.save(json_file)
    loop = CounterLoop(3)
    launcher = Launcher(json_file)
    launcher.on_referee_started(loop.check)
    launcher.on_driver_started(loop.check, driver='rep1')
    launcher.on_driver_started(loop.check, driver='rep2')
    launcher()
    try:
        loop.run(timeout=5)
    except:
        teardown_module(module)
        raise


def teardown_module(module):
    launcher.kill()
    unlink(json_file)
    dirs.delete()


def copy_file(filename, size):
    loop = BooleanLoop()
    launcher.on_transfer_ended(
        loop.stop, d_from='rep1', d_to='rep2', filename=filename
    )
    generate(os.path.join(rep1, filename), size)
    loop.run(timeout=5)
    assert(checksum(os.path.join(rep1, filename)) ==
           checksum(os.path.join(rep2, filename)))


def test_simple_copy():
    copy_file('simple', 100)


def test_other_copy():
    copy_file('other', 100)


def test_same_copy():
    copy_file('same', 100)
    copy_file('same', 100)


def test_smaller_copy():
    copy_file('smaller', 100)
    copy_file('smaller', 10)


def test_bigger_copy():
    copy_file('bigger', 100)
    copy_file('bigger', 1000)


def test_big_copy():
    copy_file('big', '10M')


def test_multipass_copy():  # dd called with a count parameter
    count = 10
    filename = 'multipass'

    loop = BooleanLoop()

    launcher.on_transfer_ended(
        loop.stop, d_from='rep1', d_to='rep2', filename=filename
    )

    generate(os.path.join(rep1, filename), '1M', count)
    size = os.path.getsize(os.path.join(rep1, filename))

    for _ in range(count):
        try:
            loop.run(timeout=2)
        except TimeoutError:
            continue

        loop.restart()

        if os.path.getsize(os.path.join(rep2, filename)) == size:
            break

    assert (checksum(os.path.join(rep1, filename)) ==
            checksum(os.path.join(rep2, filename)))