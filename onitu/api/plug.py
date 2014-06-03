"""
The Plug is the part of any driver that communicates with the rest of
Onitu. This part is common between all the drivers.
"""

from logbook import Logger

from .metadata import Metadata
from .router import Router
from .dealer import Dealer

from onitu.escalator.client import Escalator


class Plug(object):
    """The Plug is the preferred way for a driver to communicate
    with other drivers, the :class:`.Referee`, or
    the database.

    Each driver must instantiate a new Plug, and define handlers
    (see :meth:`.handler`).

    :meth:`.initialize` should be called at the beginning of the
    `start` function, and
    When it is ready to receive requests from other drivers,
    it should call :meth:`.listen`. This function blocks until
    the driver is shut down.
    """

    def __init__(self):
        super(Plug, self).__init__()

        self.name = None
        self.logger = None
        self.router = None
        self.dealer = None
        self.options = {}
        self._handlers = {}

    def initialize(self, name, session, manifest):
        """Initialize the different components of the Plug.

        You should never have to call this function directly
        as it's called by the drivers' launcher.
        """
        self.name = name
        self.session = session
        self.escalator = Escalator(self.session)
        self.logger = Logger(self.name)

        self.options = self.escalator.get(
            'entry:{}:options'.format(name), default={}
        )

        self.validate_options(manifest)

        self.escalator.put('drivers:{}:manifest'.format(name), manifest)

        self.logger.info("Started")

        self.router = Router(self)
        self.dealer = Dealer(self)

    def listen(self, wait=True):
        """Start listening to requests from other drivers or the
        :class:`.Referee`.

        :param wait: Optional. If true, blocks until the Plug is
                     killed. Default to True.
        :type wait: bool

        This method starts two threads :

        - .. autoclass:: onitu.api.router.Router
        - .. autoclass:: onitu.api.dealer.Dealer
        """
        self.router.start()
        self.dealer.resume_transfers()
        self.dealer.start()

        if wait:
            self.dealer.join()

    def handler(self, task=None):
        """Decorator used register a handler for a particular task.

        :param task: Optional. The name of the handler. If not
                     specified, the name of the function will be used.
        :type task: string

        Example::

            @plug.handler()
            def get_chunk(filename, offset, size):
                with open(filename, 'rb') as f:
                    f.seek(offset)
                    return f.read(size)
        """
        def decorator(handler):
            self._handlers[task if task else handler.__name__] = handler
            return handler

        return decorator

    def update_file(self, metadata):
        """This method should be called by the driver after each update
        of a file or after the creation of a file.
        It takes a :class:`.Metadata` object in parameter that should have been
        updated with the new value of the properties.
        """
        fid = metadata.fid

        # If the file is being uploaded, we stop it
        self.dealer.stop_transfer(fid)
        # We make sure that the key has been deleted
        # (if this event occurs before the transfer was restarted)
        self.escalator.delete('entry:{}:transfer:{}'.format(self.name, fid))

        if self.name not in metadata.owners:
            metadata.owners += (self.name,)
        metadata.uptodate = (self.name,)
        metadata.write()

        self.logger.debug(
            "Notifying the Referee about '{}'", metadata.filename
        )
        self.escalator.put('referee:event:{}'.format(fid), self.name)

    def get_metadata(self, filename):
        """
        :param filename: The name of the file, with the absolute path
                         from the driver's root
        :type string:

        :rtype: :class:`.Metadata`

        If the file does not exists in Onitu, it will be created when
        :meth:`.Metadata.write` will be called.
        """
        metadata = Metadata.get_by_filename(self, filename)

        if not metadata:
            metadata = Metadata(plug=self, filename=filename)

        metadata.entry = self.name
        return metadata

    def validate_options(self, manifest):
        """
        Validate the options and set the default values using informations
        from the manifest.

        This method is called by :meth:`.initialize`.
        """
        options = manifest.get('options', {})

        # add the options common to all drivers
        options.update({
            'chunk_size': {
                'type': 'integer',
                'default': 1 << 20  # 1 MB
            }
        })

        types = {
            'string': lambda v: (isinstance(v, type(v))
                                 or isinstance(v, str)),
            'integer': lambda v: isinstance(v, int),
            'float': lambda v: isinstance(v, float),
            'boolean': lambda v: isinstance(v, bool),
            'enumerate': lambda v: v in options[name].get('values', []),
        }

        for name, value in self.options.items():
            if name not in options:
                raise RuntimeError("Unknown option '{}'".format(name))
                return False

            excepted_type = options[name].get('type', None).lower()

            if excepted_type not in types:
                # the manifest is wrong, we print a warning but we don't
                # abort.
                # However, maybe we should validate the manifest first
                self.logger.warning(
                    "Unknown type '{}' in manifest", excepted_type
                )
            elif not types[excepted_type](value):
                raise RuntimeError(
                    "Option '{}' should be of type '{}', got '{}'.".format(
                        name, excepted_type, type(value).__name__
                    )
                )
                return False

        for name, props in options.items():
            if name not in self.options:
                if 'default' in props:
                    self.options[name] = props['default']
                else:
                    raise RuntimeError(
                        "Mandatory option '{}' not present in the "
                        "configuration.", name
                    )

        return True
