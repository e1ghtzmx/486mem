"""End-to-end checks for the local framework and its model-facing interface."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from memory_framework import MemoryBridge, MemoryStore, Scope


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.path = Path(directory) / "memory.sqlite3"
        self.store = MemoryStore(self.path)
        self.scope = Scope("orchard", "guide", "mina", "planning")

    def record(self, event_id, text="", *, scope=None, kind="user_message",
               at="2020-06-01T14:00:00+00:00", metadata=None, related_to=None):
        return self.store.record_event(
            scope or self.scope, event_id=event_id, kind=kind, occurred_at=at,
            text=text, metadata=metadata, related_to=related_to,
        )

    def add(self, *, event_id, result_id="add-default", subject="模型语速",
            predicate="默认值", value="慢速", kind="preference", approved=True, **options):
        return self.store.propose(self.scope, {
            "proposal_id": result_id, "operation": "add", "kind": kind,
            "subject": subject, "predicate": predicate, "value": value,
            "evidence_ids": [event_id], **options,
        }, event_id=event_id, approved=approved)

    def test_scope_boundaries_isolate_tenants_assistants_users_and_conversations(self):
        self.record("owner-event", "模型语速，以后默认用慢速。")
        saved = self.add(event_id="owner-event")
        self.assertEqual(saved["status"], "applied")
        self.assertEqual(len(self.store.recall(self.scope, "模型语速")), 1)

        for other in (
            Scope("harbor", "guide", "mina", "planning"),
            Scope("orchard", "tutor", "mina", "planning"),
            Scope("orchard", "guide", "ken", "planning"),
            Scope("orchard", "guide", "mina", "break-room"),
        ):
            with self.subTest(other=other):
                self.record("other-event", "记住模型语速。", scope=other)
                self.assertEqual(self.store.recall(other, "模型语速"), [])
                self.assertEqual(self.store.read_evidence(other, ["owner-event"]), [])
                self.assertIsNone(self.store.get_event(other, "owner-event"))
                rejected = self.store.propose(other, {
                    "proposal_id": "overreaching-proposal", "operation": "add", "kind": "preference",
                    "subject": "模型语速", "predicate": "默认值", "value": "慢速",
                    "evidence_ids": ["owner-event"],
                }, event_id="other-event", approved=True)
                self.assertEqual(rejected["status"], "rejected")

    def test_model_tentative_preference_and_host_approval(self):
        self.record("reaction", "这次慢一点挺好。")
        bridge = MemoryBridge(self.store)
        proposal = {
            "proposal_id": "speed-preference", "operation": "add", "kind": "preference",
            "subject": "模型语速", "predicate": "默认值", "value": "慢速",
            "evidence_ids": ["reaction"],
        }
        with self.assertRaises(ValueError):
            bridge.call_tool(self.scope, "reaction", "memory_propose", {**proposal, "approved": True})
        tentative = bridge.call_tool(self.scope, "reaction", "memory_propose", proposal)
        self.assertIn(tentative["status"], {"tentative", "needs_confirmation"})
        self.assertEqual(self.store.recall(self.scope, "模型语速"), [])
        self.record("approval", "以后默认就用慢速。")
        approved = self.store.propose(self.scope, proposal, event_id="approval", approved=True)
        self.assertEqual(approved["status"], "applied")
        memory = self.store.recall(self.scope, "模型语速")[0]
        self.assertEqual(memory["value"], "慢速")
        self.assertEqual(set(memory["evidence_ids"]), {"reaction", "approval"})

    def test_proposal_retries_are_idempotent_and_stale_revision_is_rejected(self):
        self.record("record-speed", "模型语速默认慢速。")
        initial = self.add(event_id="record-speed")
        retry = self.add(event_id="record-speed")
        self.assertEqual(initial, retry)
        different_value = self.add(event_id="record-speed", value="快速")
        self.assertEqual(different_value["status"], "rejected")
        self.assertEqual(len(self.store.recall(self.scope, "模型语速")), 1)

        self.record("new-speed", "模型语速改成正常。")
        proposal = {
            "proposal_id": "revise-speed", "operation": "revise", "memory_id": initial["memory_id"],
            "base_version": initial["version"], "relation": "supersession",
            "value": "正常", "evidence_ids": ["new-speed"],
        }
        result = self.store.propose(self.scope, proposal, event_id="new-speed", approved=True)
        self.assertEqual(result["status"], "applied")
        self.assertGreater(result["version"], initial["version"])
        self.assertEqual(result, self.store.propose(self.scope, proposal, event_id="new-speed", approved=True))

        self.record("stale-speed", "模型语速改成快速。")
        stale = {**proposal, "proposal_id": "stale-revise", "value": "快速", "evidence_ids": ["stale-speed"]}
        self.assertEqual(
            self.store.propose(self.scope, stale, event_id="stale-speed", approved=True)["status"],
            "stale_version",
        )
        self.assertEqual(self.store.recall(self.scope, "模型语速")[0]["value"], "正常")
        self.assertEqual(
            {row["value"] for row in self.store.recall(self.scope, "模型语速", intent="historical")},
            {"慢速", "正常"},
        )

    def test_conflicting_claim_keeps_both_evidence_values_and_marks_current_state_disputed(self):
        self.record("announced", "负责人确认排练周五。")
        initial = self.add(event_id="announced", subject="排练", predicate="日期", kind="fact", value="周五")
        self.record("hearsay", "朋友转述说周六。")
        proposed = self.store.propose(self.scope, {
            "proposal_id": "conflicting-date", "operation": "revise", "memory_id": initial["memory_id"],
            "base_version": initial["version"], "relation": "conflict", "value": "周六",
            "evidence_ids": ["hearsay"],
        }, event_id="hearsay", approved=True)
        self.assertEqual(proposed["status"], "applied")
        self.assertEqual(
            {row["value"] for row in self.store.recall(self.scope, "排练", intent="historical")},
            {"周五", "周六"},
        )
        self.assertEqual(self.store.recall(self.scope, "排练")[0]["state"], "disputed")

    def test_current_recall_respects_effective_time_of_a_known_change(self):
        self.record("old-location", "排练地点是北厅。", at="2020-06-01T12:00:00+00:00")
        original = self.add(event_id="old-location", subject="排练", predicate="地点", kind="fact", value="北厅")
        self.record("move", "下午四点起，排练改在东厅。", at="2020-06-01T13:00:00+00:00")
        update = self.store.propose(self.scope, {
            "proposal_id": "location-changed", "operation": "revise", "memory_id": original["memory_id"],
            "base_version": original["version"], "relation": "supersession", "value": "东厅",
            "effective_at": "2020-06-01T16:00:00+00:00", "evidence_ids": ["move"],
        }, event_id="move", approved=True)
        self.assertEqual(update["status"], "applied")
        before = self.store.recall(self.scope, "排练", as_of="2020-06-01T15:00:00+00:00")[0]
        at_change = self.store.recall(self.scope, "排练", as_of="2020-06-01T16:00:00+00:00")[0]
        self.assertEqual(before["value"], "北厅")
        self.assertEqual(at_change["value"], "东厅")

    def test_replay_distinguishes_what_was_known_from_when_a_fact_applied(self):
        before = "2020-06-01T13:00:00+00:00"
        after = "2020-06-01T15:00:00+00:00"
        self.record("original", "排练定在北厅。", at="2020-06-01T12:00:00+00:00")
        original = self.add(event_id="original", subject="排练", predicate="地点", kind="fact", value="北厅")
        self.record("correction", "我说错了，原本就是东厅。", at="2020-06-01T14:00:00+00:00")
        corrected = self.store.propose(self.scope, {
            "proposal_id": "correct-location", "operation": "revise", "memory_id": original["memory_id"],
            "base_version": original["version"], "relation": "correction", "value": "东厅",
            "effective_at": "2020-06-01T12:00:00+00:00", "evidence_ids": ["correction"],
        }, event_id="correction", approved=True)
        self.assertEqual(corrected["status"], "applied")

        self.assertEqual(self.store.recall(self.scope, "排练", as_of=before, known_at=before)[0]["value"], "北厅")
        self.assertEqual(self.store.recall(self.scope, "排练", as_of=before, known_at=after)[0]["value"], "东厅")
        self.assertEqual(
            [row["value"] for row in self.store.recall(self.scope, "排练", intent="historical", known_at=before)],
            ["北厅"],
        )
        self.assertEqual(
            [row["event_id"] for row in self.store.read_evidence(
                self.scope, ["original", "correction"], known_at=before,
            )],
            ["original"],
        )

    def test_confirmation_preserves_the_earlier_tentative_version(self):
        self.record("first", "这次慢一点好。", at="2020-06-01T12:00:00+00:00")
        proposal = {
            "proposal_id": "promote-later", "operation": "add", "kind": "preference",
            "subject": "模型语速", "predicate": "默认值", "value": "慢速", "evidence_ids": ["first"],
        }
        tentative = self.store.propose(self.scope, proposal, event_id="first")
        self.assertEqual(tentative["status"], "tentative")
        self.record("confirm", "以后默认慢速。", at="2020-06-01T14:00:00+00:00")
        confirmed = self.store.propose(self.scope, proposal, event_id="confirm", approved=True)
        self.assertEqual(confirmed["status"], "applied")
        self.assertGreater(confirmed["version"], tentative["version"])
        before = "2020-06-01T13:00:00+00:00"
        self.assertEqual(self.store.recall(self.scope, "模型语速", known_at=before), [])
        historical = self.store.recall(self.scope, "模型语速", intent="historical", known_at=before)
        self.assertEqual([(row["version"], row["state"]) for row in historical], [
            (tentative["version"], "tentative"),
        ])

    def test_repeated_confirmed_evidence_does_not_hide_earlier_known_memory(self):
        self.record("first", "默认慢速。", at="2020-06-01T12:00:00+00:00")
        original = self.add(event_id="first")
        self.assertEqual(original["status"], "applied")
        self.record("later", "我仍然想要慢速。", at="2020-06-01T14:00:00+00:00")
        repeated = self.add(event_id="later", result_id="repeat-default")
        self.assertEqual(repeated["status"], "applied")
        self.assertEqual(repeated["version"], original["version"])
        recalls = self.store.recall(self.scope, "模型语速", known_at="2020-06-01T13:00:00+00:00")
        self.assertEqual([(memory["value"], memory["evidence_ids"]) for memory in recalls], [
            ("慢速", ["first"]),
        ])

    def test_evidence_recall_follows_a_memory_anchor_and_respects_time(self):
        self.record("story", "昨天我们说的是电梯怪谈。", at="2020-06-01T12:00:00+00:00")
        memory = self.add(event_id="story", subject="怪谈", predicate="标题", kind="fact", value="电梯")
        self.record("detail", "它总会停在不存在的那一层。", related_to="story",
                    at="2020-06-01T13:00:00+00:00")
        self.record("later", "最后看到隔壁的灯。", related_to="story", at="2020-06-01T15:00:00+00:00")
        self.record("unrelated", "讨论一本新的小说。", at="2020-06-01T13:30:00+00:00")

        recalled = MemoryBridge(self.store).call_tool(self.scope, "later", "memory_recall", {
            "intent": "evidence", "anchor_ids": [memory["memory_id"]], "as_of": "2020-06-01T14:00:00+00:00",
        })
        self.assertEqual({row["event_id"] for row in recalled}, {"story", "detail"})

    def test_graph_recall_traverses_feedback_and_declared_sources_in_both_directions(self):
        self.record("origin", "下次可以聊聊以前的歌。")
        self.record("opener", "突然想起你说过那首歌。", kind="assistant_message",
                    metadata={"used_evidence_ids": ["origin"]})
        self.record("feedback", "开场我很喜欢。", related_to="opener", kind="feedback")
        self.record("unrelated", "今天去了海边。")
        other = Scope("orchard", "guide", "ken", "planning")
        self.record("opener", "另一用户同名的事件。", scope=other)
        self.record("other-feedback", "另一用户的反馈。", scope=other,
                    related_to="opener", kind="feedback")

        for anchor, neighbor in (
            ("origin", "opener"), ("opener", "origin"), ("opener", "feedback"), ("feedback", "opener"),
        ):
            with self.subTest(anchor=anchor):
                ids = {row["event_id"] for row in self.store.recall(
                    self.scope, intent="evidence", anchor_ids=[anchor],
                )}
                self.assertIn(anchor, ids)
                self.assertIn(neighbor, ids)
                self.assertNotIn("unrelated", ids)
                self.assertNotIn("other-feedback", ids)

    def test_related_memory_count_and_model_weight_are_separate_signals(self):
        self.record("shared-first", "先定下假期方案和交通方式。", at="2020-06-01T12:00:00+00:00")
        linked = self.add(event_id="shared-first", subject="假期方案", predicate="路线",
                          kind="fact", value="东线", weight=18)
        neighbor = self.add(event_id="shared-first", result_id="transport",
                            subject="交通方式", predicate="工具", kind="fact", value="巴士")
        self.record("shared-again", "路线还是东线，交通改为高铁。", at="2020-06-01T13:00:00+00:00")
        self.add(event_id="shared-again", result_id="repeat-route",
                 subject="假期方案", predicate="路线", kind="fact", value="东线")
        self.assertEqual(self.store.propose(self.scope, {
            "proposal_id": "revise-transport", "operation": "revise", "memory_id": neighbor["memory_id"],
            "base_version": neighbor["version"], "relation": "supersession", "value": "高铁",
            "evidence_ids": ["shared-again"],
        }, event_id="shared-again", approved=True)["status"], "applied")
        self.record("recent", "清单上已经带了电池。", at="2020-06-01T14:00:00+00:00")
        unlinked = self.add(event_id="recent", result_id="packing-list",
                            subject="假期清单", predicate="物品", kind="fact", value="电池", weight=72)
        self.record("extra", "后来还根据当时的方案定了酒店。", at="2020-06-01T15:00:00+00:00")
        self.assertEqual(self.store.propose(self.scope, {
            "proposal_id": "reserve-hotel", "operation": "add", "kind": "fact",
            "subject": "住宿", "predicate": "状态", "value": "已订",
            "evidence_ids": ["shared-first", "extra"],
        }, event_id="extra", approved=True)["status"], "applied")

        earlier = self.store.recall(self.scope, "假期", known_at="2020-06-01T14:30:00+00:00")
        self.assertEqual([(row["memory_id"], row["weight"], row["related_memories"]) for row in earlier], [
            (unlinked["memory_id"], 72, 0), (linked["memory_id"], 18, 1),
        ])
        self.record("priority-feedback", "做假期规划时现在更常回查路线。", at="2020-06-01T15:05:00+00:00")
        result = MemoryBridge(self.store).call_tool(self.scope, "priority-feedback", "memory_propose", {
            "proposal_id": "raise-route-priority", "operation": "prioritize", "memory_id": linked["memory_id"],
            "base_version": earlier[1]["head_version"], "weight": 93, "evidence_ids": ["priority-feedback"],
        })
        self.assertEqual(result["status"], "applied")
        replay = self.store.recall(self.scope, "假期", known_at="2020-06-01T14:30:00+00:00")
        self.assertEqual([(row["memory_id"], row["weight"]) for row in replay], [
            (unlinked["memory_id"], 72), (linked["memory_id"], 18),
        ])
        later = self.store.recall(self.scope, "假期", known_at="2020-06-01T15:05:00+00:00")
        self.assertEqual([(row["memory_id"], row["weight"], row["related_memories"]) for row in later], [
            (linked["memory_id"], 93, 2), (unlinked["memory_id"], 72, 0),
        ])
        self.assertEqual(later[0]["initial_weight"], 18)

    def test_old_isolated_pinned_memory_survives_weight_changes_and_forget_requests(self):
        self.record("early", "请长期保留这条记忆：家里那棵桂花叫小秋。",
                    at="2010-06-01T12:00:00+00:00")
        memory = self.add(event_id="early", subject="桂花", predicate="称呼", kind="fact",
                          value="小秋", weight=30, pinned=True)
        self.assertEqual(memory["status"], "applied")
        self.record("model-reassess", "这一轮只是在查其他话题。")
        bridge = MemoryBridge(self.store)
        reduced = bridge.call_tool(self.scope, "model-reassess", "memory_propose", {
            "proposal_id": "lower-weight", "operation": "prioritize", "memory_id": memory["memory_id"],
            "base_version": memory["version"], "evidence_ids": ["model-reassess"], "weight": 0,
        })
        self.assertEqual(reduced["status"], "applied")
        current = self.store.recall(self.scope, "桂花")[0]
        self.assertEqual((current["initial_weight"], current["weight"], current["related_memories"]), (30, 0, 0))
        self.assertTrue(current["pinned"])

        self.record("try-forgetting", "忘掉桂花的名字。")
        forget = {
            "proposal_id": "forget-pinned", "operation": "forget", "memory_id": memory["memory_id"],
            "base_version": current["head_version"], "evidence_ids": ["try-forgetting"],
        }
        for attempt in (bridge.call_tool(self.scope, "try-forgetting", "memory_propose", forget),
                        self.store.propose(self.scope, forget, event_id="try-forgetting", approved=True)):
            self.assertEqual(attempt["status"], "rejected")
            self.assertEqual(attempt["reason_code"], "pinned")
        self.assertEqual(self.store.recall(self.scope, "桂花"), [current])
        self.assertEqual([row["event_id"] for row in self.store.read_evidence(self.scope, ["early"])], ["early"])

        self.record("request-unpin", "桂花这条可以取消长期保留。",
                    at="2020-06-01T15:00:00+00:00")
        unpin = {
            "proposal_id": "unpin-guihua", "operation": "prioritize", "memory_id": memory["memory_id"],
            "base_version": current["head_version"], "evidence_ids": ["request-unpin"],
            "weight": 0, "pinned": False,
        }
        suggested = bridge.call_tool(self.scope, "request-unpin", "memory_propose", unpin)
        self.assertIn(suggested["status"], {"needs_confirmation", "rejected"})
        self.assertTrue(self.store.recall(self.scope, "桂花")[0]["pinned"])
        self.assertEqual(self.store.propose(
            self.scope, unpin, event_id="request-unpin", approved=True,
        )["status"], "applied")
        self.assertFalse(MemoryStore(self.path).recall(self.scope, "桂花")[0]["pinned"])

    def test_forgetting_removes_version_values_and_linked_evidence(self):
        self.record("speed-original", "语速设慢速，这是我自己的偏好。")
        original = self.add(event_id="speed-original")
        self.record("speed-change", "默认语速改成正常。")
        revision = self.store.propose(self.scope, {
            "proposal_id": "normal-speed", "operation": "revise", "memory_id": original["memory_id"],
            "base_version": original["version"], "relation": "supersession", "value": "正常",
            "evidence_ids": ["speed-change"],
        }, event_id="speed-change", approved=True)
        self.record("assistant-repeat", "好的，我会默认使用正常语速。", kind="assistant_message",
                    metadata={"used_evidence_ids": ["speed-change"]})
        self.record("feedback-copy", "记住，你刚刚说默认正常语速。", kind="feedback",
                    related_to="assistant-repeat", metadata={"used_evidence_ids": ["assistant-repeat"]})
        self.record("independent", "讨论一件碰巧无关的事。")
        self.record("forget", "忘掉我的默认语速偏好。")
        result = self.store.propose(self.scope, {
            "proposal_id": "forget-speed", "operation": "forget", "memory_id": original["memory_id"],
            "base_version": revision["version"], "evidence_ids": ["forget"],
        }, event_id="forget", approved=True)
        self.assertEqual(result["status"], "applied")

        reopened = MemoryStore(self.path)
        self.assertEqual(reopened.recall(self.scope, "模型语速"), [])
        self.assertEqual(reopened.recall(self.scope, "模型语速", intent="historical"), [])
        revoked = ["speed-original", "speed-change", "assistant-repeat", "feedback-copy"]
        self.assertEqual(reopened.read_evidence(self.scope, revoked), [])
        for event_id in revoked:
            self.assertIsNone(reopened.get_event(self.scope, event_id))
        self.assertEqual(self.add(result_id="replay-revoked", event_id="speed-original")["status"], "rejected")
        retry_original = reopened.propose(self.scope, {
            "proposal_id": "add-default", "operation": "add", "kind": "preference",
            "subject": "模型语速", "predicate": "默认值", "value": "慢速",
            "evidence_ids": ["speed-original"],
        }, event_id="independent", approved=True)
        self.assertEqual(retry_original["status"], "rejected")
        self.assertEqual(len(reopened.read_evidence(self.scope, ["independent"])), 1)

    def test_forgetting_shared_evidence_is_rejected_without_partial_deletion(self):
        self.record("shared", "记住，我的代号是青石。另一个消息是排练在北厅。")
        target = self.add(event_id="shared", subject="我的代号", predicate="称呼",
                          kind="fact", value="青石")
        other = self.add(event_id="shared", result_id="store-hall",
                         subject="合奏排练", predicate="地点", kind="fact", value="北厅")
        self.assertEqual(target["status"], "applied")
        self.assertEqual(other["status"], "applied")
        self.record("independent", "只有代号改成青岩。")
        updated = self.store.propose(self.scope, {
            "proposal_id": "revise-word", "operation": "revise", "memory_id": target["memory_id"],
            "base_version": target["version"], "relation": "supersession", "value": "青岩",
            "evidence_ids": ["independent"],
        }, event_id="independent", approved=True)
        self.assertEqual(updated["status"], "applied")
        self.record("derived", "我会使用青岩。", kind="assistant_message",
                    metadata={"used_evidence_ids": ["independent"]})
        self.record("forget-code", "忘掉我的代号。")
        proposal = {
            "proposal_id": "forget-code-word", "operation": "forget", "memory_id": target["memory_id"],
            "base_version": updated["version"], "evidence_ids": ["forget-code"],
        }
        bridge = MemoryBridge(self.store)
        source_ids = ["shared", "independent", "derived"]
        evidence = self.store.read_evidence(self.scope, source_ids)
        history = self.store.recall(self.scope, intent="historical")
        current = self.store.recall(self.scope)
        self.assertEqual(len(evidence), 3)

        staged = bridge.call_tool(self.scope, "forget-code", "memory_propose", proposal)
        approved = self.store.propose(self.scope, proposal, event_id="forget-code", approved=True)
        for outcome in (staged, approved):
            self.assertEqual(outcome["status"], "rejected")
            self.assertEqual(outcome["reason_code"], "shared_evidence")
        reopened = MemoryStore(self.path)
        self.assertCountEqual(reopened.recall(self.scope, intent="historical"), history)
        self.assertCountEqual(reopened.recall(self.scope), current)
        self.assertEqual(MemoryBridge(reopened).call_tool(
            self.scope, "forget-code", "memory_read_evidence", {"ids": source_ids},
        ), evidence)

    def test_forgetting_is_rejected_if_a_declared_derivative_supports_another_memory(self):
        self.record("origin", "计划排练在北厅。")
        target = self.add(event_id="origin", subject="排练", predicate="地点", kind="fact", value="北厅")
        self.record("derived", "通知已据此预约北厅。", kind="tool_result",
                    metadata={"used_evidence_ids": ["origin"]})
        other = self.add(event_id="derived", result_id="remember-booking", subject="预约",
                         predicate="状态", kind="fact", value="已订")
        self.assertEqual(other["status"], "applied")
        self.record("forget-location", "忘掉原来的排练地点。")
        result = self.store.propose(self.scope, {
            "proposal_id": "forget-location", "operation": "forget", "memory_id": target["memory_id"],
            "base_version": target["version"], "evidence_ids": ["forget-location"],
        }, event_id="forget-location", approved=True)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason_code"], "shared_evidence")
        self.assertEqual(
            {row["event_id"] for row in self.store.read_evidence(self.scope, ["origin", "derived"])},
            {"origin", "derived"},
        )

    def test_open_turn_and_memory_are_reusable_across_model_clients(self):
        self.record("habits", "别人只是分享时，先接细节，一句就可以。")
        self.assertEqual(self.add(event_id="habits", subject="搭话", predicate="只分享时",
                                  value="先接细节，再自然收住", kind="habit")["status"], "applied")
        self.record("prompt", "终于弄好了。")
        old_client = MemoryBridge(self.store)
        new_client = MemoryBridge(MemoryStore(self.path))
        first = old_client.open_turn(self.scope, "prompt", persona="小小园丁", language="zh")
        second = new_client.open_turn(self.scope, "prompt", persona="小小园丁", language="zh")
        self.assertEqual(first, second)
        self.assertEqual(first["event"]["event_id"], "prompt")
        old_memories = old_client.call_tool(self.scope, "prompt", "memory_recall",
                                            {"query": "搭话", "intent": "habit"})
        new_memories = new_client.call_tool(self.scope, "prompt", "memory_recall",
                                            {"query": "搭话", "intent": "habit"})
        self.assertEqual(old_memories, new_memories)
        self.assertIn("先接细节", str(old_memories))

    def test_bridge_replay_prevents_tools_and_decisions_from_using_future_events(self):
        self.record("earlier", "上次说过一首歌。", at="2020-06-01T12:00:00+00:00")
        self.record("old-turn", "你还记得那首歌吗？", at="2020-06-01T13:00:00+00:00")
        self.record("future", "后来改成只答一句。", at="2020-06-01T14:00:00+00:00")
        self.assertEqual(self.add(event_id="future", subject="回复长度", predicate="默认",
                                  value="只答一句", kind="habit")["status"], "applied")
        bridge = MemoryBridge(self.store)
        context = bridge.open_turn(self.scope, "old-turn")
        self.assertEqual(context["habits"], [])
        self.assertNotIn("future", {row["event_id"] for row in context["recent_events"]})
        # A caller-selected effective time does not expand the trusted turn's observation horizon.
        self.assertEqual(bridge.call_tool(self.scope, "old-turn", "memory_recall", {
            "query": "回复长度", "as_of": "2020-06-01T15:00:00+00:00",
        }), [])
        self.assertEqual(bridge.call_tool(self.scope, "old-turn", "memory_read_evidence", {
            "ids": ["future"],
        }), [])
        with self.assertRaises(ValueError):
            bridge.validate_decision(self.scope, "old-turn", {
                "action": "speak", "reply": "只答一句。", "evidence_ids": ["future"],
            })

    def test_open_turn_fills_recent_context_with_observed_messages_not_opportunities(self):
        for index in range(6):
            self.record(f"message-{index}", f"讨论的第 {index} 件事。",
                        at=f"2020-06-01T12:{index:02d}:00+00:00")
            self.record(f"opportunity-{index}", kind="opportunity",
                        at=f"2020-06-01T13:{index:02d}:00+00:00")
        self.record("current", "接着聊吧。")
        context = MemoryBridge(self.store).open_turn(self.scope, "current")
        self.assertEqual(
            [event["event_id"] for event in context["recent_events"]],
            [f"message-{index}" for index in range(6)],
        )

    def test_run_turn_feeds_back_scoped_recall_and_enforces_the_tool_round_limit(self):
        self.record("original", "记住，合奏在北厅。")
        self.assertEqual(self.add(event_id="original", subject="合奏", predicate="地点",
                                  value="北厅", kind="fact")["status"], "applied")
        self.record("question", "合奏在哪儿？")
        call = {"name": "memory_recall", "arguments": {"query": "合奏"}}
        bridge = MemoryBridge(self.store)

        def consumer(context, exchanges):
            self.assertEqual(context["event"]["event_id"], "question")
            if not exchanges:
                return {"tool_calls": [call]}
            memory = exchanges[0]["tool_results"][0]["result"][0]
            return {"decision": {
                "action": "speak", "reply": f"合奏在{memory['value']}。",
                "evidence_ids": memory["evidence_ids"],
            }}

        result = bridge.run_turn(self.scope, "question", consumer, max_tool_rounds=1)
        self.assertEqual(result["reply"], "合奏在北厅。")
        self.assertEqual(result["evidence_ids"], ["original"])

        def looping_consumer(_context, _exchanges):
            return {"tool_calls": [call]}

        with self.assertRaisesRegex(ValueError, "tool round limit"):
            bridge.run_turn(self.scope, "question", looping_consumer, max_tool_rounds=1)

    def test_proactive_decisions_require_a_fresh_allowed_opportunity_and_visible_evidence(self):
        self.record("common-memory", "我们上次一起聊过一个电梯怪谈，结尾没定。")
        other = Scope("orchard", "guide", "ken", "planning")
        self.record("other-memory", "另一位用户的共同经历。", scope=other)
        self.record("open", kind="opportunity", metadata={
            "allowed": True, "expires_at": "2020-06-01T15:00:00+00:00",
        })
        self.record("closed", kind="opportunity", metadata={
            "allowed": False, "expires_at": "2020-06-01T15:00:00+00:00",
        })
        bridge = MemoryBridge(self.store)
        now = "2020-06-01T14:30:00+00:00"
        decision = {
            "action": "speak", "reply": "上次的电梯怪谈，我想到一个结尾。",
            "topic": "一起聊过的电梯怪谈", "reason": "补上上次没定的结局",
            "evidence_ids": ["common-memory"],
        }
        self.assertEqual(bridge.validate_decision(self.scope, "open", decision, now=now)["action"], "speak")
        self.assertEqual(bridge.validate_decision(self.scope, "closed", {"action": "wait"}, now=now)["action"], "wait")
        with self.assertRaises(ValueError):
            bridge.validate_decision(self.scope, "closed", decision, now=now)
        with self.assertRaises(ValueError):
            bridge.validate_decision(self.scope, "open", decision, now="2020-06-01T13:59:00+00:00")
        with self.assertRaises(ValueError):
            bridge.validate_decision(self.scope, "open", decision, now="2020-06-01T15:01:00+00:00")
        with self.assertRaises(ValueError):
            bridge.validate_decision(self.scope, "open", {**decision, "evidence_ids": ["other-memory"]}, now=now)
        with self.assertRaises(ValueError):
            bridge.validate_decision(self.scope, "open", {**decision, "evidence_ids": ["open"]}, now=now)

    def test_restrict_hides_topic_from_proactive_tools_while_keeping_direct_recall(self):
        self.record("practice", "这是我们上次聊的爵士乐练习。")
        saved = self.add(event_id="practice", subject="爵士乐", predicate="话题", value="练习曲", kind="fact")
        self.record("restriction", "以后别主动提爵士乐练习。")
        restricted = self.store.propose(self.scope, {
            "proposal_id": "restrict-jazz", "operation": "restrict", "memory_id": saved["memory_id"],
            "base_version": saved["version"], "evidence_ids": ["restriction"],
        }, event_id="restriction", approved=True)
        self.assertEqual(restricted["status"], "applied")
        self.record("ask", "爵士乐练习上次聊到哪里？")
        self.record("opportunity", kind="opportunity", metadata={
            "allowed": True, "expires_at": "2020-06-01T15:00:00+00:00",
        })
        bridge = MemoryBridge(self.store)
        self.assertEqual(len(bridge.call_tool(self.scope, "ask", "memory_recall", {"query": "爵士乐"})), 1)
        self.assertEqual(bridge.call_tool(self.scope, "opportunity", "memory_recall", {"query": "爵士乐"}), [])
        self.assertEqual(bridge.call_tool(self.scope, "opportunity", "memory_read_evidence", {"ids": ["practice"]}), [])

        self.record("lift-restriction", "以后也可以主动提爵士乐。", at="2020-06-01T14:10:00+00:00")
        proposal = {
            "proposal_id": "allow-jazz-again", "operation": "restrict", "memory_id": saved["memory_id"],
            "base_version": restricted["version"], "value": False, "evidence_ids": ["lift-restriction"],
        }
        self.assertEqual(bridge.call_tool(
            self.scope, "lift-restriction", "memory_propose", proposal,
        )["status"], "needs_confirmation")
        self.assertEqual(self.store.propose(
            self.scope, proposal, event_id="lift-restriction", approved=True,
        )["status"], "applied")
        self.record("after-lift", kind="opportunity", at="2020-06-01T14:12:00+00:00")
        self.assertEqual([row["event_id"] for row in bridge.call_tool(
            self.scope, "after-lift", "memory_read_evidence", {"ids": ["practice"]},
        )], ["practice"])


if __name__ == "__main__":
    unittest.main()
