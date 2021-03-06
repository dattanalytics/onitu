import time
import socket
import threading
import sys
from ssl import SSLError

import requests
import tinys3

from onitu.plug import Plug, DriverError, ServiceError
from onitu.escalator.client import EscalatorClosed
from onitu.utils import u  # Unicode helpers

# Python 2/3 compatibility
if sys.version_info.major == 2:
    from StringIO import StringIO as IOStream
else:
    # In Py3k, chunks are passed as raw bytes. Hence we can't use StringIO
    from io import BytesIO as IOStream

plug = Plug()

# Amazon S3 related global variables
S3Conn = None
# To deal with timestamp changes
TIMESTAMP_FMT = '%Y-%m-%dT%H:%M:%S.000Z'
# When using HEAD requests on objects, the timestamp format changes
# it is like e.g. 'Sat, 03 May 2014 04:36:11 GMT'
HEAD_TIMESTAMP_FMT = '%a, %d %b %Y %H:%M:%S GMT'
# "Each part must be at least 5 MB in size, except the last part."
# http://docs.aws.amazon.com/AmazonS3/latest/API/mpUploadUploadPart.html
S3_MINIMUM_CHUNK_SIZE = 5 << 20
# The number of multipart upload objects we keep in cache
MAX_CACHE_SIZE = 100
# Key: Multipart Upload ID <-> Value: MultipartUpload object
cache = {}


def get_conn():
    """Gets the connection with the Amazon S3 server.
    Raises an error if conn cannot be established"""
    global S3Conn

    S3Conn = tinys3.Connection(plug.options['aws_access_key'],
                               plug.options['aws_secret_key'],
                               default_bucket=plug.options['bucket'], tls=True)
    # Check that the given bucket exists by doing a HEAD request
    try:
        S3Conn.head_bucket()
    except requests.HTTPError as httpe:
        err = u"Cannot reach Onitu bucket {}".format(plug.options['bucket'])
        if httpe.response.status_code == 404:
            err += u": The bucket doesn't exist."
        if httpe.response.status_code == 403:
            err += u": Invalid credentials."
        err += u" Please check your Amazon S3 configuration - {}".format(httpe)
        raise DriverError(err)
    plug.logger.debug("Connection with Amazon S3 account successful")
    return S3Conn


def get_file_timestamp(filename):
    """Returns the float timestamp based on the
    date format timestamp stored by Amazon.
    Prefixes the given filename with Onitu's root."""
    plug.logger.debug(u"Getting timestamp of {}", filename)
    metadata = S3Conn.head_object(u(filename))
    timestamp = metadata.headers['last-modified']
    # convert timestamp to timestruct...
    timestamp = time.strptime(timestamp, HEAD_TIMESTAMP_FMT)
    # ...timestruct to float
    timestamp = time.mktime(timestamp)
    return timestamp


def add_to_cache(multipart_upload):
    """Caches a multipart upload. Checks that the cache isn't growing
    past MAX_CACHE_SIZE and that it isn't in the cache yet."""
    if len(cache) < MAX_CACHE_SIZE:
        if multipart_upload.uploadId not in cache:
            cache[multipart_upload.uploadId] = multipart_upload


def remove_from_cache(multipart_upload):
    """Removes the given MultipartUpload from the cache, if in it."""
    if multipart_upload.uploadId in cache:
        del cache[multipart_upload.uploadId]


def get_multipart_upload(metadata):
    """Returns the multipart upload we have the ID of in metadata.
    As Amazon allows several multipart uploads at the same time
    for the same file, the ID is the only unique, reliable descriptor."""
    multipart_upload = None
    metadata_mp_id = None
    filename = metadata.path
    if filename.startswith(u"/"):
        filename = filename[1:]
    plug.logger.debug(u"Getting multipart upload of {}", filename)
    # Retrieve the stored multipart upload ID
    try:
        metadata_mp_id = metadata.extra['mp_id']
    except KeyError:  # No multipart upload ID
        # Raise now is faster (doesn't go through all the MP uploads)
        raise DriverError("Unable to retrieve multipart upload ID")
    if metadata_mp_id not in cache:
        # Try to only request multipart uploads of this file
        for mp in S3Conn.list_multipart_uploads(prefix=filename):
            # Go through all the multipart uploads
            # to find the one of this transfer
            if mp.uploadId == metadata_mp_id:
                multipart_upload = mp
                add_to_cache(mp)
                break
    else:
        multipart_upload = cache[metadata_mp_id]
    # At this point it shouldn't be None in any case
    if multipart_upload is None:
        raise DriverError("Cannot find upload for file '{}'"
                          .format(filename))
    plug.logger.debug(u"Found multipart upload of {} - ID {}",
                      filename, multipart_upload.uploadId)
    return multipart_upload


