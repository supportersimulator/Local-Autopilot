CREATE TABLE complexity_vectors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vector_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    signal_keywords TEXT NOT NULL,
                    risk_score REAL DEFAULT 5.0,
                    drift_ranking_score REAL DEFAULT 0.0,
                    current_alert_level TEXT DEFAULT 'none',
                    last_triggered_at TEXT,
                    trigger_count INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
INSERT INTO complexity_vectors VALUES(1,'V1','Tool vs Project Paradox','structural','["persistent_hook_structure", "self-referential", "webhook freeze", "tool maintaining itself"]',9.0,100.0,'none','2026-02-26T15:15:40.515054',2,'2026-02-24T22:46:31.363860','2026-02-26T15:15:40.515054');
INSERT INTO complexity_vectors VALUES(2,'V2','Lite vs Heavy Mode','operational','["lite mode", "heavy mode", "postgres", "sqlite fallback", "mode transition"]',7.0,56.0,'none',NULL,0,'2026-02-24T22:46:31.363860','2026-02-24T22:55:24.956258');
INSERT INTO complexity_vectors VALUES(3,'V3','Bidirectional Sync','structural','["sync conflict", "source of truth", "bidirectional", "merge conflict"]',6.0,40.0,'none','2026-02-26T15:15:40.515054',1,'2026-02-24T22:46:31.363860','2026-02-26T15:15:40.515054');
INSERT INTO complexity_vectors VALUES(4,'V4','Shallow vs Deep Memory','operational','["MEMORY.md", "evidence store", "contradicts", "stale memory", "trust hierarchy"]',6.0,35.0,'none','2026-02-24T22:59:02.250248',1,'2026-02-24T22:46:31.363860','2026-02-24T22:59:02.250248');
INSERT INTO complexity_vectors VALUES(5,'V5','Three LLMs / GPU Contention','resource','["GPU", "stampede", "starvation", "priority queue", "Metal", "gpu_lock", "concurrent"]',10.0,72.0,'none','2026-02-26T15:15:40.515054',2,'2026-02-24T22:46:31.363860','2026-02-26T15:15:40.515054');
INSERT INTO complexity_vectors VALUES(6,'V6','IDE Platform Fragmentation','operational','["cursor", "vscode", "IDE", "hooks", "cursorrules", "extension"]',6.0,30.0,'none',NULL,0,'2026-02-24T22:46:31.363860','2026-02-24T22:55:24.956258');
INSERT INTO complexity_vectors VALUES(7,'V7','Project Border Bleed','structural','["sub-project", "focus mode", "namespace", "context bleed", "wrong project"]',5.0,25.0,'none',NULL,0,'2026-02-24T22:46:31.363860','2026-02-24T22:55:24.956258');
INSERT INTO complexity_vectors VALUES(8,'V8','Atlas Context Window','operational','["context overflow", "compaction", "cannot see", "partial information", "truncated"]',7.0,45.0,'none',NULL,0,'2026-02-24T22:46:31.363860','2026-02-24T22:55:24.956258');
INSERT INTO complexity_vectors VALUES(9,'V9','Redundant Agents / Dead Code','structural','["watchdog", "dead code", "anatomical agents", "dual restore", "unused"]',7.0,56.0,'none','2026-02-26T15:15:40.515054',2,'2026-02-24T22:46:31.363860','2026-02-26T15:15:40.515054');
INSERT INTO complexity_vectors VALUES(10,'V10','Container Name Variants','identity','["context-dna", "contextdna", "acontext", "docker name", "container variant"]',5.0,20.0,'none',NULL,0,'2026-02-24T22:46:31.363860','2026-02-24T22:55:24.956258');
INSERT INTO complexity_vectors VALUES(11,'V11','API Domain Sprawl','operational','["api.ersimulator", "admin.contextdna", "wrong domain", "auth token", "domain mismatch"]',6.0,22.0,'none',NULL,0,'2026-02-24T22:46:31.363860','2026-02-24T22:55:24.956258');
INSERT INTO complexity_vectors VALUES(12,'V12','Action Fragmentation','structural','["direct HTTP", "port 5044", "bypass queue", "multiple paths", "direct call"]',7.0,38.0,'none','2026-02-26T15:15:40.515054',2,'2026-02-24T22:46:31.363860','2026-02-26T15:15:40.515054');
INSERT INTO complexity_vectors VALUES(13,'V13','Identity Alias Confusion','identity','["Synaptic butler", "neurologist", "Atlas navigator", "Cardiologist", "role confusion"]',6.0,30.0,'none',NULL,0,'2026-02-24T22:46:31.363860','2026-02-24T22:55:24.956258');
INSERT INTO complexity_vectors VALUES(14,'V14','Message Broker Complexity','operational','["Redis pub/sub", "broker", "message routing", "channel", "duplicate message"]',5.0,25.0,'none',NULL,0,'2026-02-24T22:46:31.363860','2026-02-24T22:55:24.956258');
INSERT INTO complexity_vectors VALUES(15,'V15','Scheduler/Runner/Daemon Proliferation','resource','["launchd", "nohup", "daemon", "scheduler proliferation", "cron overlap"]',7.0,40.0,'none',NULL,0,'2026-02-24T22:46:31.363860','2026-02-24T22:55:24.956258');
INSERT INTO complexity_vectors VALUES(16,'ES','Error Swallowing','structural','["bare except", "except:", "except Exception: pass", "silent fail", "swallow error"]',9.5,85.0,'none',NULL,0,'2026-02-24T22:46:31.363860','2026-02-24T22:55:24.956258');
INSERT INTO complexity_vectors VALUES(17,'TSD','Temporal State Drift','operational','["stale cache", "anticipation expired", "pre-compute", "TTL", "cache invalidation"]',7.5,60.0,'none',NULL,0,'2026-02-24T22:46:31.363860','2026-02-24T22:55:24.956258');
INSERT INTO complexity_vectors VALUES(18,'FLC','Feedback Loop Contamination','structural','["false positive", "amplify error", "cold-start", "promoted wrong", "evidence contamination"]',7.5,60.0,'none',NULL,0,'2026-02-24T22:46:31.363860','2026-02-24T22:55:24.956258');
INSERT INTO complexity_vectors VALUES(19,'PVS','Python Version Skew','operational','["python3 wrong", "python 3.9", "python 3.14", "xcode python", "version mismatch"]',7.5,55.0,'none',NULL,0,'2026-02-24T22:46:31.363860','2026-02-24T22:55:24.956258');
INSERT INTO complexity_vectors VALUES(20,'SCC','SQLite Connection Chaos','resource','["raw sqlite3.connect", "WAL missing", "220 call sites", "FD leak", "db_utils"]',7.0,55.0,'none',NULL,0,'2026-02-24T22:46:31.363860','2026-02-24T22:55:24.956258');
