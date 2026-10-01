"""Validator + session dedupe + retry-budget tests (mocked Groq, no key needed)."""
from fastapi.testclient import TestClient
import app as game

client = TestClient(game.app)


def _good(qtext="What is 2+2?", opts=None, ans="4"):
    return {"type": "multiple_choice", "question": qtext,
            "options": list(opts or ["4", "3", "5", "6"]),
            "answer": ans, "explanation": "basic math", "subtopic": "arith"}


def test_duplicate_options_rejected():
    ok, reason = game.validate_question(_good(opts=["Paris", "paris ", "Rome", "Nice"]))
    assert ok is False and "duplicate" in reason
    ok, _ = game.validate_question(_good(opts=["A", "B", "C", "D"], ans="A"))
    assert ok is True


def test_exactly_one_correct_answer():
    ok, _ = game.validate_question(_good(ans="Not-an-option"))
    assert ok is False
    # case-insensitive-but-inexact match is rejected (must exactly match)
    ok, reason = game.validate_question(_good(ans="paris", qtext="Cap?",
                                              opts=["Paris", "Rome", "Nice", "Oslo"]))
    assert ok is False and "exactly" in reason
    ok, _ = game.validate_question(_good(ans="Paris", qtext="Cap?",
                                         opts=["Paris", "Rome", "Nice", "Oslo"]))
    assert ok is True


def test_true_false_shape():
    q = {"type": "true_false", "question": "Sky is blue?", "options": ["True", "False"],
         "answer": "True", "explanation": "x", "subtopic": "y"}
    assert game.validate_question(q)[0] is True
    bad = dict(q, answer="Maybe")
    assert game.validate_question(bad)[0] is False


def test_question_exposes_validated_and_dropped_fallback():
    game._SEEN_HASHES.clear()
    client.post("/api/start", json={"topic": "DedupeT", "difficulty": "Easy"})
    q = client.post("/api/question")
    assert q.status_code == 200, q.text
    body = q.json()
    assert body["validated"] is True
    assert body["dropped"] == 0


def test_dedupe_across_calls_regenerates(monkeypatch):
    game._SEEN_HASHES.clear()
    client.post("/api/start", json={"topic": "DupTopic", "difficulty": "Medium"})
    game._MEMORY_KEY = None
    calls = {"n": 0}

    def fake_live(topic, difficulty, round_no, qtype, is_boss, api_key):
        calls["n"] += 1
        if calls["n"] == 1:
            return _good("Repeat question?", ["A1", "B1", "C1", "D1"], "A1")
        if calls["n"] == 2:
            return _good("Repeat question?", ["A1", "B1", "C1", "D1"], "A1")  # dup -> dropped
        return _good("Fresh question?", ["A2", "B2", "C2", "D2"], "A2")

    monkeypatch.setattr(game, "generate_question_live", fake_live)
    monkeypatch.setattr(game, "get_effective_key", lambda: "gsk_test")

    first = client.post("/api/question").json()
    assert first["validated"] is True and first["dropped"] == 0
    second = client.post("/api/question").json()
    assert second["validated"] is True
    assert second["dropped"] >= 1, "repeat within a run must be dropped + regenerated"
    assert second["question"] != first["question"]
    assert calls["n"] == 3


def test_retry_budget_respected_then_refill(monkeypatch):
    game._SEEN_HASHES.clear()
    client.post("/api/start", json={"topic": "BudgetT", "difficulty": "Medium"})
    calls = {"n": 0}

    def always_bad(topic, difficulty, round_no, qtype, is_boss, api_key):
        calls["n"] += 1
        return _good(f"Bad {calls['n']}?", ["Same", "same", "Other", "Else"], "Same")

    monkeypatch.setattr(game, "generate_question_live", always_bad)
    monkeypatch.setattr(game, "get_effective_key", lambda: "gsk_test")

    body = {"topic": "BudgetT", "difficulty": "Medium", "count": 2}
    r = client.post("/api/questions/batch", json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["validated"] is True
    assert d["count"] == 2 == d["requested"], "counts stay correct via drop+refill"
    # Per item: 1 initial + max 2 retries => <= 3 Groq attempts each
    assert calls["n"] <= 2 * (1 + game.MAX_JUDGE_RETRIES)
    assert d["dropped"] >= 2


def test_batch_validates_and_dedupes(monkeypatch):
    game._SEEN_HASHES.clear()
    seen = {"n": 0}

    def seq(topic, difficulty, round_no, qtype, is_boss, api_key):
        seen["n"] += 1
        if qtype == "true_false":
            ans = "True" if (seen["n"] % 2) else "False"
            return {"type": "true_false", "question": f"TF unique {seen['n']}?",
                    "options": ["True", "False"], "answer": ans,
                    "explanation": "e", "subtopic": "s"}
        return _good(f"Q unique {seen['n']}?",
                     [f"O{seen['n']}a", f"O{seen['n']}b", f"O{seen['n']}c", f"O{seen['n']}d"],
                     f"O{seen['n']}a")

    monkeypatch.setattr(game, "generate_question_live", seq)
    monkeypatch.setattr(game, "get_effective_key", lambda: "gsk_test")
    r = client.post("/api/questions/batch",
                    json={"topic": "BatchT", "difficulty": "Easy", "count": 4})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["validated"] is True and d["dropped"] == 0
    assert len(d["questions"]) == 4
    texts = [q["question"] for q in d["questions"]]
    assert len(set(texts)) == 4
