import sys
import json
from threading import Thread

from logbook import error
from logbook.queues import ZeroMQHandler

from onitu.utils import at_exit, get_available_drivers, get_logs_uri, u
from onitu.utils import log_traceback
from onitu.escalator.client import EscalatorClosed
from onitu.plug.exceptions import AbortOperation

session = u(sys.argv[1])
driver_name = sys.argv[2]
name = u(sys.argv[3])

drivers = get_available_drivers()

with ZeroMQHandler(get_logs_uri(session), multi=True).applicationbound():
    if driver_name not in drivers:
        error("Driver {} not found.", driver_name)
        exit(-1)

    try:
        entry_point = drivers[driver_name]
        driver = entry_point.load()
    except ImportError as e:
        error("Error importing driver {}: {}", driver_name, e)
        exit(-1)

    if 'start' not in driver.__all__:
        error(
            "Driver {} is not exporting a start function.", driver_name
        )
        exit(-1)

    if 'plug' not in driver.__all__:
        error(
            "Driver {} is not exporting a Plug instance.", driver_name
        )
        exit(-1)

    at_exit(driver.plug.close)

    try:
        # Using get_resource_stream doesn't seem to be working on Python 3 as
        # it returns bytes
        content = entry_point.dist.get_resource_string('', 'manifest.json')
        manifest = json.loads(u(content))
    except ValueError as e:
        error("Error parsing the manifest file of {} : {}", name, e)
        exit(-1)
    except (IOError, OSError) as e:
        error(
            "Cannot open the manifest file of '{}' : {}", name, e
        )
        exit(-1)

    def start():
        try:
            driver.start()
        except EscalatorClosed:
            return
        except Exception:
            log_traceback(driver.plug.logger)

    try:
        driver.plug.initialize(name, session, manifest)
        del manifest

        thread = Thread(target=start)
        thread.start()

        while thread.is_alive():
            thread.join(100)
    except AbortOperation:
        error("Closing service {} due to an error.", name)
    except Exception:
        log_traceback()

    driver.plug.logger.info("Exited")