@plug.handler()
def set_chunk_size(chunk_size):
    if chunk_size < S3_MINIMUM_CHUNK_SIZE:
        return S3_MINIMUM_CHUNK_SIZE
    else:
        return None


@plug.handler()
def get_chunk(metadata, offset, size):
    filename = metadata.path
    plug.logger.debug(u"Downloading {} bytes from the {} key"
                      u" on bucket {}".format(size, filename,
                                              plug.options['bucket']))
    # Using the REST API "Range" header.
    headers = {'Range': "bytes={}-{}".format(offset, offset + (size-1))}
    try:
        key = S3Conn.get(filename, headers=headers)
    except requests.HTTPError as httpe:
        err = u"Cannot retrieve chunk from {} on bucket {}".format(
            filename, plug.options['bucket'])
        if httpe.response.status_code == 404:
            err += u": the file doesn't exist anymore."
        err += u" - {}".format(httpe)
        raise ServiceError(err)
    chunk = key.content
    plug.logger.debug(u"Download of {} bytes from the {} key on bucket {}"
                      u" is complete".format(size, filename,
                                             plug.options['bucket']))
    return chunk


@plug.handler()
def upload_chunk(metadata, offset, chunk):
    multipart_upload = get_multipart_upload(metadata)
    part_num = multipart_upload.number_of_parts() + 1
    plug.logger.debug(u"Start upload chunk of {}".format(
        u(multipart_upload.key)))
    plug.logger.debug(u"Uploading {} bytes at offset {}"
                      u" in part {}".format(len(chunk),
                                            offset, part_num))
    # upload_part_from_file expects a file pointer object.
    # we can simulate a file pointer with StringIO or BytesIO.
    # IOStream = StringIO in Python 2, BytesIO in Python 3.
    upload_fp = IOStream(chunk)
    try:
        multipart_upload.upload_part_from_file(upload_fp, part_num)
    except requests.HTTPError as httpe:
        plug.logger.debug(u"Chunk uploaded: {}".format(chunk))
        plug.logger.debug(httpe.response.text)
        err = u"Cannot upload part {} of {} multipart upload - {}"
        err = err.format(part_num, multipart_upload.key, httpe)
        raise ServiceError(err)
    plug.logger.debug(u"Chunk upload complete")
    add_to_cache(multipart_upload)
    upload_fp.close()


@plug.handler()
def start_upload(metadata):
    filename = metadata.path
    plug.logger.debug(u"Starting upload of '{}' to '{}' on bucket {}"
                      .format(filename, filename,
                              plug.options['bucket']))
    # Create a new multipart upload for this file
    new_mp = S3Conn.initiate_multipart_upload(filename)
    # Write the new multipart ID in metadata
    metadata.extra['mp_id'] = new_mp.uploadId
    # New file ? Create a default timestamp
    if metadata.extra.get('timestamp') is None:
        plug.logger.debug(u"Creating a new timestamp"
                          u" for {}".format(filename))
        metadata.extra['timestamp'] = 0.0
    metadata.write()
    # Store the Multipart upload id in cache
    add_to_cache(new_mp)
    plug.logger.debug(u"Storing upload ID {} for {}"
                      .format(new_mp.uploadId, filename))


@plug.handler()
def upload_file(metadata, data):
    filename = metadata.path
    plug.logger.debug(u"Starting one-shot upload of '{}' on bucket {}"
                      .format(filename, plug.options['bucket']))
    fp = IOStream(data)
    try:
        S3Conn.upload(filename, fp)
    except requests.HTTPError as httpe:
        err = u"Upload on file {} failed - {}"
        err = err.format(filename, httpe)
        raise ServiceError(err)
    plug.logger.debug(u"Chunk upload complete")


