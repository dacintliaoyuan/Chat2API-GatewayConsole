BEGIN TRANSACTION;
CREATE TABLE configgen (
                    schema_version INTEGER NOT NULL,
                    config_generation INTEGER NOT NULL,
                    applied_generation INTEGER NOT NULL DEFAULT 0
                );
INSERT INTO "configgen" VALUES(3,8,6);
CREATE TABLE keys (
                    key_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    secret_hash TEXT NOT NULL,
                    secret_version INTEGER NOT NULL DEFAULT 1,
                    masked_key TEXT NOT NULL,
                    secret_blob BLOB,
                    secret_algo TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    max_concurrency INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT,
                    row_version INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(name, deleted_at)
                );
INSERT INTO "keys" VALUES('00000000-0000-0000-0000-000000000001','alpha-renamed','ba5260459f41020f5621b3c5ac6b5dfd310f29997170b62a76b623685eab8ddb',1,'um-share-****cdef',X'01010101010101010101010102AEC84600E41A03F9350B6870067FE8FA5CBCEFA4B521E4CABA658FF96C9813D24A2FD8A06CDF82244D5A1A5A1DCA','aesgcm-file-key-v1',1,8,'2026-11-16T00:00:01.000000Z','2026-11-16T00:00:08.000000Z',NULL,3);
INSERT INTO "keys" VALUES('00000000-0000-0000-0000-000000000004','beta','3bff4f7e9ca92853219e79a8cb448a3f2836e79ac5a9a243673f22b2e6ec3701',2,'um-share-****wxyz',NULL,NULL,0,16,'2026-11-16T00:00:04.000000Z','2026-11-16T00:00:15.000000Z','2026-11-16T00:00:15.000000Z',3);
INSERT INTO "keys" VALUES('legacy-key','legacy','1b387faeb510b3968accf441f00dac7401a7e51fade44ce474b18e8e6faf79db',1,'um-share-****cdef',NULL,NULL,1,1,'2026-11-16T00:00:10.000000Z','2026-11-16T00:00:10.000000Z',NULL,1);
CREATE TABLE meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
INSERT INTO "meta" VALUES('schema_version','3');
INSERT INTO "meta" VALUES('share_max_concurrency','32');
CREATE TABLE ops (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    idempotency_key TEXT,
                    status TEXT NOT NULL,
                    payload TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(idempotency_key)
                );
INSERT INTO "ops" VALUES('00000000-0000-0000-0000-000000000002','publish',NULL,'pending','{"target_generation": 2, "revoking": false, "action_kind": "create", "key_id": "00000000-0000-0000-0000-000000000001"}','2026-11-16T00:00:02.000000Z');
INSERT INTO "ops" VALUES('00000000-0000-0000-0000-000000000003','create','create-alpha','ok','{"key_id": "00000000-0000-0000-0000-000000000001", "name": "alpha"}','2026-11-16T00:00:03.000000Z');
INSERT INTO "ops" VALUES('00000000-0000-0000-0000-000000000005','publish',NULL,'pending','{"target_generation": 3, "revoking": false, "action_kind": "create", "key_id": "00000000-0000-0000-0000-000000000004"}','2026-11-16T00:00:05.000000Z');
INSERT INTO "ops" VALUES('00000000-0000-0000-0000-000000000006','publish',NULL,'pending','{"target_generation": 4, "revoking": false, "action_kind": "rename", "key_id": "00000000-0000-0000-0000-000000000001"}','2026-11-16T00:00:07.000000Z');
INSERT INTO "ops" VALUES('00000000-0000-0000-0000-000000000008','publish',NULL,'pending','{"target_generation": 5, "revoking": true, "action_kind": "update", "key_id": "00000000-0000-0000-0000-000000000001"}','2026-11-16T00:00:10.000000Z');
INSERT INTO "ops" VALUES('00000000-0000-0000-0000-00000000000a','publish',NULL,'pending','{"target_generation": 6, "revoking": true, "action_kind": "rotate", "key_id": "00000000-0000-0000-0000-000000000004"}','2026-11-16T00:00:13.000000Z');
INSERT INTO "ops" VALUES('00000000-0000-0000-0000-00000000000b','rotate','rotate-beta','ok','{"key_id": "00000000-0000-0000-0000-000000000004", "secret_version": 2}','2026-11-16T00:00:14.000000Z');
INSERT INTO "ops" VALUES('00000000-0000-0000-0000-00000000000d','publish',NULL,'pending','{"target_generation": 7, "revoking": true, "action_kind": "delete", "key_id": "00000000-0000-0000-0000-000000000004"}','2026-11-16T00:00:17.000000Z');
INSERT INTO "ops" VALUES('00000000-0000-0000-0000-00000000000f','publish',NULL,'pending','{"target_generation": 8, "revoking": true, "action_kind": "set_share_limits", "key_id": null}','2026-11-16T00:00:19.000000Z');
INSERT INTO "ops" VALUES('00000000-0000-0000-0000-000000000010','set_share_limits','limits-1','ok','{"share_max_concurrency": 32}','2026-11-16T00:00:20.000000Z');
INSERT INTO "ops" VALUES('00000000-0000-0000-0000-000000000011','gateway_start','op-1','ok','{"source": "fixture"}','2026-11-16T00:00:22.000000Z');
CREATE TABLE security (
                    record_id TEXT PRIMARY KEY,
                    key_id TEXT,
                    generation INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    secret_version INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    recorded_at TEXT NOT NULL,
                    applied_at TEXT,
                    applied_generation INTEGER,
                    instance_id TEXT,
                    remote_confirmed_at TEXT,
                    remote_receipt TEXT
                );
INSERT INTO "security" VALUES('00000000-0000-0000-0000-000000000007','00000000-0000-0000-0000-000000000001',5,'limit_lower',1,'applied','2026-11-16T00:00:09.000000Z','2026-11-16T00:00:21.000000Z',5,'instance-a',NULL,NULL);
INSERT INTO "security" VALUES('00000000-0000-0000-0000-000000000009','00000000-0000-0000-0000-000000000004',6,'rotate',2,'pending','2026-11-16T00:00:11.000000Z',NULL,NULL,NULL,NULL,NULL);
INSERT INTO "security" VALUES('00000000-0000-0000-0000-00000000000c','00000000-0000-0000-0000-000000000004',7,'delete',2,'pending','2026-11-16T00:00:16.000000Z',NULL,NULL,NULL,NULL,NULL);
INSERT INTO "security" VALUES('00000000-0000-0000-0000-00000000000e',NULL,8,'limit_lower',NULL,'pending','2026-11-16T00:00:18.000000Z',NULL,NULL,NULL,NULL,NULL);
CREATE INDEX idx_security_generation
                    ON security(generation);
CREATE UNIQUE INDEX idx_keys_active_name
                    ON keys(name) WHERE deleted_at IS NULL;
COMMIT;
