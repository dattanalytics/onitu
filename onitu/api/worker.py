from threading import Thread

import zmq
import redis
from logbook import Logger

from .metadata import Metadata


class Worker(Thread):
    """Thread waiting for a notification from the Referee and handling
    it.
    """

    def __init__(self, plug):
        super(Worker, self).__init__()

        self.plug = plug
        self.logger = Logger("{} - Worker".format(self.plug.name))
        self.context = zmq.Context.instance()
        self.sub = None

    def run(self):
        port = self.plug.redis.get('referee:publisher')
        publisher = 'tcp://localhost:{}'.format(port)
        self.sub = self.context.socket(zmq.SUB)
        self.sub.connect(publisher)
        self.sub.setsockopt(zmq.SUBSCRIBE, self.plug.name)

        while True:
            self.logger.info("Listening for orders from the Referee...")
            _, driver, fid = self.sub.recv_multipart()

            self.async_get_file(fid, driver=driver)

    def resume_transfers(self):
        for fid in self.plug.redis.smembers('drivers:{}:transfers'
                                            .format(self.plug.name)):
            self.async_get_file(fid, driver=None, restart=True)

    def async_get_file(self, *args, **kwargs):
        # should probably be in a thread pool, but YOLO
        thread = Thread(target=self.get_file, args=args, kwargs=kwargs)
        thread.start()

    def get_file(self, fid, driver=None, restart=False):
        """Transfers a file from a Driver to another.
        """
        transfer_key = 'drivers:{}:transfers:{}'.format(self.plug.name, fid)

        if driver:
            self.plug.redis.sadd(
                'drivers:{}:transfers'.format(self.plug.name),
                fid
            )
            self.plug.redis.hmset(transfer_key, {'from': driver, 'offset': 0})
            offset = 0
            self.logger.info("Starting to get file {} from {}"
                             .format(fid, driver))
        else:
            transfer = self.plug.redis.hgetall(transfer_key)
            driver = transfer['from']
            offset = int(transfer['offset'])
            self.logger.info("Restarting transfer for file {} from {}"
                             .format(fid, driver))

        metadata = Metadata.get_by_id(self.plug, fid)

        dealer = self.context.socket(zmq.DEALER)
        port = self.plug.redis.get('drivers:{}:router'.format(driver))
        dealer.connect('tcp://localhost:{}'.format(port))

        filename = metadata.filename
        end = metadata.size
        chunk_size = self.plug.options.get('chunk_size', 1 * 1024 * 1024)

        if not restart:
            self._call('start_upload', metadata)

        while offset < end:
            dealer.send_multipart((filename, str(offset), str(chunk_size)))
            chunk = dealer.recv()

            self.logger.debug("Received chunk of size {} from {} for file {}"
                              .format(len(chunk), driver, fid))

            with self.plug.redis.pipeline() as pipe:
                try:
                    assert len(chunk) > 0

                    pipe.watch(transfer_key)

                    assert pipe.hget(transfer_key, 'offset') == str(offset)

                    self._call('upload_chunk', filename, offset, chunk)

                    pipe.multi()
                    pipe.hincrby(transfer_key, 'offset', len(chunk))
                    offset = int(pipe.execute()[-1])

                except (redis.WatchError, AssertionError):
                    # another transaction for the same file has
                    # probably started
                    self.logger.info("Aborting transfer for file {} from {}"
                                     .format(fid, driver))
                    return

        self._call('end_upload', metadata)

        self.plug.redis.delete(transfer_key)
        self.plug.redis.srem(
            'drivers:{}:transfers'.format(self.plug.name),
            fid
        )
        self.logger.info(
            "Transfer for file {} from {} successful",
            fid, driver
        )

    def _call(self, handler_name, *args, **kwargs):
        """Calls a handler defined by the Driver if it exists.
        """
        handler = self.plug._handlers.get(handler_name)

        if handler:
            return handler(*args, **kwargs)
