# Offline migration to protocol 3

Migration reads an idle source and creates a separate new target. Normal startup accepts only the current schema; it never upgrades a database in place or selects an old execution path.

```console
oea migrate --source /path/to/old-state
oea migrate --source /path/to/old-state --target /path/to/new-state --apply
```

`migrate` recognizes legacy schema 29 and supported earlier protocol-2 and protocol-3 state revisions. For an `oea.sqlite` source, use `upgrade-state` to inspect the source and write a separate target.

```console
oea upgrade-state --data-dir /path/to/old-state
oea upgrade-state --data-dir /path/to/old-state --target /path/to/new-state --apply
```

Inspection is read-only. Applying requires an absent target outside source, Git and synchronized folders. Stop the original host and allow SQLite to checkpoint first. Active host locks and unresolved WAL/journal state are rejected; a lock file's existence alone is not proof of an active host. The converter never opens a second host on the source.

Protocol-2 schema revisions 1, 2 and 3 are recognized. The converter preserves IDs, configuration, secrets, usage, original messages/read facts, artifacts and state-owned material. File hashes, foreign keys, source stability, copied vault availability and historical usage totals are verified before the target is marked usable. A partial target remains available for diagnosis and cannot masquerade as a completed conversion.

The recognized earlier protocol-3 schema receives empty architecture tables. Its records, credentials, historical accounting and source files are retained; unresolved actions and costs remain for review. Earlier graph files are archived under `imported/protocol-3-initial`, and their runs carry an unsupported historical execution version instead of being resumed with new code. Existing memory and later evidence remain available by reference. This transition does not validate old code factories or process checkpoints as resumable.

The new tool, message and budget schemas are converted explicitly. Old total-input ceilings keep their original meaning. Historical cache, price or provider facts that were not recorded remain unknown. Legacy command permission becomes an explicit trusted configuration; old programs remain disabled until they implement the current SDK. Converted optional roles do not silently gain business capabilities.

Messages retain their exact audience through private imported boards. Old graph checkpoints are archived rather than loaded by the new runtime. Nonterminal work and unknown effects remain for review with their original execution version; they are not automatically replayed or resumed. Existing memory and subsequent durable evidence are retained by reference before new work can be accepted.

The source database and source files remain unchanged during conversion. External material and work-directory bindings are preserved, not silently moved. Keep the private conversion report with backups and runtime state, never in a public repository. After target verification, switch clients and launchers explicitly. Once new work has occurred, a rollback must account for those new facts rather than overwrite them with an old snapshot.

The SDK entries are `oea.migration.offline.migrate_legacy` and `oea.migration.upgrade.upgrade_state`. Historical legacy records are available through project-scoped `history.summary`, `history.list`, `history.get`, `history.conversation` and `history.usage`. Reading history does not execute it or grant source-tool permission.

## Agent canvas state upgrade

The Agent canvas release adds candidate ownership and deletion markers plus cached topology projections. Existing state must be stopped and converted with `oea upgrade-state` into a new external directory. The source is unchanged. This additive transition preserves architecture definitions, immutable version hashes, bindings, graph checkpoints and existing directory bindings. Old shared candidates have no Agent owner and remain available for copying. State schema revision 4 adds a nullable `handled_at` field while preserving the original Run state. Old tasks begin unhandled. Existing canvas-schema directories receive this additive change through the same offline copy workflow, with checkpoint and credential preservation checks.

## OEA 3.4.7 state upgrade

OEA 3.4.7 uses state schema revision 5 to record instance removal while retaining history. Use `oea upgrade-state` with the 3.4.7 CLI to convert a stopped revision 4 state directory into a new target. The conversion preserves existing Run records, graph checkpoints, workspace bindings and model secrets. It verifies managed artifact copies and rotates the target's local control credential. After switching to the target, run `oea open` to sign in again and reconnect existing MCP clients.
