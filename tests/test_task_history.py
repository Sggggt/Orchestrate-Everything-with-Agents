"""Paged task history and user handling preserve execution and accounting facts."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from oea import AgentDefinition, Model, RunSpec, RunHandlingSelection
from oea.contracts.errors import RuntimeFault
from oea.migration.handling import PRE_HANDLING_SCHEMA
from oea.migration.upgrade import upgrade_state
from oea.runtime.host import Host
from oea.storage.persistence import one


class TaskHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='oea-task-history-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.host = await Host(self.root / 'source').__aenter__()
        self.addAsyncCleanup(self.host.aclose)
        for project in ('p', 'q'):
            await self.host.projects.create(project, project_id=project)
            await self.host.agents.save(AgentDefinition(id='agent-' + project, project_id=project, name='Agent', model=Model(base_url='http://model.invalid', model='test')))
            await self.host.sessions.create(project, 'First', session_id='session-' + project)
        await self.host.sessions.create('p', 'Older session', session_id='older')
        self.instances = {}
        for session, agent in (('session-p', 'agent-p'), ('older', 'agent-p'), ('session-q', 'agent-q')):
            self.instances[session] = (await self.host.instances.create(session, agent))['id']

    async def seed(self, key, *, state='completed', session='session-p', usage=False):
        def write(db):
            row, _ = self.host.runs._submit(db, RunSpec(instance_id=self.instances[session], input='Synthetic task ' + key, idempotency_key=key))
            db.execute('UPDATE runs SET state=?,stopped_at=? WHERE id=?', (state, row['created_at'] if state in {'completed', 'failed', 'cancelled'} else None, row['id']))
            if usage:
                db.execute("INSERT INTO usage(id,budget_id,run_id,purpose,kind,state,input_tokens,output_tokens,created_at) VALUES(?,?,?,'model','model','needs_review',100,20,?)", ('usage-' + key, row['budget_id'], row['id'], row['created_at']))
            return row['id']
        return await self.host.store.call(write, transaction=True)

    async def selection(self, identity):
        row = await self.host.runs.get(identity)
        return RunHandlingSelection(run_id=identity, expected_updated_at=row['updated_at'])

    async def test_project_history_pages_include_archived_sessions_without_duplicates(self):
        expected = [await self.seed(str(index), session='older' if index % 2 else 'session-p') for index in range(27)]
        await self.seed('other-project', session='session-q')
        await self.host.sessions.set_archived('older', 'p', True)
        found, cursor = [], 0
        while cursor is not None:
            page = await self.host.registry.call('runs.list', {'project_id': 'p', 'limit': 7, 'cursor': cursor})
            found.extend(row['id'] for row in page['items'])
            cursor = page['cursor']
        self.assertEqual(found, list(reversed(expected)))
        self.assertEqual(len(found), len(set(found)))
        current = await self.host.runs.list('session-p', project_id='p', limit=100)
        self.assertTrue(all(row['session_id'] == 'session-p' for row in current['items']))

    async def test_batch_handling_hides_attention_but_keeps_original_facts(self):
        failed = await self.seed('failed', state='failed')
        pending = await self.seed('cost', usage=True)
        normal = await self.seed('normal')
        self.assertEqual((await self.host.sessions.summary('session-p', 'p'))['attention_count'], 2)
        before = await self.host.store.call(lambda db: one(db, 'SELECT * FROM usage WHERE run_id=?', (pending,)))
        choices = [await self.selection(failed), await self.selection(pending)]
        marked = await self.host.registry.call('runs.mark_handled', {'project_id': 'p', 'items': [item.model_dump() for item in choices]})
        self.assertEqual(len(marked['items']), 2)
        self.assertEqual(marked['conflicts'], [])
        self.assertEqual((await self.host.runs.list(project_id='p', view='attention'))['items'], [])
        self.assertEqual((await self.host.sessions.summary('session-p', 'p'))['attention_count'], 0)
        self.assertEqual((await self.host.runs.get(failed))['state'], 'failed')
        self.assertEqual((await self.host.runs.get(pending))['state'], 'completed')
        self.assertEqual((await self.host.runs.get(pending))['handling_state'], 'handled')
        self.assertEqual(before, await self.host.store.call(lambda db: one(db, 'SELECT * FROM usage WHERE run_id=?', (pending,))))
        repeated = await self.host.runs.mark_handled('p', choices)
        self.assertTrue(all(item['reused'] for item in repeated['items']))
        events = await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM events WHERE kind='run.handled'"))
        self.assertEqual(events['n'], 2)
        self.assertEqual(len((await self.host.runs.list(project_id='p'))['items']), 3)
        self.assertEqual((await self.host.runs.get(normal))['handling_state'], 'none')

    async def test_batch_authorization_precedes_all_mutations_and_excludes_application_records(self):
        local = await self.seed('local', state='failed')
        other = await self.seed('other', state='failed', session='session-q')
        for forbidden in (other,):
            with self.assertRaises(RuntimeFault):
                await self.host.runs.mark_handled('p', [await self.selection(local), await self.selection(forbidden)])
            self.assertIsNone((await self.host.runs.get(local))['handled_at'])
        hidden = await self.seed('application', state='failed', session='older')
        await self.host.store.call(lambda db: db.execute("UPDATE sessions SET source='application' WHERE id='older'"), transaction=True)
        self.assertNotIn(hidden, [row['id'] for row in (await self.host.runs.list(project_id='p'))['items']])
        with self.assertRaises(RuntimeFault):
            await self.host.registry.call('runs.mark_handled', {'project_id': 'p', 'items': [(await self.selection(local)).model_dump(), (await self.selection(hidden)).model_dump()]})
        self.assertIsNone((await self.host.runs.get(local))['handled_at'])

    async def test_changed_rows_are_not_acknowledged_and_waiting_instances_are_not_released(self):
        waiting = await self.seed('waiting', state='waiting')
        failed = await self.seed('failed', state='failed')
        choices = [await self.selection(waiting), await self.selection(failed)]
        await self.host.store.call(lambda db: db.execute('UPDATE instances SET active_run_id=? WHERE id=?', (waiting, self.instances['session-p'])), transaction=True)
        await self.host.store.call(lambda db: db.execute('UPDATE runs SET updated_at=updated_at+1 WHERE id=?', (failed,)), transaction=True)
        result = await self.host.runs.mark_handled('p', choices)
        self.assertEqual([item['run_id'] for item in result['items']], [waiting])
        self.assertEqual([item['run_id'] for item in result['conflicts']], [failed])
        self.assertEqual((await self.host.instances.get(self.instances['session-p']))['active_run_id'], waiting)
        self.assertEqual((await self.host.runs.get(waiting))['state'], 'waiting')
        await self.host.store.call(lambda db: self.host.runs._resume(db, waiting, 'resume', input='Continue'), transaction=True)
        self.assertIsNone((await self.host.runs.get(waiting))['handled_at'])
        await self.host.finish(waiting, 'failed', error=RuntimeFault('EXAMPLE_FAILURE', 'New attempt failed'))
        self.assertEqual((await self.host.runs.get(waiting))['handling_state'], 'pending')

    async def test_handled_state_survives_restart(self):
        task = await self.seed('closed', state='failed')
        await self.host.runs.mark_handled('p', [await self.selection(task)])
        await self.host.aclose()
        async with Host(self.root / 'source') as reopened:
            self.assertEqual((await reopened.runs.get(task))['handling_state'], 'handled')
            self.assertEqual((await reopened.runs.list(project_id='p', view='attention'))['items'], [])

    async def test_new_command_uncertainty_reopens_acknowledged_attention(self):
        task = await self.seed('command', state='waiting')
        await self.host.runs.mark_handled('p', [await self.selection(task)])
        await self.host.store.call(lambda db: db.execute("INSERT INTO actions(id,run_id,operation_key,name,input_hash,input_json,side_effects,state,created_at,updated_at) VALUES('command',?,'command','exec_command','input','{}','local','completed',1,1)", (task,)), transaction=True)
        await self.host._command_settled({'run_id': task, 'action_id': 'command', 'command_session_id': 'command', 'process_stopped': False, 'state': 'unknown'})
        actual = await self.host.runs.get(task)
        self.assertEqual(actual['state'], 'needs_review')
        self.assertEqual(actual['handling_state'], 'pending')
        self.assertFalse(actual['stop_confirmed'])

    async def test_confirmed_stop_does_not_reopen_already_acknowledged_usage(self):
        task = await self.seed('stopping', state='waiting', usage=True)
        await self.host.runs.mark_handled('p', [await self.selection(task)])
        await self.host.finish(task, 'cancelled')
        actual = await self.host.runs.get(task)
        self.assertEqual(actual['state'], 'cancelled')
        self.assertEqual(actual['handling_state'], 'handled')
        self.assertEqual((await self.host.runs.list(project_id='p', view='attention'))['items'], [])

    async def test_offline_handling_upgrade_retains_history_and_checkpoint_bytes(self):
        task = await self.seed('original', usage=True)
        await self.host.aclose()
        source = self.root / 'source'
        frozen = Path(__file__).parents[1] / 'src/oea/migration/schema-v3-pre-handling.sql'
        self.assertEqual(hashlib.sha256(frozen.read_text(encoding='utf-8').encode()).hexdigest(), PRE_HANDLING_SCHEMA)
        with closing(sqlite3.connect(source / 'oea.sqlite')) as db, db:
            db.execute('DROP INDEX usage_attention')
            db.execute('ALTER TABLE runs DROP COLUMN handled_at')
            db.execute("UPDATE metadata SET value=? WHERE key='schema_sha256'", (PRE_HANDLING_SCHEMA,))
            db.execute("UPDATE metadata SET value='3' WHERE key='schema_revision'")
        original = (source / 'oea.sqlite').read_bytes()
        graphs = (source / 'graphs.sqlite').read_bytes()
        target = self.root / 'upgraded'
        self.assertFalse(upgrade_state(source, target=target)['applied'])
        self.assertTrue(upgrade_state(source, target=target, apply=True)['applied'])
        self.assertEqual((source / 'oea.sqlite').read_bytes(), original)
        self.assertEqual((target / 'graphs.sqlite').read_bytes(), graphs)
        async with Host(target) as upgraded:
            run = await upgraded.runs.get(task)
            self.assertEqual(run['state'], 'completed')
            self.assertEqual(run['handling_state'], 'pending')
            self.assertIsNone(run['handled_at'])
