"""Candidate ownership, cached canvas projections and retained execution versions."""

import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx

from oea import AgentArchitecture, AgentDefinition, ArchitectureBinding, ArchitectureClient, Completion, Model, RunSpec
from oea.contracts.errors import RuntimeFault
from oea.migration.canvas import PRE_CANVAS_SCHEMA
from oea.migration.upgrade import upgrade_state
from oea.orchestration.architecture_templates import react
from oea.runtime.host import Host
from oea.web.app import create_app


class ModelStub:
    async def complete(self, *args, **kwargs):
        return Completion(text='Complete', usage={'prompt_tokens': 10, 'completion_tokens': 2})


class CanvasTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='oea-canvas-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.host = await Host(self.root / 'source', model_api=ModelStub()).__aenter__()
        self.addAsyncCleanup(self.host.aclose)
        await self.host.projects.create('Project', project_id='p')
        await self.host.projects.create('Other', project_id='q')
        for identity in ('first', 'second'):
            await self.host.agents.save(AgentDefinition(id=identity, project_id='p', name=identity, model=Model(base_url='http://model.invalid', model='test')))
        self.client = ArchitectureClient(self.host)
        self.graph = react().model_dump(mode='json')
        self.graph['metadata']['extension'] = {'preserve': [1, 2]}
        self.definition = AgentArchitecture(id='draft', project_id='p', owner_agent_id='first', name='Candidate')
        await self.client.create(self.definition, self.graph)

    async def test_owner_filter_and_creation_idempotency(self):
        self.assertEqual((await self.client.create(self.definition, self.graph))['revision'], 1)
        self.assertEqual(len((await self.client.list('p', owner_agent_id='first'))['items']), 1)
        self.assertEqual((await self.client.list('p', owner_agent_id='second'))['items'], [])
        with self.assertRaises(RuntimeFault):
            await self.client.create(AgentArchitecture(id='wrong', project_id='q', owner_agent_id='first', name='Wrong'), self.graph)

    async def test_remove_candidate_retains_current_version_and_execution(self):
        published = await self.client.publish('draft', 'p', 1)
        binding = ArchitectureBinding(architecture_id='draft', version=published['version'])
        await self.client.bind('first', 'p', binding, 1)
        with self.assertRaises(RuntimeFault):
            await self.client.bind('second', 'p', binding, 1)
        with self.assertRaises(RuntimeFault):
            await self.client.remove('draft', 'p', 'second', 1)
        result = await self.client.remove('draft', 'p', 'first', 1)
        self.assertTrue(result['removed'])
        self.assertEqual(await self.client.remove('draft', 'p', 'first', 1), result)
        self.assertEqual((await self.client.list('p'))['items'], [])
        self.assertEqual(len((await self.client.list('p', include_removed=True))['items']), 1)
        for action in (self.client.publish('draft', 'p', 2), self.client.save('draft', 'p', 2, self.graph)):
            with self.assertRaises(RuntimeFault) as fault:
                await action
            self.assertEqual(fault.exception.code, 'ARCHITECTURE_REMOVED')
        self.assertEqual((await self.client.get('draft', 'p', version=1))['definition'], self.graph)
        await self.host.sessions.create('p', session_id='s')
        instance = await self.host.instances.create('s', 'first')
        run = await self.host.runs.submit(RunSpec(instance_id=instance['id'], input='Test', idempotency_key='once'))
        self.assertEqual((await self.host.runs.result(run.id, timeout=10)).text, 'Complete')

    async def test_rename_compare_and_cached_topology_preserve_hidden_fields(self):
        await self.client.publish('draft', 'p', 1)
        graph = json.loads(json.dumps(self.graph))
        graph['nodes'][0]['position'] = {'x': 128, 'y': 240}
        saved = await self.client.save('draft', 'p', 1, graph, name='Renamed')
        self.assertEqual(saved['name'], 'Renamed')
        self.assertEqual(saved['draft']['metadata']['extension'], {'preserve': [1, 2]})
        with self.assertRaises(RuntimeFault):
            await self.client.save('draft', 'p', 1, self.graph)
        with patch.object(self.host.architectures, '_preview', side_effect=AssertionError('No traversal on read')):
            preview = await self.client.preview('draft', 'p', detail='canvas')
            original = await self.client.preview('draft', 'p', version=1, detail='canvas')
        self.assertEqual(preview['canvas']['nodes'][0]['position'], {'x': 128, 'y': 240})
        self.assertNotEqual(original['canvas']['nodes'][0]['position'], preview['canvas']['nodes'][0]['position'])
        self.assertTrue(all('config' not in node for node in preview['canvas']['nodes']))
        self.assertEqual(len(preview['canvas']['edges']), len(graph['edges']))

    async def test_function_discovery_is_paginated_and_never_calls_function(self):
        def addition(value: int) -> int:
            raise AssertionError('Discovery must not execute the function')
        self.host.workflows.register_function(addition)
        page = await self.client.functions(limit=1)
        self.assertEqual(page['items'][0]['name'], 'addition')
        detail = await self.client.functions('addition')
        self.assertEqual(detail['input_schema']['properties']['value']['type'], 'integer')

    async def test_http_and_registry_share_remove_semantics(self):
        app = create_app(host=self.host)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1', headers={'Authorization': 'Bearer ' + app.state.token}) as client:
                response = await client.post('/api/rpc', json={'protocol_version': '3.0', 'request_id': 'remove', 'operation': 'architectures.remove',
                    'params': {'project_id': 'p', 'architecture_id': 'draft', 'owner_agent_id': 'first', 'expected_revision': 1}})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()['result']['removed'])
                description = self.host.registry.describe('architectures.remove')
                self.assertFalse(description['read_only'])

    async def test_offline_upgrade_preserves_original_versions_and_checkpoints(self):
        await self.client.publish('draft', 'p', 1)
        await self.host.aclose()
        source = self.root / 'source'
        frozen = Path(__file__).parents[1] / 'src/oea/migration/schema-v3-pre-canvas.sql'
        self.assertEqual(hashlib.sha256(frozen.read_text(encoding='utf-8').encode()).hexdigest(), PRE_CANVAS_SCHEMA)
        with closing(sqlite3.connect(source / 'oea.sqlite')) as db, db:
            db.execute('DROP INDEX usage_attention')
            db.execute('ALTER TABLE runs DROP COLUMN handled_at')
            db.execute('DROP INDEX agent_architectures_owner')
            db.execute('ALTER TABLE agent_architectures DROP COLUMN owner_agent_id')
            db.execute('ALTER TABLE agent_architectures DROP COLUMN removed')
            db.execute("UPDATE metadata SET value=? WHERE key='schema_sha256'", (PRE_CANVAS_SCHEMA,))
            db.execute("UPDATE metadata SET value='2' WHERE key='schema_revision'")
            original = db.execute('SELECT definition_json,sha256 FROM architecture_versions').fetchall()
        before = (source / 'oea.sqlite').read_bytes()
        checkpoint = (source / 'graphs.sqlite').read_bytes()
        target = self.root / 'upgraded'
        dry = upgrade_state(source, target=target)
        self.assertFalse(dry['applied'])
        self.assertFalse(target.exists())
        report = upgrade_state(source, target=target, apply=True)
        self.assertTrue(report['applied'])
        self.assertEqual((source / 'oea.sqlite').read_bytes(), before)
        self.assertEqual((target / 'graphs.sqlite').read_bytes(), checkpoint)
        async with Host(target, model_api=ModelStub()) as host:
            value = await host.architectures.get('draft', 'p')
            self.assertIsNone(value['owner_agent_id'])
            self.assertEqual((await host.architectures.preview('draft', 'p', version=1, detail='canvas'))['canvas']['entry'], self.graph['entry'])
        with closing(sqlite3.connect(target / 'oea.sqlite')) as db:
            self.assertEqual(db.execute('SELECT definition_json,sha256 FROM architecture_versions').fetchall(), original)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
