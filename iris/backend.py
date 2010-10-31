#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Backend data helpers for iris.

Iris uses mongodb because it's web scale."""

import os
import imghdr

import pymongo
import threading

from iris.loaders import file, picasa
from iris.utils import memoize, OpenStruct

@memoize
def get_database(host=None, port=None):
    """Get the iris mongo db from host and port.  If none are supplied, attempt
    to read an iris configuration file.  If it doesn't exist, connect to
    localhost:27017."""
    if host is None or port is None:
        from iris import config
        cfg = config.IrisConfig()
        try:
            cfg.read()
            host, port = cfg.host, cfg.port
        except:
            host, port = '127.0.0.1', 27017
    connection = pymongo.Connection(host, port)
    db = connection.iris
    photos = db.photos
    photos.create_index([('path', pymongo.DESCENDING)])
    photos.create_index([('date', pymongo.DESCENDING)])
    return connection.iris


class BulkUpdater(object):
    """A caching updater for mongo documents going into the same collection.
    You can choose a threshold, and add documents to it, and they will be
    flushed after the threshold number of documents have been reached."""
    def __init__(self, collection, threshold=100, unique_attr=None):
        # this has to be reentrant so we can protect flushes
        self.collection = collection
        self.unique_attr = unique_attr
        self.threshold = threshold
        self.total = 0
        self.documents = {
            'updates' : [],
            'inserts' : [],
        }
        self._inserts = 0
        self._updates = 0
        self.lock = threading.RLock()

    def update(self, *documents):
        """Add one or more documents to be updated whenever the threshold is met.
        If the document has an '_id', it's considered an update.  If it doesn't,
        it's considered an insert.  If the document has a '_unique_attr'
        attribute, it's used later to check if it is actually an insert or an
        update.  This method is thread safe."""
        self.lock.acquire()
        for document in documents:
            if '_id' in document:
                self.documents['updates'].append(document)
            else:
                self.documents['inserts'].append(document)
            self.total += 1
            if self.total >= self.threshold:
                self.flush(False)
        self.lock.release()

    def flush(self, force=True):
        """Flush all of the documents with as few queries as possible.  If
        force is False (default), documents are only saved if they meet the
        threshold.  This method is thread safe."""
        self.lock.acquire()
        if self.total < self.threshold and not force:
            self.lock.release()
            return
        self._flush()
        self.lock.release()

    def _flush(self):
        """Save all documents with as few queries as possible.  Checks the
        db for 'inserts' documents that pre-exist (based on the presence of
        a '_unique_attr' attribute), and moves those that exist over to the
        'updates', then bulk inserts whatever's left and saves the others one
        at a time.  This method is NOT thread safe."""
        updates = self.documents['updates']
        inserts = self.documents['inserts']
        lookups = {}
        # check all insert documents for a 'unique attr' that will allow us
        # to search the db for possible duplicates
        for document in inserts:
            unique = getattr(document, '_unique_attr', self.unique_attr)
            if unique:
                lookups.setdefault(unique, {})[document[unique]] = document
        # search db for duplicates based on unique attrs of insert documents
        results = {}
        for lookup,values in lookups.iteritems():
            keys = [lookup]
            if '_id' not in keys:
                keys.append('_id')
            spec = {lookup: {'$in' : list(values)}}
            results[lookup] = dict([(d[lookup], d['_id']) for d in self.collection.find(spec, keys)])
        # for each match, remove that document from inserts, add the _id from
        # the database, and add that document to updates
        for key in results:
            for unique, _id in results[key].iteritems():
                # add _id to document in inserts with unique value 'unique'
                document = lookups[key][unique]
                # XXX: this is O(n) but it could be O(1);  room for improvement
                # here with potentially large docucment cache thresholds
                inserts.remove(document)
                document['_id'] = _id
                updates.append(document)
        if inserts:
            self.collection.insert(inserts)
        for doc in updates:
            self.collection.save(doc)
        self._clear()

    def _clear(self):
        """Clears out documents that have already been flushed.  This method
        is NOT thread safe."""
        self._inserts += len(self.documents['inserts'])
        self._updates += len(self.documents['updates'])
        self.documents.clear()
        self.documents['updates'] = []
        self.documents['inserts'] = []
        self.total = 0


class PagingCursor(object):
    def __init__(self, collection, sort=None):
        self.collection = collection
        self.sort = sort or [('_id', pymongo.DESCENDING)]

    def find(self, spec, fields=None, sort=None):
        pass

class Model(OpenStruct):
    def save(self):
        import bson
        db = get_database()
        collection_name = getattr(self, '_collection', None)
        collection = db[collection_name]
        try:
            collection.save(self.__dict__)
        except bson.errors.InvalidDocument:
            import traceback
            tb = traceback.format_exc()
            import ipdb; ipdb.set_trace();


class Photo(Model):
    _collection = 'photos'

    def from_path(self, path):
        path = os.path.realpath(path)
        meta = file.MetaData(path)
        copykeys = ('x', 'y', 'exif', 'iptc', 'tags', 'path')
        d = dict([(k,v) for k,v in meta.__dict__.iteritems() if k in copykeys])
        self.__dict__.update(d)
        stat = os.stat(meta.path)
        self.size = stat.st_size

    def _init_from_dict(self, d):
        """When loading from the db."""
        self.__dict__.update(d)

    def sync(self):
        self.load(self.path)
        self.save()

class FileLoader(object):
    pass

