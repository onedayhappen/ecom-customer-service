"""Immutable Chroma collections with an atomic, cross-process release pointer."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from app.rag.backends.chroma_backend import ChromaBackend
from app.rag.policy import PolicyError, PolicyMetadata, digest


class PolicyIndex:
    def __init__(self, persist_dir, name='ecom_kb'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', name):
            raise ValueError('Invalid index name')
        self.root = Path(persist_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / (name + '.policy-releases.sqlite3')
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS releases (
                    generation TEXT PRIMARY KEY, model TEXT NOT NULL, dimension INTEGER NOT NULL,
                    manifest TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS active_release (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation TEXT, revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO active_release VALUES (1,NULL,0);
                CREATE TABLE IF NOT EXISTS revoked_sources (
                    source_id TEXT NOT NULL, version TEXT NOT NULL, PRIMARY KEY(source_id,version));
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def revision(self):
        with self.db() as db:
            return db.execute('SELECT revision FROM active_release WHERE singleton=1').fetchone()[0]

    def backend(self, generation):
        return ChromaBackend(self.root, generation)

    def head(self):
        with self.db() as db:
            row = db.execute('''SELECT r.*,a.revision FROM active_release a JOIN releases r
                ON a.generation=r.generation WHERE a.singleton=1''').fetchone()
            if not row:
                raise PolicyError('policy_index_unpublished')
            result = dict(row)
            result['manifest'] = json.loads(result['manifest'])
            result['revoked'] = {(r['source_id'], r['version']) for r in db.execute('SELECT * FROM revoked_sources')}
            return result

    @staticmethod
    def token(head):
        return dict(generation=head['generation'], revision=head['revision'], model=head['model'],
                    manifest_digest=digest(head['manifest']))

    def publish(self, chunks, vectors, model, *, expected_revision):
        if not chunks or len(chunks) != len(vectors) or len(chunks) > 20000:
            raise PolicyError('invalid_policy_index_size')
        dimension = len(vectors[0])
        if not model or not 1 <= dimension <= 10000 or any(
            len(v) != dimension or not all(type(x) in {float, int} and math.isfinite(x) for x in v)
            or not any(x != 0 for x in v) for v in vectors):
            raise PolicyError('invalid_policy_vectors')
        manifest = {c.chunk_id: dict(doc=c.doc, section=c.section, policy=c.policy, text_digest=digest(c.text)) for c in chunks}
        if len(manifest) != len(chunks) or any(not c.policy for c in chunks):
            raise PolicyError('invalid_policy_manifest')
        try:
            for chunk in chunks:
                PolicyMetadata.model_validate({k: v for k, v in chunk.policy.items() if k != 'source_digest'})
                if not re.fullmatch(r'[0-9a-f]{64}', chunk.policy.get('source_digest', '')):
                    raise ValueError('Missing source digest')
        except (ValueError, TypeError, AttributeError) as error:
            raise PolicyError('invalid_policy_manifest') from error
        generation = 'policy_' + uuid4().hex
        backend = self.backend(generation)
        # This collection is new and unreachable by readers until activated.
        # A failed build never removes the collection used by the active index.
        backend.upsert(chunks, vectors, model)
        if backend.size() != len(chunks):
            raise PolicyError('policy_build_incomplete')
        self.verify_bodies(backend.fetch(list(manifest)), manifest)
        with self.db() as db:
            db.execute('INSERT INTO releases VALUES (?,?,?,?,?)', (generation, model, dimension,
                json.dumps(manifest, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
        self.activate(generation, expected_revision=expected_revision, model=model)
        return generation

    @staticmethod
    def verify_bodies(chunks, manifest):
        if {c.chunk_id for c in chunks} != set(manifest):
            raise PolicyError('policy_index_incomplete')
        for chunk in chunks:
            saved = manifest[chunk.chunk_id]
            if (digest(chunk.text) != saved['text_digest'] or chunk.doc != saved['doc']
                    or chunk.section != saved['section']):
                raise PolicyError('policy_index_corrupt')

    def activate(self, generation, *, expected_revision, model):
        with self.db() as db:
            row = db.execute('SELECT * FROM releases WHERE generation=?', (generation,)).fetchone()
        if row is None or row['model'] != model:
            raise PolicyError('policy_model_mismatch')
        backend = self.backend(generation)
        backend.load()
        if backend.expected_embedding_model() != model:
            raise PolicyError('policy_model_mismatch')
        manifest = json.loads(row['manifest'])
        self.verify_bodies(backend.fetch(list(manifest)), manifest)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute('''UPDATE active_release SET generation=?,revision=revision+1
                WHERE singleton=1 AND revision=?''', (generation, expected_revision)).rowcount
            if not changed:
                raise PolicyError('policy_publication_conflict')

    def revoke(self, source_id, version='*'):
        if not source_id or not version:
            raise PolicyError('invalid_policy_revocation')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('INSERT OR IGNORE INTO revoked_sources VALUES (?,?)', (source_id, version)).rowcount:
                db.execute('UPDATE active_release SET revision=revision+1 WHERE singleton=1')

    def current(self, token):
        try:
            return self.token(self.head()) == token
        except (PolicyError, sqlite3.Error, ValueError):
            return False
