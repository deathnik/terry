from datetime import datetime, timedelta
from uuid import uuid4

import pymongo

from .api import AveryJob, IAveryJobController, IAveryWorkerController


__all__ = ['RetriableError', 'ConcurrencyError', 'AveryController']


class RetriableError(Exception):
    pass


class ConcurrencyError(Exception):
    pass


class AveryController(IAveryJobController, IAveryWorkerController):
    HEARTBEAT_TIMEOUT = timedelta(minutes=10)

    def __init__(self, db_uri, col_name='jobs'):
        self._validate_db_uri(db_uri)
        self._client = pymongo.MongoClient(db_uri)
        self._jobs = self._client.get_default_database()[col_name]
        self._ensure_indexes()

    def _validate_db_uri(self, uri):
        res = pymongo.uri_parser.parse_uri(uri)
        if 'database' not in res:
            raise Exception('You should explicitly specify database')

    def _ensure_indexes(self):
        def idx(*args, **kwargs):
            keys = [(field, pymongo.ASCENDING) for field in args]
            return pymongo.IndexModel(keys, **kwargs)

        self._jobs.create_indexes([idx('job_id', unique=True),
                                   idx('job_id', 'version'),
                                   idx('tags', 'status', 'worker_heartbeat')])

    def _job_from_doc(self, doc):
        return AveryJob(doc.pop('job_id'), **doc)

    ########################
    #    PUBLIC METHODS    #
    ########################

    def create_job_id(self):
        return uuid4().hex

    #############################
    #    IAveryJobController    #
    #############################

    def _update_job(self, job_id, version, **kwargs):
        query = {'job_id': job_id, 'version': version}

        update = {'$inc': {'version': 1},
                  '$set': kwargs}
        try:
            r = self._jobs.find_one_and_update(query, update, projection={'_id': False},
                                               return_document=pymongo.collection.ReturnDocument.AFTER)
        except pymongo.errors.AutoReconnect:
            raise RetriableError('find_one_and_update has failed due to AutoReconnect')

        if r is None:
            raise ConcurrencyError('invalid version: {}'.format(version))

    def get_job(self, job_id):
        try:
            doc = self._jobs.find_one({'job_id': job_id}, projection={'_id': False})
        except pymongo.errors.AutoReconnect:
            raise RetriableError('find_one has failed due to the AutoReconnect error')

        return self._job_from_doc(doc)

    def create_job(self, job_id, tag, args=None):
        doc = {'job_id': job_id, 'tag': tag, 'args': args or {},
               'version': 0, 'status': AveryJob.IDLE}
        try:
            self._jobs.insert_one(doc)
        except pymongo.errors.AutoReconnect:
            raise RetriableError('insert_one has failed due to the AutoReconnect error')
        except pymongo.errors.DuplicateKeyError:
            pass

    def cancel_job(self, job_id, version):
        self._update_job(job_id, version, status=AveryJob.CANCELLED)

    def delete_job(self, job_id, version):
        try:
            r = self._jobs.delete_one({'job_id': job_id, 'version': version})
        except pymongo.errors.AutoReconnect:
            raise RetriableError('delete_one has failed due to AutoReconnect')

        if r.deleted_count == 0:
            raise ConcurrencyError('job_id={}, version={} not found'.format(job_id, version))

        assert r.deleted_count == 1

    ################################
    #    IAveryWorkerController    #
    ################################

    def _find_one_and_update(self, query, update):
        try:
            r = self._jobs.find_one_and_update(query, update, projection={'_id': False},
                                               return_document=pymongo.collection.ReturnDocument.AFTER)
        except pymongo.errors.AutoReconnect:
            raise RetriableError('find_one_and_update has failed due to the AutoReconnect error')

        if r:
            return self._job_from_doc(r)

    def _try_acquire_idle_job(self, tags, worker_id):
        query = {'tag': {'$in': tags}, 'status': AveryJob.IDLE}
        update = {'$inc': {'version': 1},
                  '$set': {'status': AveryJob.LOCKED,
                           'worker_id': worker_id,
                           'worker_heartbeat': datetime.utcnow()}}

        return self._find_one_and_update(query, update)

    def _try_reacquire_locked_job(self, tags, worker_id):
        query = {'tag': {'$in': tags}, 'status': AveryJob.LOCKED,
                 'worker_heartbeat': {'$lt': datetime.utcnow() - self.HEARTBEAT_TIMEOUT}}

        update = {'$inc': {'version': 1},
                  '$set': {'status': AveryJob.LOCKED,
                           'worker_id': worker_id,
                           'worker_heartbeat': datetime.utcnow()}}

        return self._find_one_and_update(query, update)

    def acquire_job(self, tags, worker_id):
        job = self._try_acquire_idle_job(tags, worker_id)

        if job is None:
            job = self._try_reacquire_locked_job(tags, worker_id)

        if job is None:
            # there are no available jobs
            return None

        return job

    def heartbeat_job(self, job_id, version):
        self._update_job(job_id, version, worker_heartbeat=datetime.utcnow())

    def finalize_job(self, job_id, version, status, worker_exception=None):
        self._update_job(job_id, version, status=status, worker_exception=worker_exception)
