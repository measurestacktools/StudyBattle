"""StudyBattle API tests: state machine, scoring math, validation. No live key needed (fallback)."""
from fastapi.testclient import TestClient
import app as game

client = TestClient(game.app)

def test_scoring_combo():
    assert game.combo_multiplier(0) == 1.0
    assert game.combo_multiplier(2) == 1.0
    assert game.combo_multiplier(3) == 1.5
    assert game.combo_multiplier(6) == 2.0
    assert game.combo_multiplier(9) == 2.5

def test_scoring_speed():
    assert game.speed_multiplier(0, 15000) == 1.5
    assert game.speed_multiplier(3000, 15000) == 1.5
    assert game.speed_multiplier(8000, 15000) == 1.25
    assert game.speed_multiplier(14000, 15000) == 1.0
    assert game.speed_multiplier(20000, 15000) == 0.8

def test_calc_xp_boss_triple():
    normal = game.calc_xp("multiple_choice", "Medium", 1, 5000, 15000, False)
    boss = game.calc_xp("multiple_choice", "Medium", 1, 5000, 15000, True)
    assert boss == normal * 3

def test_calc_level():
    assert game.calc_level(0) == 1
    assert game.calc_level(499) == 1
    assert game.calc_level(500) == 2
    assert game.calc_level(1000) == 3

def test_adapt_difficulty():
    d, note = game.adapt_difficulty("Hard", 0, 2)
    assert d == "Medium" and note == "difficulty_eased"
    d, note = game.adapt_difficulty("Medium", 5, 0)
    assert d == "Hard" and note == "difficulty_raised"
    d, note = game.adapt_difficulty("Medium", 1, 0)
    assert d == "Medium" and note is None

def test_validation_start():
    r = client.post("/api/start", json={"topic": "", "difficulty": "Medium"})
    assert r.status_code == 422
    r = client.post("/api/start", json={"topic": "x" * 201, "difficulty": "Medium"})
    assert r.status_code == 422
    r = client.post("/api/start", json={"topic": "Math", "difficulty": "Impossible"})
    assert r.status_code == 422

def test_full_state_machine_fallback():
    r = client.post("/api/start", json={"topic": "Photosynthesis", "difficulty": "Medium"})
    assert r.status_code == 200 and r.json()["ok"]
    # play 5 rounds to hit boss on round 5
    boss_seen = False
    for i in range(1, 6):
        q = client.post("/api/question")
        assert q.status_code == 200, q.text
        qd = q.json()
        assert qd["round"] == i
        if i == 5:
            assert qd["is_boss"] is True and qd["type"] == "boss"
            boss_seen = True
        else:
            assert qd["is_boss"] is False
        # answer: fetch real answer from server-side GAME (test-only peek)
        ans = game.GAME["current"]["answer"]
        a = client.post("/api/answer", json={"choice": ans, "time_ms": 2000})
        assert a.status_code == 200, a.text
        assert a.json()["correct"] is True
    assert boss_seen
    f = client.post("/api/finish")
    assert f.status_code == 200
    fd = f.json()
    assert fd["correct"] == 5 and fd["total"] == 5
    assert fd["accuracy"] == 100.0
    assert fd["xp_total"] > 0 and fd["score"] > 0

def test_answer_validation_and_weak_topics():
    client.post("/api/start", json={"topic": "History", "difficulty": "Easy"})
    client.post("/api/question")
    r = client.post("/api/answer", json={"choice": "", "time_ms": 100})
    assert r.status_code == 422
    r = client.post("/api/answer", json={"choice": "whatever-wrong-xyz", "time_ms": 100})
    assert r.status_code == 200 and r.json()["correct"] is False
    f = client.post("/api/finish").json()
    assert len(f["weak_topics"]) >= 1

def test_status_shape():
    r = client.get("/api/status").json()
    assert "has_key" in r and "source" in r and "model" in r
    assert r["model"] == "openai/gpt-oss-120b"
    assert r["source"] in ("memory", "env", "none")
