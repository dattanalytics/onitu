import requests
import time

from circus.client import CircusClient

from tests.utils.launcher import Launcher
from tests.utils.setup import Setup, Rule
from tests.utils.driver import LocalStorageDriver, TargetDriver
from tests.utils.loop import CounterLoop
from tests.utils.files import KB

from onitu.utils import get_fid

api_addr = "http://localhost:3862"
monitoring_path = "/api/v1.0/entries/{}/{}"
circus_client = CircusClient()
launcher, setup = None, None
rep1, rep2 = LocalStorageDriver("rep1"), TargetDriver("rep2")
json_file = "test_startup.json"


def get(*args, **kwargs):
    while True:
        try:
            return requests.get(*args, **kwargs)
        except requests.exceptions.ConnectionError:
            time.sleep(0.1)


def put(*args, **kwargs):
    while True:
        try:
            return requests.put(*args, **kwargs)
        except requests.exceptions.ConnectionError:
            time.sleep(0.1)


def is_running(name):
    query = {
        'command': "status",
        'properties': {
            'name': name
        }
    }
    status = circus_client.call(query)
    return status['status'] == "active"


def start(name):
    query = {
        'command': "start",
        'properties': {
            'name': name,
            'waiting': True
        }
    }
    circus_client.call(query)


def stop(name):
    query = {
        'command': "stop",
        'properties': {
            'name': name,
            'waiting': True
        }
    }
    circus_client.call(query)


def setup_module(module):
    global launcher, setup
    setup = Setup()
    setup.add(rep1)
    setup.add(rep2)
    setup.add_rule(Rule().match_path("/").sync(rep1.name, rep2.name))
    setup.save(json_file)
    loop = CounterLoop(4)
    launcher = Launcher(json_file)
    launcher.on_referee_started(loop.check)
    launcher.on_driver_started(loop.check, driver="rep1")
    launcher.on_driver_started(loop.check, driver="rep2")
    launcher.on_api_started(loop.check)
    launcher()
    try:
        loop.run(timeout=5)
    except:
        teardown_module(module)
        raise


def teardown_module(module):
    launcher.kill()
    setup.clean()


def test_list_files():
    list_files = "/api/v1.0/files"
    url = "{}{}".format(api_addr, list_files)

    files_number = 10
    files_types = ['txt', 'pdf', 'exe', 'jpeg', 'png',
                   'mp4', 'zip', 'tar', 'rar', 'html']
    files_names = ["test_list_files-{}.{}".format(i, files_types[i])
                   for i in range(files_number)]
    origin_files = {files_names[i]: i * KB
                    for i in range(files_number)}

    for i in range(files_number):
        file_name = files_names[i]
        file_size = origin_files[file_name]
        rep1.generate(file_name, file_size)
    r = get(url)
    json = r.json()
    files = json['files']
    assert len(files) == files_number
    for i in range(files_number):
        origin_file_size = origin_files[files[i]['filename']]
        assert files[i]['size'] == origin_file_size


def test_file():
        rep1.generate("test_file.txt", 10 * KB)
        fid = get_fid("test_file.txt")

        url = "{}/api/v1.0/files/{}/metadata".format(api_addr, fid)
        r = get(url)
        json = r.json()

        assert json['fid'] == fid
        assert json['filename'] == "test_file.txt"
        assert json['size'] == 10 * KB
        assert json['mimetype'] == "text/plain"


def test_stop():
    start(rep1.name)
    monitoring = monitoring_path.format(rep1.name, "stop")
    url = "{}{}".format(api_addr, monitoring)
    r = put(url)
    json = r.json()
    assert json['status'] == "ok"
    assert json['name'] == rep1.name
    assert 'time' in json
    assert is_running(rep1.name) is False
    start(rep1.name)


def test_start():
    stop(rep1.name)
    monitoring = monitoring_path.format(rep1.name, "start")
    url = "{}{}".format(api_addr, monitoring)
    r = put(url)
    json = r.json()
    assert json['status'] == "ok"
    assert json['name'] == rep1.name
    assert 'time' in json
    assert is_running(rep1.name) is True
    start(rep1.name)


def test_restart():
    start(rep1.name)
    monitoring = monitoring_path.format(rep1.name, "restart")
    url = "{}{}".format(api_addr, monitoring)
    r = put(url)
    json = r.json()
    assert json['status'] == "ok"
    assert json['name'] == rep1.name
    assert 'time' in json
    assert is_running(rep1.name) is True


def test_stop_already_stopped():
    stop(rep1.name)
    monitoring = monitoring_path.format(rep1.name, "stop")
    url = "{}{}".format(api_addr, monitoring)
    r = put(url)
    json = r.json()
    assert json['status'] == "error"
    assert json['reason'] == "entry {} is already stopped".format(
        rep1.name
    )
    assert is_running(rep1.name) is False
    start(rep1.name)


def test_start_already_started():
    start(rep1.name)
    monitoring = monitoring_path.format(rep1.name, "start")
    url = "{}{}".format(api_addr, monitoring)
    r = put(url)
    json = r.json()
    assert json['status'] == "error"
    assert json['reason'] == "entry {} is already running".format(
        rep1.name
    )
    assert is_running(rep1.name) is True


def test_restart_stopped():
    stop(rep1.name)
    monitoring = monitoring_path.format(rep1.name, "restart")
    url = "{}{}".format(api_addr, monitoring)
    r = put(url)
    json = r.json()
    assert json['status'] == "error"
    assert json['reason'] == "entry {} is stopped".format(
        rep1.name
    )
    assert is_running(rep1.name) is False
    start(rep1.name)


def test_stats_running():
    start(rep1.name)
    infos = ['age', 'cpu', 'create_time', 'ctime', 'mem', 'mem_info1',
             'mem_info2', 'started']
    monitoring = monitoring_path.format(rep1.name, "stats")
    url = "{}{}".format(api_addr, monitoring)
    r = get(url)
    json = r.json()
    assert json['status'] == "ok"
    assert json['name'] == rep1.name
    assert 'time' in json
    keys = json['info'].keys()
    for info in infos:
        assert info in keys


def test_stats_stopped():
    stop(rep1.name)
    monitoring = monitoring_path.format(rep1.name, "stats")
    url = "{}{}".format(api_addr, monitoring)
    r = get(url)
    json = r.json()
    assert json['status'] == "error"
    assert json['reason'] == "entry {} is stopped".format(
        rep1.name
    )
    start(rep1.name)


def test_status_started():
    start(rep1.name)
    monitoring = monitoring_path.format(rep1.name, "status")
    url = "{}{}".format(api_addr, monitoring)
    r = get(url)
    json = r.json()
    assert json['name'] == rep1.name
    assert json['status'] == "active"


def test_status_stopped():
    stop(rep1.name)
    monitoring = monitoring_path.format(rep1.name, "status")
    url = "{}{}".format(api_addr, monitoring)
    r = get(url)
    json = r.json()
    assert json['name'] == rep1.name
    assert json['status'] == "stopped"
    start(rep1.name)
