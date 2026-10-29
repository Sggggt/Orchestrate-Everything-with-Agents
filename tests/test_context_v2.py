"""Bounded projections retain original evidence and incremental memory."""

import json
import re
import tempfile
import unittest
from pathlib import Path

from oea.contracts.configuration import AgentDefinition, ContextPolicy, Model
from oea.context.store import ContextStore, estimated_tokens, message_groups
from oea.contracts.errors import RuntimeFault
from oea.storage.persistence import Store, encode, one, rows
from oea.context.views import deduplicate_skills


class ContextTests(unittest.IsolatedAsyncioTestCase):
    def test_skill_body_deduplication_keeps_pairing_and_preserves_distinct_versions(self):
        messages = []
        for index, reference in enumerate(("source-one", "source-one", "source-two")):
            identity = str(index)
            messages.extend([{"role": "assistant", "tool_calls": [{"id": identity, "function": {"name": "read_skill"}}]},
                             {"role": "tool", "tool_call_id": identity, "content": encode({"reference": reference, "text": "BODY", "path": "SKILL.md"})}])
        projected = deduplicate_skills(messages)
        message_groups(projected)
        self.assertEqual(sum("BODY" in message.get("content", "") for message in projected), 2)
        self.assertEqual(sum("BODY" in message.get("content", "") for message in messages), 3)
        self.assertIn("already_in_context", projected[3]["content"])
        self.assertIn("BODY", deduplicate_skills(messages[2:4])[1]["content"])

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-v2-context-")
        self.store = Store(Path(self.temporary.name))
        await self.store.open()
        self.definition = AgentDefinition(id="worker", name="Worker")
        def seed(db):
            db.execute("INSERT INTO projects VALUES('p','Project',NULL,0,1)")
            db.execute("INSERT INTO agent_definitions VALUES('worker','p',?,1,1)", (self.definition.model_dump_json(),))
            db.execute("INSERT INTO sessions(id,project_id,name,created_at) VALUES('s','p','Session',1)")
            db.execute("INSERT INTO budgets(id,project_id,root_run_id,policy_json,started_at,created_at) VALUES('b','p','r','{}',1,1)")
            for instance, run in (("i", "r"), ("j", "other")):
                db.execute("INSERT INTO instances VALUES(?,'p','s','worker',?,'main','{}','{}',?,1)", (instance, instance, run))
                db.execute("INSERT INTO runs(id,instance_id,root_run_id,budget_id,state,input_json,idempotency_key,input_hash,model_role,created_at,updated_at) VALUES(?,?,?,'b','running',?,'key','hash','main',1,1)",
                           (run, instance, run, encode("Read the sources and deliver the confirmed answer")))
            return one(db, "SELECT * FROM runs WHERE id='r'"), one(db, "SELECT * FROM instances WHERE id='i'")
        self.run, self.instance = await self.store.call(seed, transaction=True)
        self.contexts = ContextStore(self.store)

    async def asyncTearDown(self):
        await self.store.close()
        self.temporary.cleanup()

    async def append_messages(self, messages, *, prefix="history"):
        def save(db):
            for index, message in enumerate(messages):
                db.execute("INSERT INTO conversation(id,instance_id,run_id,role,body_json,created_at) VALUES(?,'i','r',?,?,1)",
                           (prefix + "-" + str(index), message["role"], encode(message)))
        await self.store.call(save, transaction=True)

    def high_input_definition(self):
        return self.definition.model_copy(update={"context": ContextPolicy(
            input_tokens=1048576, memory_tokens=131072, recent_turns=100, pressure=0.9999999999999999)})

    async def test_high_input_pages_oldest_history_and_preserves_originals_and_memory_chain(self):
        definition = self.high_input_definition()
        await self.contexts.input(self.run, self.instance)
        await self.append_messages({"role": "user", "content": "FACT=" + str(index)} for index in range(900))
        original = await self.store.call(lambda db: rows(db, "SELECT * FROM conversation ORDER BY position"))
        archived, requests = [], []

        async def summarize(messages, limit):
            payload = json.loads(messages[1]["content"])
            message_groups(payload["new_history"])
            self.assertLess(estimated_tokens(messages), definition.context.input_tokens)
            self.assertEqual(limit, self.definition.model.max_output_tokens)
            requests.append(payload)
            archived.extend(payload["new_history"])
            return "Confirmed through " + archived[-1]["content"]

        view, projection = await self.contexts.project(self.run, self.instance, definition, "Review", [], summarize=summarize)
        self.assertGreater(len(requests), 1)
        self.assertEqual([message["content"] for message in archived], ["FACT=" + str(index) for index in range(len(archived))])
        self.assertEqual([message["content"] for message in view[3:]], ["FACT=" + str(index) for index in range(len(archived), 900)])
        self.assertGreaterEqual(len(view[3:]), definition.context.recent_turns)
        self.assertLessEqual(len(view[3:]), 256)
        self.assertLess(projection["input_tokens"], definition.context.input_tokens)
        memories = await self.store.call(lambda db: rows(db, "SELECT * FROM memories ORDER BY rowid"))
        through = 1  # The fixed current input is excluded from historical deltas.
        for index, memory in enumerate(memories):
            references = json.loads(memory["source_ids_json"])
            if index:
                self.assertEqual(references.pop(0), "memory:" + memories[index - 1]["id"])
                self.assertEqual(requests[index]["previous_memory"], memories[index - 1]["body"])
            expected = ["message:" + row["id"] for row in original if through < row["position"] <= memory["through_position"]]
            self.assertEqual(references, expected)
            self.assertGreater(memory["through_position"], through)
            through = memory["through_position"]
        self.assertEqual(original, await self.store.call(lambda db: rows(db, "SELECT * FROM conversation ORDER BY position")))

        async def unexpected(messages, limit):
            raise AssertionError("A resumed bounded projection must reuse persisted memory")

        resumed, resumed_projection = await ContextStore(self.store).project(
            self.run, self.instance, definition, "Review", [], summarize=unexpected)
        self.assertEqual(resumed, view)
        self.assertEqual(resumed_projection["memory_ref"], projection["memory_ref"])

    async def test_tool_region_crossing_a_row_page_is_never_split(self):
        await self.append_messages({"role": "user", "content": "Earlier " + str(index)} for index in range(255))
        calls = [{"id": "call-" + str(index), "function": {"name": "read_source", "arguments": "{}"}} for index in range(2)]
        region = [{"role": "assistant", "content": None, "tool_calls": calls},
                  *({"role": "tool", "tool_call_id": call["id"], "content": "Evidence"} for call in calls),
                  {"role": "user", "content": "Continue with both sources"}]
        await self.append_messages(region, prefix="region")
        archived = []

        async def summarize(messages, limit):
            delta = json.loads(messages[1]["content"])["new_history"]
            message_groups(delta)
            archived.extend(delta)
            return "Earlier sources remain available"

        view, _ = await self.contexts.project(self.run, self.instance, self.high_input_definition(), "Review", [], summarize=summarize)
        self.assertEqual(len(archived), 255)
        self.assertEqual(view[3:], region)
        message_groups(view)

    async def test_one_complete_tool_region_can_span_several_bounded_pages(self):
        calls = [{"id": "call-" + str(index), "function": {"name": "read_source", "arguments": "{}"}} for index in range(600)]
        region = [{"role": "assistant", "content": None, "tool_calls": calls},
                  *({"role": "tool", "tool_call_id": call["id"], "content": "Evidence " + str(index)} for index, call in enumerate(calls))]
        await self.append_messages([*region, {"role": "user", "content": "Use the complete evidence"}])
        requests = []

        async def summarize(messages, limit):
            delta = json.loads(messages[1]["content"])["new_history"]
            self.assertEqual(delta, region)
            self.assertEqual(len(message_groups(delta)), 1)
            requests.append(delta)
            return "All completed calls are available in original history"

        view, _ = await self.contexts.project(self.run, self.instance, self.high_input_definition(), "Review", [], summarize=summarize)
        self.assertEqual(len(requests), 1)
        self.assertEqual(view[3:], [{"role": "user", "content": "Use the complete evidence"}])
        memory = await self.store.call(lambda db: one(db, "SELECT * FROM memories"))
        self.assertEqual(memory["through_position"], len(region))
        original, cursor = [], 0
        while cursor is not None:
            page = await self.contexts.history("i", after=cursor, limit=100)
            original.extend(item["message"] for item in page["items"])
            cursor = page["cursor"]
        self.assertEqual(original, [*region, view[-1]])

    async def test_projection_uses_current_secret_filter_before_shared_estimation(self):
        await self.append_messages([{"role": "user", "content": "A later-secret value was recorded"}])
        def redact(value):
            return json.loads(json.dumps(value).replace("later-secret", "[redacted]"))
        async def unexpected(messages, limit):
            raise AssertionError("Small input needs no summary")
        view, projection = await ContextStore(self.store, redact=redact).project(self.run, self.instance, self.definition, "Review", [], summarize=unexpected)
        self.assertNotIn("later-secret", encode(view))
        self.assertEqual(projection["input_tokens"], estimated_tokens(view) + estimated_tokens([]))
        original = await self.contexts.history(self.instance["id"])
        self.assertIn("later-secret", original["items"][0]["message"]["content"])

    async def test_summary_partition_uses_its_own_output_reserve(self):
        definition = self.definition.model_copy(update={"model": Model(context_window_tokens=7000, max_output_tokens=5000),
            "context": ContextPolicy(memory_tokens=256)})
        await self.append_messages({"role": "user", "content": "saved evidence " * 210} for _ in range(20))
        calls, reservations = [], []
        async def reserve(requested):
            reservations.append(requested)
            return 64
        async def summarize(messages, limit):
            self.assertEqual(limit, 64)
            self.assertLessEqual(estimated_tokens(messages), 7000 - 64 - 1024)
            calls.append(messages)
            return "Saved original references and facts."
        view, projection = await self.contexts.project(self.run, self.instance, definition, "Review", [], summarize=summarize, output_tokens=5000, summary_reserve=reserve)
        self.assertTrue(calls)
        self.assertEqual(reservations, [256] * len(calls))
        self.assertLessEqual(projection["input_tokens"], 7000 - 5000 - 1024)
        message_groups(view)

    async def test_latest_large_paired_region_is_protected_from_prefix_summary(self):
        calls = [{"id": "call-" + str(index), "function": {"name": "read_source", "arguments": "{}"}} for index in range(300)]
        region = [{"role": "assistant", "content": None, "tool_calls": calls},
                  *({"role": "tool", "tool_call_id": call["id"], "content": "Evidence"} for call in calls)]
        await self.append_messages(region)
        async def unexpected(messages, limit):
            raise AssertionError("The latest atomic group must be retained")
        view, _ = await self.contexts.project(self.run, self.instance, self.high_input_definition(), "Review", [], summarize=unexpected)
        self.assertEqual(view[2:], region)
        self.assertEqual(await self.store.call(lambda db: rows(db, "SELECT * FROM memories")), [])

    async def test_single_tool_region_exceeding_summary_budget_reports_the_real_boundary(self):
        calls = [{"id": "call-" + str(index), "function": {"name": "read_source", "arguments": "{}"}} for index in range(300)]
        await self.append_messages([{"role": "assistant", "content": None, "tool_calls": calls},
                                    *({"role": "tool", "tool_call_id": call["id"], "content": "Evidence"} for call in calls)])
        definition = self.definition.model_copy(update={"context": ContextPolicy(input_tokens=4096, memory_tokens=512)})

        async def unexpected(messages, limit):
            raise AssertionError("An incomplete or over-budget group cannot be summarized")

        with self.assertRaises(RuntimeFault) as caught:
            await self.contexts.project(self.run, self.instance, definition, "Review", [], summarize=unexpected)
        self.assertEqual(caught.exception.code, "MODEL_CONTEXT_BUDGET")
        self.assertEqual(await self.store.call(lambda db: rows(db, "SELECT * FROM memories")), [])
        self.assertEqual(await self.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM conversation")), {"count": 301})

    async def test_history_pages_also_split_summary_requests_at_the_token_budget(self):
        await self.append_messages({"role": "user", "content": "FACT=" + str(index) + " evidence" * 40} for index in range(300))
        definition = self.definition.model_copy(update={"context": ContextPolicy(input_tokens=4096, memory_tokens=512)})
        archived, count = [], 0

        async def summarize(messages, limit):
            nonlocal count
            self.assertLess(estimated_tokens(messages), definition.context.input_tokens)
            count += 1
            archived.extend(json.loads(messages[1]["content"])["new_history"])
            return "Confirmed evidence through " + archived[-1]["content"].split()[0]

        view, projection = await self.contexts.project(self.run, self.instance, definition, "Review", [], summarize=summarize)
        self.assertGreater(count, 2)
        self.assertLessEqual(projection["input_tokens"], definition.context.input_tokens)
        complete = list(archived)
        for message in view[3:]:
            if message["content"].startswith('{"content_view":'):
                reference = json.loads(message["content"])["content_view"]["source_ref"]
                page = await self.contexts.history(self.instance["id"], after=reference["after"], limit=1)
                self.assertEqual(page["items"][0]["id"], reference["id"])
                message = page["items"][0]["message"]
            complete.append(message)
        self.assertEqual([int(message["content"].split()[0][5:]) for message in complete], list(range(300)))
        self.assertEqual(await self.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM conversation")), {"count": 300})

    async def test_later_page_failure_keeps_the_successful_prefix_for_resume(self):
        await self.store.call(lambda db: db.execute("INSERT INTO memories VALUES('prior','i','r',0,'[]','Prior confirmed fact',1)"), transaction=True)
        await self.append_messages({"role": "user", "content": "FACT=" + str(index)} for index in range(700))
        definition = self.high_input_definition()
        successful = []

        async def fail_second(messages, limit):
            if successful:
                raise RuntimeFault("MODEL_TIMEOUT", "Summary timed out")
            successful.extend(json.loads(messages[1]["content"])["new_history"])
            return "First page confirmed"

        with self.assertRaises(RuntimeFault) as caught:
            await self.contexts.project(self.run, self.instance, definition, "Review", [], summarize=fail_second)
        self.assertEqual(caught.exception.code, "CONTEXT_SUMMARY_FAILED")
        memory = await self.store.call(lambda db: one(db, "SELECT * FROM memories ORDER BY rowid DESC LIMIT 1"))
        self.assertEqual(memory["through_position"], len(successful))
        self.assertEqual(caught.exception.details["memory_ref"], memory["id"])
        prior = await self.store.call(lambda db: one(db, "SELECT * FROM memories WHERE id='prior'"))
        self.assertEqual(prior["body"], "Prior confirmed fact")
        resumed = []

        async def summarize(messages, limit):
            resumed.extend(json.loads(messages[1]["content"])["new_history"])
            return "Further evidence confirmed"

        view, _ = await ContextStore(self.store).project(self.run, self.instance, definition, "Review", [], summarize=summarize)
        self.assertEqual([int(message["content"][5:]) for message in successful + resumed + view[3:]], list(range(700)))
        self.assertEqual(await self.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM conversation")), {"count": 700})

    async def test_summary_input_is_stable_if_memory_commit_fails_after_the_receipt(self):
        await self.append_messages({"role": "user", "content": "FACT=" + str(index)} for index in range(257))
        definition = self.high_input_definition()
        receipts, requested = {}, []

        async def summarize(messages, limit):
            key = encode(messages)
            requested.append(key)
            if key not in receipts:
                receipts[key] = "Confirmed prefix"
            return receipts[key]

        class InterruptedStore:
            async def call(inner, function, **options):
                if options.get("transaction"):
                    raise OSError("Interrupted before memory commit")
                return await self.store.call(function, **options)

        with self.assertRaises(OSError):
            await ContextStore(InterruptedStore()).project(self.run, self.instance, definition, "Review", [], summarize=summarize)
        self.assertEqual(await self.store.call(lambda db: rows(db, "SELECT * FROM memories")), [])
        await self.contexts.project(self.run, self.instance, definition, "Review", [], summarize=summarize)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(len(requested), 2)
        self.assertEqual(requested[0], requested[1])

    async def test_small_projection_uses_one_indexed_bounded_history_read(self):
        await self.append_messages({"role": "user", "content": "FACT=" + str(index)} for index in range(12))
        queries, calls = [], []

        class ObservedStore:
            async def call(inner, function, **options):
                calls.append(function)
                def measured(db):
                    def trace(cursor, statement, bindings):
                        if "FROM conversation c" in statement:
                            queries.append((statement, bindings))
                        return True
                    db.set_exec_trace(trace)
                    try:
                        return function(db)
                    finally:
                        db.set_exec_trace(None)
                return await self.store.call(measured, **options)

        async def unexpected(messages, limit):
            raise AssertionError("A small projection needs no summary")

        view, _ = await ContextStore(ObservedStore()).project(self.run, self.instance, self.definition, "Review", [], summarize=unexpected)
        self.assertEqual(len(view), 14)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(queries), 1)
        query, parameters = queries[0]
        self.assertIn("ORDER BY c.position LIMIT 257", query)
        plan = await self.store.call(lambda db: rows(db, "EXPLAIN QUERY PLAN " + query, parameters))
        self.assertTrue(any("conversation_instance" in step["detail"] for step in plan))
        self.assertFalse(any("TEMP B-TREE" in step["detail"] for step in plan))

    async def test_eighty_rounds_keep_facts_pairs_and_bounded_active_input(self):
        summary_cost, model_cost, summaries = 0, 0, 0

        async def summarize(messages, limit):
            nonlocal summary_cost, summaries
            summaries += 1
            facts = re.findall(r"KNOWN_FACT=(\d+)", encode(messages))
            self.assertTrue(facts)
            text = "Confirmed KNOWN_FACT=" + facts[0] + ". Keep source references, completed reads, unresolved work, and the delivery goal."
            summary_cost += estimated_tokens(messages) + estimated_tokens(text)
            return text

        for index in range(80):
            call = {"id": "call-" + str(index), "type": "function", "function": {"name": "read_source", "arguments": "{}"}}
            await self.contexts.append(self.run, self.instance, {"role": "assistant", "content": None, "tool_calls": [call]}, "model-" + str(index))
            text = ("KNOWN_FACT=37\n" if index == 0 else "") + "SOURCE=" + str(index) + "\n" + "bounded evidence " * 650
            await self.contexts.tool_result(self.run, self.instance, self.definition.context, call, {"text": text})
            view, plan = await self.contexts.project(self.run, self.instance, self.definition, "Review evidence; preserve original sources.", [], summarize=summarize)
            model_cost += plan["input_tokens"]
            self.assertLessEqual(plan["input_tokens"], self.definition.context.input_tokens)
            message_groups(view)
        self.assertEqual(summaries, 0, "Readable minimum views fit without a summary call")
        self.assertGreater(plan["selection"]["reference_groups"], 0)
        # A fixed ceiling makes a regression visible without embedding private
        # benchmark output or using model-network calls in a unit test.
        self.assertLess(model_cost + summary_cost, 2_700_000)
        original = await self.store.call(lambda db: one(db, "SELECT * FROM context_sources ORDER BY rowid LIMIT 1"))
        self.assertIn("KNOWN_FACT=37", original["body"])
        self.assertTrue(original["body"].endswith('"}'))

    async def test_tool_source_can_reference_the_original_action_without_a_body_copy(self):
        value = {"text": "evidence " * 1500}
        def save(db):
            db.execute("INSERT INTO actions(id,run_id,operation_key,name,input_hash,input_json,side_effects,state,result_json,error_json,created_at,updated_at) VALUES('call','r','original','read_source','hash','{}','none','completed',?,NULL,1,1)", (encode(value),))
        await self.store.call(save, transaction=True)
        call = {"id": "call", "function": {"name": "read_source"}}
        await self.contexts.tool_result(self.run, self.instance, self.definition.context, call, value, action_key="original")
        source = await self.store.call(lambda db: one(db, "SELECT * FROM context_sources"))
        self.assertIsNone(source["body"])
        self.assertEqual(source["action_id"], "call")
        original = await self.contexts.read(self.run, self.instance, source["id"], limit=16384)
        self.assertEqual(original["text"], encode(value))

    async def test_summary_failure_leaves_previous_memory_and_sources_intact(self):
        await self.store.call(lambda db: db.execute("INSERT INTO memories VALUES('memory','i','r',0,'[]','Confirmed fact remains',1)"), transaction=True)
        for index in range(260):
            await self.contexts.append(self.run, self.instance, {"role": "user", "content": "context " * 900}, "large-" + str(index))

        async def fail(messages, limit):
            raise RuntimeFault("MODEL_TIMEOUT", "Summary timed out")

        with self.assertRaises(RuntimeFault) as caught:
            await self.contexts.project(self.run, self.instance, self.definition, "Review", [], summarize=fail)
        self.assertEqual(caught.exception.code, "CONTEXT_SUMMARY_FAILED")
        memory = await self.store.call(lambda db: one(db, "SELECT * FROM memories WHERE id='memory'"))
        self.assertEqual(memory["body"], "Confirmed fact remains")
        self.assertEqual(len(await self.store.call(lambda db: rows(db, "SELECT * FROM conversation"))), 260)

    async def test_current_tool_revocation_blocks_reading_its_old_source(self):
        reference = await self.contexts.source(self.run, self.instance, "private evidence", "tool:read_source")

        async def revoked(run_id, name):
            raise RuntimeFault("PERMISSION_DENIED", "Tool access was revoked")

        with self.assertRaises(RuntimeFault):
            await self.contexts.read(self.run, self.instance, reference, authorize=revoked)
        other = {**self.instance, "id": "j"}
        with self.assertRaises(RuntimeFault):
            await self.contexts.read(self.run, other, reference)

    async def test_latest_memory_uses_insert_order_without_scanning_old_memories(self):
        async def summarize(messages, limit):
            raise AssertionError("A fixed active projection needs no summary")
        async def measured_projection():
            counts = []
            store = self.store
            class ObservedStore:
                async def call(self, function, **options):
                    def measured(db):
                        steps = 0
                        def progress():
                            nonlocal steps
                            steps += 1
                            return False
                        db.set_progress_handler(progress, 1)
                        try:
                            return function(db)
                        finally:
                            db.set_progress_handler(None)
                            counts.append(steps)
                    return await store.call(measured, **options)
            contexts = ContextStore(ObservedStore())
            messages, projection = await contexts.project(self.run, self.instance, self.definition, "Review", [], summarize=summarize)
            return messages, projection, sum(counts)
        def seed_memories(db, start, end):
            for index in range(start, end):
                db.execute("INSERT INTO memories VALUES(?,'i','r',0,'[]',?,?)", ("memory-" + str(index), "Confirmed fact " + str(index), 10000 - index))
                db.execute("INSERT INTO memories VALUES(?,'j','other',0,'[]','Other instance private fact',?)", ("other-memory-" + str(index), index))
        await self.store.call(lambda db: seed_memories(db, 0, 16), transaction=True)
        _, first, first_steps = await measured_projection()
        self.assertEqual(first["memory_ref"], "memory-15")
        await self.store.call(lambda db: seed_memories(db, 16, 1000), transaction=True)
        messages, last, last_steps = await measured_projection()
        self.assertEqual(last["memory_ref"], "memory-999")
        self.assertIn("Confirmed fact 999", encode(messages))
        self.assertNotIn("Other instance private fact", encode(messages))
        self.assertLessEqual(last_steps, first_steps + 20)

    async def test_pressure_threshold_does_not_reject_a_valid_initial_fixed_input(self):
        from oea.contracts.configuration import ContextPolicy
        definition = self.definition.model_copy(update={"context": ContextPolicy(input_tokens=4096, pressure=0.5, memory_tokens=512)})
        await self.contexts.input(self.run, self.instance)
        async def summarize(messages, output_tokens):
            raise AssertionError("There is no older history to summarize")
        messages, projection = await self.contexts.project(self.run, self.instance, definition, "Bounded guidance. " * 400, [], summarize=summarize)
        self.assertGreater(projection["input_tokens"], 2048)
        self.assertLessEqual(projection["input_tokens"], 4096)
        self.assertEqual(len(messages), 2)
        self.assertIsNone(projection["memory_ref"])