@plug.handler()
def end_upload(metadata):
    multipart_upload = get_multipart_upload(metadata)
    filename = metadata.path
    if filename.startswith(u"/"):
        filename = filename[1:]
    # Finish the upload on remote server before getting rid of the
    # multipart upload ID
    try:
        plug.logger.debug(u"Completing upload of {}", filename)
        if multipart_upload.number_of_parts() == 0:
            # Since we initialise the multipart upload in start_upload, it
            # may be useless since we may have called upload_file instead of
            # get_chunk, hence having sent no chunk at all. So in that case we
            # must cancel instead of complete.
            plug.logger.debug(u"Cancelling empty multipart upload of {}",
                              filename)
            multipart_upload.cancel_upload()
        else:
            multipart_upload.complete_upload()
    # If the file is left empty (i.e. for tests),
    # an exception is raised
    except requests.HTTPError as exc:
        # don't pollute S3 server with a void MP upload
        multipart_upload.cancel_upload()
        if metadata.size == 0:
            # Explicitly set this file contents to "nothing"
            fp = IOStream(b"")
            S3Conn.upload(filename, fp)
        else:
            remove_from_cache(multipart_upload)
            raise DriverError(u"Error while ending upload of {}: {}"
                              .format(filename, exc))
    # From here we're sure that's OK for Amazon
    new_timestamp = get_file_timestamp(filename)
    del metadata.extra['mp_id']  # erases the upload ID
    metadata.extra['timestamp'] = new_timestamp
    metadata.write()
    # Delete the mp id from cache
    remove_from_cache(multipart_upload)


@plug.handler()
def abort_upload(metadata):
    try:
        multipart_upload = get_multipart_upload(metadata)
        # Cancel the upload on remote server before getting rid of the
        # multipart upload ID
        multipart_upload.cancel_upload()
        # Delete the mp id from cache
        remove_from_cache(multipart_upload)
    except DriverError:
        plug.logger.info(u"Multipart upload of {} already cancelled",
                         metadata.filename)
    # From here we're sure that's OK for Amazon
    del metadata.extra['mp_id']  # erases the upload ID
    metadata.write()


@plug.handler()
def move_file(old_metadata, new_metadata):
    old_filename = old_metadata.path
    new_filename = new_metadata.path
    bucket = plug.options['bucket']
    plug.logger.debug(u"Moving file '{}' to '{}' on bucket '{}'"
                      .format(old_filename, new_filename, bucket))
    try:
        plug.logger.debug(u"Copying file '{}' to '{}' on bucket '{}'..."
                          .format(old_filename, new_filename, bucket))
        S3Conn.copy(old_filename, bucket, new_filename, bucket)
        plug.logger.debug(u"Copying file '{}' to '{}' on bucket '{}'..."
                          " - Done".format(old_filename, new_filename, bucket))
        plug.logger.debug(u"Deleting file '{}' on bucket '{}'..."
                          .format(old_filename, bucket))
        S3Conn.delete(old_filename)
        plug.logger.debug(u"Deleting file '{}' on bucket '{}'..."
                          u" - Done".format(old_filename, bucket))
        # Update timestamp of new object
        # This permits to not detect a new update in the check changes thread
        # and thus avoids an useless transfer the other way around
        plug.logger.debug(u"Updating timestamp of {}".format(new_filename))
        timestamp = get_file_timestamp(new_filename)
        new_metadata.extra['timestamp'] = timestamp
        new_metadata.write()
    except requests.HTTPError as httpe:
        raise ServiceError(u"Network problem while moving file - {}"
                           .format(httpe))


@plug.handler()
def delete_file(metadata):
    try:
        filename = metadata.path
        plug.logger.debug(u"Deleting {} "
                          u"on bucket {}".format(filename,
                                                 plug.options['bucket']))
        S3Conn.delete(filename)
    except requests.HTTPError as httpe:
        raise ServiceError(
            u"Error deleting file {} on bucket {}: {}".format(
                filename, plug.options['bucket'], httpe
                )
            )


