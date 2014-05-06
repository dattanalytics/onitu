from os import unlink

from tests.utils.launcher import Launcher
from tests.utils.setup import Setup, Rule
from tests.utils.driver import LocalStorageDriver, TargetDriver
from tests.utils.loop import BooleanLoop, CounterLoop
from tests.utils.files import KB

launcher = None
rep1, rep2 = LocalStorageDriver('rep1', chunk_size=1), TargetDriver('rep2')
json_file = 'test_multipass_copy.json'


def setup_module(module):
    global launcher
    setup = Setup()
    setup.add(rep1)
    setup.add(rep2)
    setup.add_rule(Rule().match_path('/').sync(rep1.name, rep2.name))
    setup.save(json_file)
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
    rep1.close()
    rep2.close()


def test_multipass_copy():
    count = 10
    filename = 'multipass'

    startloop = BooleanLoop()
    loop = BooleanLoop()

    launcher.on_transfer_started(
        startloop.stop, d_from='rep1', d_to='rep2', filename=filename,
        unique=False
    )
    launcher.on_transfer_ended(
        loop.stop, d_from='rep1', d_to='rep2', filename=filename, unique=False
    )
    launcher.on_transfer_aborted(
        loop.stop, d_from='rep1', d_to='rep2', filename=filename, unique=False
    )

    rep1.generate(filename, 100 * KB)
    startloop.run(timeout=2)

    for _ in range(count):
        startloop.restart()
        loop.restart()
        rep1.generate(filename, 100 * KB)
        loop.run(timeout=5)
        startloop.run(timeout=2)
    loop.restart()
    loop.run(timeout=5)

    assert rep1.checksum(filename) == rep2.checksum(filename)