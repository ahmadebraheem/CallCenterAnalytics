"""Run ONLY against a disposable ClickHouse server.

Uses an isolated randomly named database, then removes that database. Takes the
generated fixture directory as its only argument; credentials are environment-based.
"""
from pathlib import Path
import os
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import load as loader
import checks


def must_fail(function, text):
    try:
        function()
    except Exception as error:
        assert text in str(error), str(error)
    else:
        raise AssertionError('Expected failure: '+text)


def main(directory):
    loader.DATABASE = 'loader_test_'+uuid.uuid4().hex
    ch = loader.client()
    other = loader.client()
    report = checks.validate(directory, loader.CATALOG)
    try:
        loader.create_tables(ch)
        # Real atomic exclusion across two independently connected clients.
        with loader.writer_lock(ch):
            def overlap():
                with loader.writer_lock(other):
                    raise AssertionError('Concurrent writer acquired the lock')
            must_fail(overlap, 'Cannot acquire loader lock')
            must_fail(lambda: loader.unlock(other, 'wrong-owner'), 'owner mismatch')
        # Exercise the relative path used by the local CLI's default --directory out.
        result = loader.load(ch, Path(os.path.relpath(directory)), 'dataset-a', report=report)
        assert result['status'] == 'loaded'
        assert loader.load(ch, directory, 'dataset-a', report=report)['status'] == 'skipped'
        must_fail(lambda: loader.load(ch, directory, 'dataset-b', report=report), 'Identical content')
        assert loader.lock_owner(ch) is None
        # Corrupt a physical raw row without changing the completion ledger.
        ch.command(f'INSERT INTO {loader.DATABASE}.dim_site SELECT * FROM {loader.DATABASE}.dim_site LIMIT 1')
        must_fail(lambda: loader.load(ch, directory, 'dataset-a', report=report), 'Count/key/load identity mismatch')
        assert loader.lock_owner(ch) is None
        ch.command(f'DROP DATABASE {loader.DATABASE} SYNC')
        loader.create_tables(ch)
        class FailAfterFirstInsert:
            def __getattr__(self, name):
                return getattr(ch, name)
            def insert_arrow(self, *args, **kwargs):
                ch.insert_arrow(*args, **kwargs)
                raise RuntimeError('simulated lost acknowledgment')
        must_fail(lambda: loader.load(FailAfterFirstInsert(), directory, 'retry', report=report), 'lost acknowledgment')
        assert not loader.completed(ch, 'retry')
        assert any(loader.pending_rows(ch, 'retry').values())
        owner = loader.lock_owner(ch)
        assert owner
        must_fail(lambda: loader.load(other, directory, 'retry', report=report), 'Cannot acquire loader lock')
        loader.unlock(ch, owner)  # Test confirms failed writer has stopped.
        must_fail(lambda: loader.load(ch, directory, 'retry', report=report), 'Incomplete physical rows')
        result = loader.load(ch, directory, 'retry', report=report, retry=True)
        assert result['status'] == 'loaded'
        loader.verify_data(ch, 'retry', report, result['load_id'])
        # Published data is never deleted by --retry.
        assert loader.load(ch, directory, 'retry', report=report, retry=True)['status'] == 'skipped'
        assert loader.pending_rows(ch, 'retry') == {k:v['rows'] for k,v in report['tables'].items()}
        # Reconciliation detects changes even with unchanged row counts/keys.
        ch.command(f"ALTER TABLE {loader.DATABASE}.interaction_resource_fact UPDATE DURATION=DURATION+1 "
                   "WHERE _dataset_id='retry'", settings={'mutations_sync': 2})
        must_fail(lambda: loader.verify_data(ch, 'retry', report, result['load_id']), 'Total mismatch')
        ch.command(f'DROP DATABASE {loader.DATABASE} SYNC')
        loader.create_tables(ch)
        class FailAfterPublication:
            def __getattr__(self, name):
                return getattr(ch, name)
            def insert(self, *args, **kwargs):
                ch.insert(*args, **kwargs)
                raise RuntimeError('publication acknowledgment lost')
        must_fail(lambda: loader.load(FailAfterPublication(), directory, 'published', report=report), 'acknowledgment lost')
        assert len(loader.completed(ch,'published'))==1
        loader.unlock(ch, loader.lock_owner(ch))
        assert loader.load(ch,directory,'published',retry=True,report=report)['status']=='skipped'
        print('PASS: seven-table load, replay, cross-ID duplicates, concurrent writers, owner protection, '
              'raw duplicates, lost acknowledgment, retained lock, retry cleanup, and aggregate corruption')
    finally:
        ch.command(f'DROP DATABASE IF EXISTS {loader.DATABASE} SYNC')
        ch.close()
        other.close()


if __name__ == '__main__':
    main(Path(sys.argv[1]))
