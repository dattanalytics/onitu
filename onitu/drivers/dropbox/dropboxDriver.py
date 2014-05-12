import dropbox
import os
import sys
import requests
import threading
import time

from dropbox import rest
from configobj import ConfigObj

from onitu.api import Plug

plug = Plug()
drop = None

########################################################################
class DropboxDriver :
    """
    Dropbox object that can access your dropbox folder,
    as well as download and upload files to dropbox
    """
    cursor = None
    
    #----------------------------------------------------------------------
    def __init__(self, path='/'):
        """Constructor"""
        self.base_path = os.path.dirname(os.path.abspath(__file__))
        self.path = path
        self.client = None
        self.session = None
        self.access_type = "dropbox"
        self.client_tokens_file = plug.options['token_file']
        self.chunked_file_size = 25600000 # 25Mo
        
        self.session = dropbox.session.DropboxSession(plug.options["key"],
                                                      plug.options["secret"],
                                                      self.access_type)
 
        # Try to get a saved token, or get and store a new token with first_connect()
        try :
            with open(self.client_tokens_file) as token_file:
                token_key, token_secret = token_file.read().split('|')
        except (IOError, ValueError) as e :
            token_key, token_secret = self.first_connect()
 
        self.session.set_token(token_key, token_secret)
        self.client = dropbox.client.DropboxClient(self.session)
 
    #----------------------------------------------------------------------
    def first_connect(self):
        """
        Connect, authenticate with dropbox and store client tokens
        """
       
        request_token = self.session.obtain_request_token()
 
        url = self.session.build_authorize_url(request_token)
        msg = "Open %s and allow Onitu to use your dropbox."
        print msg % url
        while not ('access_token' in locals() or 'access_token' in globals()) :
            try :
                access_token = self.session.obtain_access_token(request_token)
            except (dropbox.rest.ErrorResponse) as e:
                time.sleep(2)

        with open(self.client_tokens_file, 'w') as token_file:
            token_file.write("%s|%s" % (access_token.key, access_token.secret))
   
        return access_token.key, access_token.secret
 
    #----------------------------------------------------------------------
    def download_file(self, filename, outDir=None):
        """
        Download either the file passed to the class or the file passed
        to the method
        """
 
        fname = filename
        f, metadata = self.client.get_file_and_metadata("/" + fname)
 
        if metadata['bytes'] > self.chunked_file_size :
            return self.download_big_file(fname)

        if outDir:
            dst = os.path.join(outDir, fname)
        else:
            dst = fname

 
        with open(fname, "wb") as fh:
            fh.write(f.read())
 
        return dst, metadata
 
    #----------------------------------------------------------------------
    def download_big_file(self, filename, outDir=None):
       
        fname = filename
        metadata = self.client.metadata(fname)
        size = metadata['bytes']

        if outDir:
            dst = os.path.join(outDir, fname)
        else:
            dst = fname

        endchunk = self.chunked_file_size
        startchunk = 0
        with open(fname, "wb") as fh:
            try:
                while startchunk < size:
                    url, params, headers = self.client.request("/files/dropbox/"+fname, {}, method='GET', content_server=True)
                    headers['Range'] = 'bytes=' + str(startchunk)+"-"+str(endchunk)
                    f = self.client.rest_client.request("GET", url, headers=headers, raw_response=True)
                    fh.write(f.read())
                    endchunk += self.chunked_file_size
                    startchunk += self.chunked_file_size + 1
                    if endchunk > size:
                        endchunk = size
                    
            except Exception, e:
                print "ERROR: ", e

        return dst, metadata
 
    #----------------------------------------------------------------------
    def download_chunk(self, filename, offset, size):
        if not filename.startswith("/files/dropbox/") :
            filename = "/files/dropbox/"+filename
        url, params, headers = self.client.request(filename, {}, method='GET', content_server=True)
        headers['Range'] = 'bytes=' + str(offset)+"-"+str(offset+size)
        f = self.client.rest_client.request("GET", url, headers=headers, raw_response=True)
        return f.read()
    #----------------------------------------------------------------------
    def upload_chunk(self, metadata, offset, chunk):

        uploader = self.client.get_chunked_uploader(StringIO(chunk), metadata.size)
        print "uploading: ", metadata.size
        print "uploading: ", metadata.filename
        
        uploader.offset = offset
        uploader.upload_chunked(len(chunk))

        res = uploader.finish(os.path.join(self.path, metadata.filename))
        print res
        # e, upload_id = self.client.upload_chunk(StringIO(chunk), len(chunk), offset)
        # print "Uploaded: ", e, " upload_id: ", upload_id
        # try:
        #     url, params, headers = self.client.request("/chunked_upload?offset="+str(offset), {}, method='PUT', content_server=True)
        #     print url
        #     print params
        #     self.client.rest_client.request("PUT", url, body=chunk, headers=headers, raw_response=True)
        # except Exception, e:
        #     plug.logger.warn("Error while uploading `{}`: {}", filename, e)
    #----------------------------------------------------------------------
    def upload_file(self, filename):
        """
        Upload a file to dropbox, returns file info dict
        """
        path = os.path.join(self.path, filename)
 
        if os.path.getsize(filename) > self.chunked_file_size :
            return self.upload_big_file(filename)
 
        try:
            with open(filename,'rb') as fh:
                res = self.client.put_file(path, fh)
                print "uploaded: ", res
        except Exception, e:
            print "ERROR: ", e
 
        return res
 
    #----------------------------------------------------------------------
    def upload_big_file(self, filename):
        """
        Upload a file to dropbox, returns file info dict
        """
        size = os.path.getsize(filename)
        with open(filename, 'rb') as fh:
            uploader = self.client.get_chunked_uploader(fh, size)
            print "uploading: ", size
       
            while uploader.offset < size:
                try:
                    uploader.upload_chunked(1024000)
                except rest.ErrorResponse, e:
                    pass
                   
            res = uploader.finish(os.path.join(self.path, filename))
 
        return res
           
    #----------------------------------------------------------------------
    def get_account_info(self):
        """
        Returns the account information, such as user's display name,
        quota, email address, etc
        """
        return self.client.account_info()
 
    #----------------------------------------------------------------------
    def list_folder(self, folder=None):
        """
       Return a dictionary of information about a folder
       """
        if folder:
            folder_metadata = self.client.metadata(folder)
        else:
            folder_metadata = self.client.metadata("/")
        return folder_metadata

    #----------------------------------------------------------------------
    def change_watcher(self):
        delta = self.client.delta(self.cursor)
        # print delta
        for f in delta["entries"]:
            if f[0][0] == '/':
                f[0] = f[0][1:]
            metadata = plug.get_metadata(f[0])
            metadata.size = f[1]["bytes"]
            metadata.revision = f[1]["revision"]
            plug.update_file(metadata)
        if delta['has_more']:
            self.change_watcher()
        else:
            threading.Timer(600.0, self.change_watcher).start()
 
@plug.handler()
def get_chunk(metadata, offset, size):
    drop = DropboxDriver()
    return drop.download_chunk(metadata.filename, offset, size)

@plug.handler()
def upload_chunk(metadata, offset, chunk):
    print "Upload chunk"
    print metadata
    print metadata.filename
    print metadata.size
    drop = DropboxDriver()
    return drop.upload_chunk(metadata, offset, chunk)

@plug.handler()
def end_upload(metadata):
    print "End upload"

@plug.handler()
def start_upload(metadata):
    print "Start upload: ", metadata.filename, " --- ", metadata.size
    print drop
    
def start(*args, **kwargs):
    plug.initialize(args[0])

    # print "-----Starting the driver-----"

    drop = DropboxDriver()

    drop.change_watcher()
    plug.listen()
