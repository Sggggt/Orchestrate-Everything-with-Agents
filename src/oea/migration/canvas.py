"""Add candidate ownership and cached canvas projections without changing execution."""

import json

from oea.orchestration.architecture_preview import canvas_preview
from oea.storage.persistence import encode

PRE_CANVAS_SCHEMA = '0005237484cd93ac06c9a4bed4e560e7a8a259ad713245cbe397f0670c62a50b'


def upgrade_canvas(db):
    db.execute('ALTER TABLE agent_architectures ADD COLUMN owner_agent_id TEXT REFERENCES agent_definitions(id)')
    db.execute('ALTER TABLE agent_architectures ADD COLUMN removed INTEGER NOT NULL DEFAULT 0 CHECK(removed IN (0,1))')
    db.execute('CREATE INDEX agent_architectures_owner ON agent_architectures(project_id,owner_agent_id,removed)')
    for identity, representation, draft, preview in db.execute('SELECT id,representation,draft_json,preview_json FROM agent_architectures').fetchall():
        if representation == 'declarative':
            db.execute('UPDATE agent_architectures SET preview_json=? WHERE id=?',
                       (encode({**json.loads(preview), 'canvas': canvas_preview(json.loads(draft))}), identity))
            for version, definition, saved in db.execute('SELECT version,definition_json,preview_json FROM architecture_versions WHERE architecture_id=?', (identity,)).fetchall():
                db.execute('UPDATE architecture_versions SET preview_json=? WHERE architecture_id=? AND version=?',
                           (encode({**json.loads(saved), 'canvas': canvas_preview(json.loads(definition))}), identity, version))
