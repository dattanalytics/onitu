import sys

from logbook.queues import ZeroMQHandler

from .local_storage import start

if __name__ == '__main__':
    with ZeroMQHandler(sys.argv[2], multi=True).applicationbound():
        start(sys.argv[1])
