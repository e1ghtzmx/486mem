"""Exercise the same handshake for two independent assistants without network calls."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from memory_framework import MemoryBridge, MemoryStore, Scope


def rehearsal_model(context, exchanges):
    """Substitute a provider adapter here. This adapter runs offline."""
    if not exchanges:
        return {"tool_calls": [{
            "name": "memory_recall",
            "arguments": {"intent": "evidence", "query": "慢一点的节拍"},
        }]}
    result = exchanges[-1]["tool_results"][0]["result"]
    evidence = next(item for item in result if item["event_id"] == "rehearsal-message")
    return {"decision": {
        "action": "speak",
        "reply": "我想到一个练法。下次可以先用那个慢一点的节拍练最容易抢拍的两小节。",
        "topic": "这次排练的节拍",
        "reason": "接回用户确实提到的慢节拍，并提供一个新练法。",
        "evidence_ids": [evidence["event_id"]],
    }}


def main():
    with TemporaryDirectory(prefix="memory-handshake-") as directory:
        store = MemoryStore(Path(directory) / "memory.sqlite3")
        bridge = MemoryBridge(store)
        rehearsal = Scope("example", "rehearsal-assistant", "lin", "direct")
        tutoring = Scope("example", "tutoring-assistant", "chen", "direct")

        store.record_event(
            rehearsal,
            event_id="rehearsal-message",
            kind="user_message",
            occurred_at="2026-11-26T17:00:00Z",
            text="这次用慢一点的节拍挺好。",
        )
        turn = bridge.open_turn(
            rehearsal, "rehearsal-message", persona="善于接住排练细节的助手", language="zh"
        )
        observation = bridge.call_tool(
            rehearsal,
            "rehearsal-message",
            "memory_propose",
            {
                "proposal_id": "rehearsal-tempo-observation",
                "operation": "add",
                "kind": "observation",
                "subject": "这次排练",
                "predicate": "节拍反馈",
                "value": "这次用慢一点的节拍比较好",
                "evidence_ids": ["rehearsal-message"],
            },
        )
        assert observation["status"] == "tentative", observation

        store.record_event(
            tutoring,
            event_id="tutoring-message",
            kind="user_message",
            occurred_at="2026-11-26T17:04:00Z",
            text="以后讲代码题时，请先给结论，再解释为什么。",
        )
        # The host has verified that this user explicitly requested the change.
        preference = store.propose(
            tutoring,
            {
                "proposal_id": "tutoring-answer-order",
                "operation": "add",
                "kind": "preference",
                "subject": "代码题讲解",
                "predicate": "回复顺序",
                "value": "先给结论，再解释原因",
                "evidence_ids": ["tutoring-message"],
            },
            event_id="tutoring-message",
            approved=True,
        )
        assert preference["status"] == "applied", preference
        current = bridge.call_tool(
            tutoring, "tutoring-message", "memory_recall", {"query": "代码题", "intent": "current"}
        )
        assert "先给结论" in json.dumps(current, ensure_ascii=False), current
        assert store.get_event(tutoring, "rehearsal-message") is None
        assert store.get_event(rehearsal, "tutoring-message") is None

        store.record_event(
            rehearsal,
            event_id="rehearsal-opportunity",
            kind="opportunity",
            occurred_at="2026-11-26T18:30:00Z",
            metadata={"allowed": True, "expires_at": "2026-11-26T18:35:00Z"},
        )
        decision = bridge.run_turn(
            rehearsal,
            "rehearsal-opportunity",
            rehearsal_model,
            now="2026-11-26T18:31:00Z",
        )
        # Simulate a successful delivery and one explicit, in-topic user reaction.
        store.record_event(
            rehearsal, event_id="rehearsal-opener", kind="assistant_message",
            occurred_at="2026-11-26T18:31:00Z", text=decision["reply"],
            related_to="rehearsal-opportunity",
            metadata={"used_evidence_ids": decision["evidence_ids"]},
        )
        feedback = store.record_event(
            rehearsal, event_id="rehearsal-feedback", kind="feedback",
            occurred_at="2026-11-26T18:32:00Z", text="这个点子好，下次想到类似的可以发我。",
            related_to="rehearsal-opener",
        )
        print(json.dumps({
            "协议版本": turn["protocol_version"],
            "可用工具": [tool["name"] for tool in turn["tools"]],
            "排练反馈": observation["status"],
            "辅导偏好": preference["status"],
            "主动搭话决策": decision,
            "搭话反馈": feedback["text"],
            "范围隔离": "通过",
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
