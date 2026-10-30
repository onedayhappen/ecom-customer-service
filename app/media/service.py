# 文件说明：管理私有图片资产生命周期、有效关联与按问题保存的视觉观察。
"""Asset lifecycle and immutable, question-scoped observations."""
import hashlib
import io
import json
import shutil
import warnings
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

from app.config.settings import settings
from app.media.vision import Observation, PROMPT_VERSION, VisionClient, digest
from app.runtime.outbox import clock
from app.support.store import SupportError
from app.tasks.provenance import lock_input_user, revision


def asset(tx, user, aid, *, ready=True):
    row = tx.one('SELECT * FROM media_assets WHERE attachment_id=? AND user_id=?', (aid, user))
    if not row:
        raise SupportError('图片不存在', 404)
    if ready and (row['status'] != 'ready' or row['expires_at'] <= clock(tx)):
        raise SupportError('图片已撤回或过期', 410)
    return row


def public_asset(row):
    return {k: row[k] for k in ('attachment_id', 'origin_session_id', 'mime_type', 'byte_size',
                               'width', 'height', 'status', 'generation', 'expires_at')}


class MediaService:
    def __init__(self, store, *, root=None, vision=None):
        self.store = store
        self.root = Path(root or settings.media_storage_dir).resolve()
        self.vision = vision or VisionClient()

    def path(self, key):
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise SupportError('图片路径不可用', 404)
        return path

    def upload(self, user, sid, request_id, data):
        if not data or len(data) > settings.media_max_bytes:
            raise SupportError('图片大小超出限制', 413)
        sha = hashlib.sha256(data).hexdigest()
        with self.store.transaction(read_only=True) as tx:
            self.store._control(tx, sid, user)
        # Decode before persisting. SVG, animation and oversized pixel canvases
        # are not accepted; MIME and filenames supplied by clients are ignored.
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as source:
                    if source.format not in {'JPEG', 'PNG', 'WEBP'} or getattr(source, 'n_frames', 1) != 1:
                        raise ValueError('Unsupported format')
                    width, height = source.size
                    if width * height > settings.media_max_pixels:
                        raise ValueError('Too many pixels')
                    mime = Image.MIME[source.format]
                    source.load()
                    oriented = ImageOps.exif_transpose(source).convert('RGBA')
                    normal = Image.new('RGB', oriented.size, 'white')
                    normal.paste(oriented, mask=oriented.getchannel('A'))
                    normal.thumbnail((2048, 2048))
                    output = io.BytesIO()
                    normal.save(output, format='JPEG', quality=90)
                    model_data = output.getvalue()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
                Image.DecompressionBombWarning) as exc:
            raise SupportError('请上传有效的静态 JPEG、PNG 或 WebP 图片', 422) from exc
        aid = str(uuid4())
        directory = self.path(aid)
        try:
            with self.store.transaction() as tx:
                lock_input_user(tx, user)
                self.store._control(tx, sid, user)
                old = tx.one('SELECT * FROM media_assets WHERE user_id=? AND upload_request_id=?', (user, request_id))
                if old:
                    if old['sha256'] != sha or old['origin_session_id'] != sid:
                        raise SupportError('上传编号已用于不同图片')
                    return public_asset(old)
                count = tx.one("SELECT COUNT(*) AS n FROM media_assets WHERE user_id=? AND status='ready'", (user,))['n']
                if count >= settings.media_max_assets_per_user:
                    raise SupportError('图片数量已达上限，请删除不再需要的图片', 429)
                directory.mkdir(parents=True, exist_ok=False)
                (directory / 'original').write_bytes(data)
                (directory / 'model.jpg').write_bytes(model_data)
                variants = {'model': {'key': aid + '/model.jpg',
                                     'sha256': hashlib.sha256(model_data).hexdigest(), 'version': 1}}
                timestamp = clock(tx)
                tx.execute('''INSERT INTO media_assets
                    (attachment_id,user_id,origin_session_id,upload_request_id,storage_key,sha256,mime_type,
                     byte_size,width,height,variants,status,created_at,expires_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,'ready',?,?)''',
                    (aid, user, sid, request_id, aid + '/original', sha, mime, len(data), width, height,
                     json.dumps(variants), timestamp, timestamp + settings.media_retention_days * 86400))
                return public_asset(asset(tx, user, aid))
        except BaseException:
            if directory.exists():
                shutil.rmtree(directory)
            raise

    def metadata(self, user, aid):
        with self.store.transaction(read_only=True) as tx:
            row = asset(tx, user, aid, ready=False)
            result = public_asset(row)
            result['analyses'] = tx.all('''SELECT session_id,sequence,status,error_code FROM image_jobs
                WHERE user_id=? AND attachment_id=? ORDER BY sequence''', (user, aid))
            if row['status'] == 'ready' and row['expires_at'] <= clock(tx):
                result['status'] = 'expired'
            return result

    def content(self, user, aid, variant='preview'):
        with self.store.transaction(read_only=True) as tx:
            row = asset(tx, user, aid)
            key = row['storage_key'] if variant == 'original' else json.loads(row['variants'])['model']['key']
            mime = row['mime_type'] if variant == 'original' else 'image/jpeg'
        try:
            data = self.path(key).read_bytes()
        except OSError as exc:
            raise SupportError('图片文件不可用', 410) from exc
        with self.store.transaction(read_only=True) as tx:
            asset(tx, user, aid)
        return data, mime

    def revoke(self, user, aid):
        with self.store.transaction() as tx:
            lock_input_user(tx, user)
            row = asset(tx, user, aid, ready=False)
            if row['status'] in {'revoked', 'deleted'}:
                return
            revision(tx, user, lock=True)
            tx.execute("UPDATE media_assets SET status='revoked',generation=generation+1 WHERE attachment_id=?", (aid,))
            tx.execute("UPDATE image_jobs SET status='cancelled',error_code='revoked' WHERE attachment_id=?", (aid,))
            tx.execute("UPDATE image_observations SET status='invalidated' WHERE attachment_id=?", (aid,))
            tx.execute("UPDATE message_attachments SET status='revoked',version=version+1 WHERE attachment_id=?", (aid,))
            tx.execute("UPDATE task_attachments SET status='revoked',version=version+1 WHERE attachment_id=?", (aid,))
            # Existing execution/confirmation/reply fences already compare task
            # versions. Revoke under the same provenance -> task lock ordering.
            tasks = tx.all('SELECT DISTINCT task_id FROM task_attachments WHERE attachment_id=? ORDER BY task_id', (aid,))
            for task in tasks:
                tx.one('SELECT task_id FROM conversation_tasks WHERE task_id=?' + (' FOR UPDATE' if tx.postgres else ''),
                       (task['task_id'],))
                tx.execute('UPDATE conversation_tasks SET version=version+1 WHERE task_id=?', (task['task_id'],))
            tx.execute('UPDATE task_provenance_users SET revision=revision+1 WHERE user_id=?', (user,))
            control = self.store._control(tx, row['origin_session_id'], user)
            self.store._event(tx, control, 'attachment_revoked', user, {'attachment_id': aid})

    def cleanup(self):
        with self.store.transaction(read_only=True) as tx:
            rows = tx.all("SELECT * FROM media_assets WHERE status='revoked' OR (status='ready' AND expires_at<=?) LIMIT 100",
                          (clock(tx),))
        for row in rows:
            self.revoke(row['user_id'], row['attachment_id'])
            directory = self.path(row['attachment_id'])
            if directory.exists():
                shutil.rmtree(directory)
            with self.store.transaction() as tx:
                tx.execute("UPDATE media_assets SET status='deleted' WHERE attachment_id=? AND status='revoked'",
                           (row['attachment_id'],))
        # A process crash between file preparation and DB commit leaves no row.
        # UUID directories older than a day can be collected safely.
        import time
        from uuid import UUID
        if self.root.exists():
            for directory in self.root.iterdir():
                try:
                    UUID(directory.name)
                    if not directory.is_dir() or time.time() - directory.stat().st_mtime < 86400:
                        continue
                except (ValueError, OSError):
                    continue
                with self.store.transaction(read_only=True) as tx:
                    exists = tx.one('SELECT attachment_id FROM media_assets WHERE attachment_id=?', (directory.name,))
                if not exists:
                    shutil.rmtree(self.path(directory.name))

    def prepare(self, user, aid, question, detail, sources, *, task_id=None, purpose='initial'):
        with self.store.transaction(read_only=True) as tx:
            row = asset(tx, user, aid)
            if task_id:
                from app.media.context import require_task_asset
                require_task_asset(tx, user, task_id, aid)
            variant = json.loads(row['variants'])['model']
            snapshot = dict(attachment_id=aid, generation=row['generation'], user_id=user, task_id=task_id,
                            input_hash=variant['sha256'], purpose=purpose, detail=detail, question=question,
                            context_sources=sources, context_digest=digest(sources),
                            model_revision=self.vision.revision, prompt_version=PROMPT_VERSION)
            snapshot['reuse_key'] = digest(snapshot)
            cached = tx.one("SELECT * FROM image_observations WHERE reuse_key=? AND status='valid'", (snapshot['reuse_key'],))
        return snapshot, cached

    def recognize(self, snapshot, *, timeout):
        data, _ = self.content(snapshot['user_id'], snapshot['attachment_id'])
        if hashlib.sha256(data).hexdigest() != snapshot['input_hash']:
            raise SupportError('图片内容已变化')
        question = snapshot['question'] + '\n参考上下文（仅辅助定位，不代表图中事实）：' + json.dumps(
            snapshot['context_sources'], ensure_ascii=False)
        return Observation.model_validate(self.vision.observe(data, question, snapshot['detail'], timeout=timeout)).model_dump()

    def persist(self, tx, snapshot, result):
        row = asset(tx, snapshot['user_id'], snapshot['attachment_id'])
        if row['generation'] != snapshot['generation']:
            raise SupportError('图片来源已变化')
        oid = str(uuid4())
        tx.execute('''INSERT INTO image_observations
            (observation_id,attachment_id,generation,user_id,task_id,input_hash,purpose,detail,question,
             context_sources,context_digest,model_revision,prompt_version,result,reuse_key,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(reuse_key) DO NOTHING''',
            (oid, snapshot['attachment_id'], snapshot['generation'], snapshot['user_id'], snapshot['task_id'],
             snapshot['input_hash'], snapshot['purpose'], snapshot['detail'], snapshot['question'],
             json.dumps(snapshot['context_sources']), snapshot['context_digest'], snapshot['model_revision'],
             snapshot['prompt_version'], json.dumps(result, ensure_ascii=False), snapshot['reuse_key'], clock(tx)))
        return tx.one("SELECT * FROM image_observations WHERE reuse_key=? AND status='valid'", (snapshot['reuse_key'],))

    def inspect(self, user, task_id, aid, question, detail, *, before_call, check, timeout):
        from app.media.context import observation_view, inspection_sources
        with self.store.transaction(read_only=True) as tx:
            sources = inspection_sources(tx, user, task_id, aid)
        snapshot, cached = self.prepare(user, aid, question, detail, sources, task_id=task_id, purpose='inspect')
        if cached:
            check()
            return observation_view(cached)
        before_call()
        result = self.recognize(snapshot, timeout=timeout)
        check()
        with self.store.transaction() as tx:
            lock_input_user(tx, user)
            revision(tx, user, lock=True)
            tx.one('SELECT task_id FROM conversation_tasks WHERE task_id=? AND user_id=?' +
                   (' FOR UPDATE' if tx.postgres else ''), (task_id, user))
            if digest(inspection_sources(tx, user, task_id, aid)) != snapshot['context_digest']:
                raise SupportError('图片问题的上下文已变化')
            observation = self.persist(tx, snapshot, result)
        check()
        return observation_view(observation)