class CheckChanges(threading.Thread):
    """A class spawned in a thread to poll for changes on the S3 bucket.
    Amazon S3 hasn't any bucket watching system in its API, so the best
    we can do is periodically polling the bucket's contents and compare the
    timestamps."""

    def __init__(self, folder, timer):
        threading.Thread.__init__(self)
        self.stopEvent = threading.Event()
        self.timer = timer
        self.folder = folder
        self.prefix = folder.path.lstrip(u"/")  # strip leading slashes
        # If the user provided '/' as root, self.prefix is "". But filtering
        # with '/' as prefix on Amazon S3 gives no result, so we absolutely
        # must not append a slash if the string's empty !!
        if not self.prefix.endswith(u"/") and self.prefix != u"":
            self.prefix += u"/"

    def check_bucket(self):
        prfx = self.prefix  # Folder name, e.g. "pando/music/"
        plug.logger.debug(u"Checking folder"
                          u" {} for changes".format(prfx))
        # We need to unroll the generator to be able to check for folders
        keys = [key for key in S3Conn.list(prefix=prfx)]
        plug.logger.debug(u"Processing {} files under '{}'", len(keys), prfx)
        # Getting multipart uploads once for all files under this folder
        # is WAY faster than re-getting them for each file
        multipart_uploads = S3Conn.get_all_multipart_uploads(prefix=prfx)
        keys_being_uploaded = [mp.key for mp in multipart_uploads]
        for key in keys:
            plug.logger.debug(u"Processing file '{}'".format(key['key']))
            # During an upload, files can appear on the S3 file system
            # before the transfer has been completed.
            # Skip if there's currently an upload going on
            if key in keys_being_uploaded:
                plug.logger.debug(u"Remote file '{}' is being uploaded"
                                  u" - skipped".format(key['key']))
                continue
            # Amazon S3 has no concept of folder, they are just empty files to
            # it. But we must not notify them on Onitu or it transfers them as
            # regular files. If a file is empty, check if other files begin
            # by its name (meaning it contains them)
            if key['size'] == 0:
                folderPrefix = key['key'] + u"/"
                children = [child for child in keys
                            if child['key'].startswith(folderPrefix)]
                if children:
                    plug.logger.debug(u"File '{}' is a folder on S3"
                                      u" - skipped".format(key['key']))
                    continue
            # Strip the folder name of the S3 filename for folder coherence.
            filename = key['key'][len(self.prefix):]
            metadata = plug.get_metadata(filename, self.folder)
            onitu_ts = metadata.extra.get('timestamp', 0.0)
            remote_ts = time.mktime(key['last_modified'].timetuple())
            if onitu_ts < remote_ts:  # Remote timestamp is more recent
                plug.logger.debug(u"Updating metadata"
                                  u" of file {}".format(metadata.path))
                metadata.size = int(key['size'])
                metadata.extra['timestamp'] = remote_ts
                plug.update_file(metadata)
        plug.logger.debug(u"Next check in folder {} in {} seconds",
                          self.folder.path, self.timer)

    def run(self):
        while not self.stopEvent.isSet():
            try:
                self.check_bucket()
            except requests.HTTPError as httpe:
                err = u"Error while polling Onitu's S3 bucket:"
                if httpe.response.status_code == 404:
                    err += u" The given bucket {} doesn't exist".format(
                        plug.options['bucket'])
                err += u" - {}".format(httpe)
                plug.logger.error(err)
            except requests.ConnectionError as conne:
                err = u"Failed to connect to Onitu's S3 bucket for polling"
                err += u" - {}".format(conne)
                plug.logger.error(err)
            # if the bucket read operation times out, cannot do much about it
            except SSLError as ssle:
                plug.logger.warning(u"Couldn't poll S3 bucket '{}': {}"
                                    .format(plug.options['bucket'], ssle))
            # Happens when connection is reset by peer
            except socket.error as serr:
                plug.logger.warning(u"Network problem, trying to reconnect. "
                                    u"{}".format(serr))
                get_conn()
            except EscalatorClosed:
                # We are closing
                return
            self.stopEvent.wait(self.timer)

    def stop(self):
        self.stopEvent.set()


def start():
    if plug.options['changes_timer'] < 0:
        raise DriverError(
            u"The change timer option must be a positive integer")
    get_conn()  # connection to S3
    for folder in plug.folders_to_watch:
        # Check that the given folder isn't a regular file
        try:
            folder_key = S3Conn.get(folder.path)
        except requests.HTTPError as httpe:
            if httpe.response.status_code == 404:
                # it's alright, root doesn't exist, no problem
                pass
            else:  # another error
                raise DriverError(u"Error while fetching folder '{}' on"
                                  u" bucket {}: {}"
                                  .format(folder.path,
                                          plug.options['bucket'],
                                          httpe))
        else:  # no error - root already exists
            # Amazon S3 has no concept of directories, they're just 0-size
            # files. So if root hasn't a size of 0, it is a regular file.
            if (len(folder_key.content) != 0 and folder.path != u"/"
               and len(folder.path) != 0):
                raise DriverError(u"Folder {} is a regular file on the"
                                  u"'{}' bucket. Please delete it"
                                  .format(folder.path,
                                          plug.options['bucket']))
        check = CheckChanges(folder, plug.options['changes_timer'])
        check.daemon = True
        check.start()

    plug.listen()
