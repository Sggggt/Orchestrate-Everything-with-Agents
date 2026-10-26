# Offline migration

The offline converter reads legacy schema 29 and creates a separate version 2 state
directory. Stop the old host and checkpoint SQLite first. Active host locks and
nonempty WAL or rollback journals are rejected. Existing lock files are inspected
read-only; their existence alone does not mean a host is running.
The source, its original artifacts, and its external work directories remain in place.

```console
oea migrate --source /path/to/legacy-state
oea migrate --source /path/to/legacy-state --target /path/to/new-state --apply
```

Inspection is the default and writes no files. Applying requires a target directory
that does not exist, outside Git, source, and synchronized directories. An incomplete
target is retained for inspection and cannot be opened as completed migrated state.
Blocking reconciliation issues retain the archived facts and `migration-report.json`
without a protocol marker. Choose another new target after correcting a failed conversion.

Projects, sessions, Agent definitions, instances, registered Skills, MCP configurations,
messages, and verified artifacts retain their original IDs where their ownership and
configuration can be mapped. Closed sessions are archived. Main and manager model
settings and vault references remain separate. Unsupported legacy programs and
environment capabilities require review before enabling their converted definition.
Imported artifacts have `origin=import` and no fabricated Run or instance owner.
Instance workspace bindings retain the exact original thread directory, or the latest
recorded task directory when no thread directory exists. Old environment objects are
not interpreted as new environment-variable grants. Original directory paths and
availability are listed in the private report; no external workspace is copied or moved.

Every original table row, table definition, row identity, and content hash is archived
in the new database. The complete legacy database and state-owned files are copied
under `imported/`; the encrypted vault is also retained in the new vault location.
The conversion report records table counts, mapped counts, token usage, file and
artifact hashes, secret availability, directory availability, and items needing review.
State-owned material is checked for additions, removals, or changes before the target
is published. When cumulative budget rows exist, their model/tool call and token totals
must match the original per-budget ledger; mismatches block publication. The report
keeps reservation and settlement totals separate, and copied vault contents are checked
for continued availability. Store this report with private runtime state rather than
in a source repository.

Old tasks, graph runs, checkpoints, tool receipts, usage ledgers, delivery facts, and
exact read state remain historical facts. Nonterminal work and unknown external
actions are marked `needs_review`. Legacy workflow definitions also require review
before an equivalent LangGraph graph can be registered. The converter creates no execution Runs and does
not automatically replay, resume, reconnect, or call a model. Imported messages have
no active delivery, so they cannot wake an instance. A v2 cursor is not inferred from
potentially non-contiguous old read receipts.

`oea.migration.offline.migrate_legacy(source, target=None, apply=False)` is the
synchronous SDK entry. It returns a JSON-compatible report and uses `RuntimeFault`
for unsupported sources, occupied targets, changing sources, and blocking issues.

`oea.migration.history.ImportedHistory(host)` exposes asynchronous `summary`, `list`,
`get`, `conversation`, and `usage` methods. Each requires `project_id`; record listing
and transcript reads use bounded pages. `get` returns the complete archived row,
including tagged Base64 values for SQLite blobs. These historical views are separate
from conversation/context tables used to build new model inputs. Reading history
does not execute it or grant a new Run access to its source files.

The Host registers `history.summary`, `history.list`, `history.get`,
`history.conversation`, and `history.usage` as read-only operations shared by SDK,
HTTP, and control MCP. `list` pages forward by archive position and `conversation`
pages backward with `before`; both return the next `cursor`, or `null` at the end.
Usage retains ledger groups by kind and state and exposes original cumulative budget
totals separately. Records without a project owner remain in the private source
snapshot and archive; a project-scoped history read does not expose them to another project.

## 显式 v2 状态升级

状态修订 2 增加 instance_read_positions，并将已识别的旧消息来源角色规范为 leader / instance。协议仍为 2.0。启动校验只接受当前 Schema，启动过程不自动升级旧库。

在已有宿主停止后先运行 `oea upgrade-state --data-dir EXTERNAL_STATE_DIRECTORY` 检查。执行时使用 `oea upgrade-state --data-dir EXTERNAL_STATE_DIRECTORY --backup-file EXTERNAL_BACKUP_FILE --apply`。升级工具只接受已声明的来源 Schema 校验值，获取原宿主排他锁，创建一个不存在的新数据库备份，然后在单事务中升级。旧游标、消息正文和对象 ID 保留；实例游标初始为空，不能推定历史已读。未知 Schema、活动宿主或已存在的备份目标都会拒绝。

此命令不迁移数据目录，不重置任务、产物或凭据。回滚须先停止宿主并核对升级后的新增事实，不能用旧备份抹去后续结果。
