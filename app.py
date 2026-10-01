"""StudyBattle — AI Study Battle Game (FastAPI + vanilla JS)."""
import hashlib
import json
import os
import re
import time
import random
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

# ---- Config ----
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
MODEL_ID = "openai/gpt-oss-120b"
PORT = 8010

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Load .env manually (no hard dependency on python-dotenv)
def _load_dotenv():
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass

_load_dotenv()

# Process-global server memory key (never persisted, never logged)
_MEMORY_KEY: Optional[str] = None

def _env_key() -> Optional[str]:
    for name in ("GROQ_API_KEY", "GROQ_TEST_KEY"):
        v = os.environ.get(name)
        if v and v.strip():
            return v.strip()
    return None

def get_effective_key() -> Optional[str]:
    if _MEMORY_KEY and _MEMORY_KEY.strip():
        return _MEMORY_KEY.strip()
    return _env_key()

def get_key_source() -> str:
    if _MEMORY_KEY and _MEMORY_KEY.strip():
        return "memory"
    if _env_key():
        return "env"
    return "none"

def verify_key_live(api_key: str):
    """Verify key via live models.list against Groq endpoint. Raises on failure."""
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=GROQ_BASE_URL, timeout=20.0)
    models = client.models.list()
    return models

def groq_chat_json(api_key: str, messages: List[Dict[str, str]], max_tokens: int = 800) -> Dict[str, Any]:
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=GROQ_BASE_URL, timeout=30.0)
    resp = client.chat.completions.create(
        model=MODEL_ID,
        messages=messages,
        response_format={"type": "json_object"},
        max_tokens=max_tokens,
        temperature=0.8,
    )
    raw = resp.choices[0].message.content or "{}"
    return defensive_json_parse(raw)

def defensive_json_parse(raw: str) -> Dict[str, Any]:
    """Best-effort parse of model JSON output."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    # try to extract {...} block
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    # strip code fences
    cleaned = re.sub(r"```(?:json)?", "", raw).strip()
    try:
        return json.loads(cleaned)
    except Exception as e:
        raise ValueError(f"Model returned non-JSON output: {raw[:300]}") from e

def groq_error_to_status_and_msg(exc: Exception) -> tuple[int, str]:
    status = getattr(exc, "status_code", None)
    msg = str(exc)
    # openai SDK errors carry .status_code
    try:
        from openai import AuthenticationError, RateLimitError, NotFoundError, APIConnectionError, APITimeoutError, InternalServerError
        if isinstance(exc, AuthenticationError):
            return 401, "Invalid API key (Groq rejected it, 401). Check your key and try again."
        if isinstance(exc, RateLimitError):
            return 429, "Groq rate limit hit (429). Wait a few seconds and retry."
        if isinstance(exc, NotFoundError):
            return 404, f"Model not found (404): {MODEL_ID} is not available on this endpoint/key."
        if isinstance(exc, (APIConnectionError, APITimeoutError)):
            return 503, "Could not reach Groq (network/timeout). Check connection and retry."
        if isinstance(exc, InternalServerError):
            return 503, "Groq server error (5xx). Retry in a moment."
    except Exception:
        pass
    if status == 401:
        return 401, "Invalid API key (401). Check your key and try again."
    if status == 429:
        return 429, "Rate limit hit (429). Wait and retry."
    if status == 404:
        return 404, "Model/endpoint not found (404)."
    return 503, f"Groq request failed: {msg[:200]}"

# ---- Question validation + session-level dedupe (pure, unit-testable) ----
# Retry budget: 1 initial Groq attempt + up to 2 retries per question item.
MAX_JUDGE_RETRIES = 2

# Session-level set of normalized-question hashes for the current run.
# Cleared on every /api/start; also mirrored into GAME["seen_hashes"].
_SEEN_HASHES: set = set()


def normalize_qtext(text: str) -> str:
    """Normalize question text for dedupe: lowercase + collapse whitespace."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def question_hash(text: str) -> str:
    """Stable hash of normalized question text (session-level dedupe key)."""
    return hashlib.sha256(normalize_qtext(text).encode("utf-8")).hexdigest()


def validate_question(q: Dict[str, Any]) -> Tuple[bool, str]:
    """Judge pass for one generated question (pure Python, no network).

    Verifies:
      - exactly one correct answer (answer matches exactly one option),
      - no duplicate options (case-insensitive, whitespace-trimmed),
      - correct index in range (answer is one of the options).

    Returns (ok, reason); reason is "ok" when valid.
    """
    if not isinstance(q, dict):
        return False, "question must be an object"
    text = str(q.get("question", "")).strip()
    answer = str(q.get("answer", "")).strip()
    options = q.get("options")
    qtype = q.get("type", "multiple_choice")
    if not text:
        return False, "missing question text"
    if not answer:
        return False, "missing answer"
    if qtype == "true_false":
        if not isinstance(options, list) or [str(o).strip() for o in options] != ["True", "False"]:
            return False, "true_false options must be exactly [True, False]"
        if answer not in ("True", "False"):
            return False, "true_false answer must be True or False"
        return True, "ok"
    if not isinstance(options, list) or len(options) < 2:
        return False, "needs >= 2 options"
    cleaned = [str(o).strip() for o in options]
    if any(not o for o in cleaned):
        return False, "empty option text"
    lowered = [o.casefold() for o in cleaned]
    if len(set(lowered)) != len(lowered):
        return False, "duplicate options"
    matches = [o for o in cleaned if o == answer]
    if len(matches) != 1:
        # case-insensitive matches still ambiguous / not exact
        ci = [o for o in cleaned if o.casefold() == answer.casefold()]
        if len(ci) != 1:
            return False, "answer must match exactly one option"
        return False, "answer must exactly match one option (case-sensitive)"
    idx = cleaned.index(answer)
    if not (0 <= idx < len(cleaned)):
        return False, "correct index out of range"
    return True, "ok"


# ---- Scoring math (pure, unit-testable) ----
DIFF_MULT = {"Easy": 1.0, "Medium": 1.2, "Hard": 1.5}
BASE_XP = {"multiple_choice": 100, "true_false": 80, "boss": 100}
TIME_LIMITS = {"multiple_choice": 15000, "true_false": 10000, "boss": 20000}

def combo_multiplier(streak_after: int) -> float:
    """Combo grows every 3 streak: 1.0, then +0.5 per 3-streak block."""
    if streak_after <= 0:
        return 1.0
    return 1.0 + (streak_after // 3) * 0.5

def speed_multiplier(time_ms: int, limit_ms: int) -> float:
    if time_ms <= 0:
        return 1.5
    ratio = time_ms / max(1, limit_ms)
    if ratio <= 0.3:
        return 1.5
    if ratio <= 0.6:
        return 1.25
    if ratio <= 1.0:
        return 1.0
    return 0.8

def calc_xp(qtype: str, difficulty: str, streak_after: int, time_ms: int, limit_ms: int, is_boss: bool) -> int:
    base = BASE_XP.get(qtype, 100)
    dm = DIFF_MULT.get(difficulty, 1.0)
    cm = combo_multiplier(streak_after)
    sm = speed_multiplier(time_ms, limit_ms)
    boss_mult = 3.0 if is_boss else 1.0
    return max(0, int(round(base * dm * cm * sm * boss_mult)))

def calc_level(xp_total: int) -> int:
    return 1 + (max(0, xp_total) // 500)

DIFF_ORDER = ["Easy", "Medium", "Hard"]

def adapt_difficulty(current: str, streak: int, wrong_streak: int) -> tuple[str, Optional[str]]:
    """Returns (new_difficulty, adaptation_note)."""
    try:
        idx = DIFF_ORDER.index(current)
    except ValueError:
        idx = 1
    if wrong_streak >= 2 and idx > 0:
        return DIFF_ORDER[idx - 1], "difficulty_eased"
    if streak >= 5 and idx < len(DIFF_ORDER) - 1:
        return DIFF_ORDER[idx + 1], "difficulty_raised"
    return current, None

# ---- Game state (server-side, in memory) ----
def fresh_state(topic: str, difficulty: str) -> Dict[str, Any]:
    return {
        "topic": topic,
        "difficulty": difficulty,
        "base_difficulty": difficulty,
        "round": 0,
        "score": 0,
        "xp": 0,
        "level": 1,
        "streak": 0,
        "best_streak": 0,
        "wrong_streak": 0,
        "correct": 0,
        "answered": 0,
        "weak_topics": [],
        "history": [],
        "current": None,  # current question dict
        "finished": False,
        "opened_at": time.time(),
        "seen_hashes": [],
        "dropped_total": 0,
    }

GAME: Dict[str, Any] = fresh_state(topic="", difficulty="Medium")
GAME["idle"] = True  # no active game yet

FALLBACK_BANK = [
    {"sub": "core concepts", "q": "Which of these best describes the core idea?", "opts": ["A fundamental principle", "An unrelated fact", "A random guess", "None of these"], "a": 0, "exp": "The core idea is the fundamental principle behind the topic."},
    {"sub": "definitions", "q": "Which statement is most accurate?", "opts": ["Precise definition", "Vague guess", "Opposite meaning", "Unrelated trivia"], "a": 0, "exp": "The precise definition captures the concept correctly."},
    {"sub": "applications", "q": "Where is this concept most usefully applied?", "opts": ["In real problem solving", "Nowhere at all", "Only in fiction", "Only by accident"], "a": 0, "exp": "Concepts matter when applied to real problem solving."},
]

def build_fallback_question(topic: str, difficulty: str, round_no: int, qtype: str, is_boss: bool) -> Dict[str, Any]:
    bank = FALLBACK_BANK[(round_no - 1) % len(FALLBACK_BANK)]
    if qtype == "true_false":
        answer = "True" if (round_no % 2 == 1) else "False"
        return {
            "type": "true_false",
            "question": f"True or False ({difficulty}): {bank['q']} [Round {round_no} · {topic}]",
            "options": ["True", "False"],
            "answer": answer,
            "explanation": bank["exp"],
            "subtopic": bank["sub"],
        }
    if is_boss or qtype == "boss":
        return {
            "type": "boss",
            "question": f"BOSS — {difficulty} challenge on {topic}: which option shows the deepest understanding? ({bank['q']})",
            "options": bank["opts"],
            "answer": bank["opts"][bank["a"]],
            "explanation": bank["exp"] + " Boss questions demand synthesis.",
            "subtopic": bank["sub"],
        }
    return {
        "type": "multiple_choice",
        "question": f"[{difficulty}] {topic} — {bank['q']} (Round {round_no})",
        "options": bank["opts"],
        "answer": bank["opts"][bank["a"]],
        "explanation": bank["exp"],
        "subtopic": bank["sub"],
    }

def normalize_answer(data: Dict[str, Any], qtype: str, options: Optional[List[str]]) -> Dict[str, Any]:
    """Normalize Groq JSON into canonical question shape. Raises ValueError on bad data."""
    q = str(data.get("question", "")).strip()
    ans = str(data.get("answer", "")).strip()
    exp = str(data.get("explanation", "")).strip() or "No explanation provided."
    sub = str(data.get("subtopic", "")).strip() or "general"
    if not q or not ans:
        raise ValueError("Model JSON missing question/answer.")
    if qtype == "true_false":
        # normalize to True/False options
        opts = ["True", "False"]
        al = ans.lower()
        if al not in ("true", "false", "t", "f"):
            # try to infer
            if "true" in al:
                ans = "True"
            elif "false" in al:
                ans = "False"
            else:
                raise ValueError("True/False answer must be True or False.")
        else:
            ans = "True" if al in ("true", "t") else "False"
        return {"type": "true_false", "question": q, "options": opts, "answer": ans, "explanation": exp, "subtopic": sub}
    # multiple_choice / boss need options
    opts = data.get("options")
    if not isinstance(opts, list) or len(opts) < 2:
        raise ValueError("Multiple-choice JSON needs >= 2 options.")
    opts = [str(o).strip() for o in opts][:4]
    if ans not in opts:
        # try index-based answer
        try:
            idx = int(ans)
            if 0 <= idx < len(opts):
                ans = opts[idx]
            else:
                raise ValueError("Answer not among options.")
        except (ValueError, TypeError):
            # case-insensitive match attempt
            lowered = {o.lower(): o for o in opts}
            if ans.lower() in lowered:
                ans = lowered[ans.lower()]
            else:
                raise ValueError("Answer must be one of the options.")
    t = "boss" if qtype == "boss" else "multiple_choice"
    return {"type": t, "question": q, "options": opts, "answer": ans, "explanation": exp, "subtopic": sub}

def generate_question_live(topic: str, difficulty: str, round_no: int, qtype: str, is_boss: bool, api_key: str) -> Dict[str, Any]:
    target = "boss" if is_boss else qtype
    hard_note = "Make it HARDER than usual: deeper reasoning, tricky distractors." if (is_boss or difficulty == "Hard") else ""
    sys = (
        "You are a quiz game master. Output ONLY a JSON object with keys: "
        "question (string), options (array, omit for true_false... actually always include options: 4 for multiple_choice/boss, [\"True\",\"False\"] for true_false), "
        "answer (string, must exactly match one option), explanation (string, 1-2 sentences), subtopic (string, specific subtopic of the main topic). "
        "No markdown, no extra keys."
    )
    user = (
        f"Topic: {topic}\nDifficulty: {difficulty}\nRound: {round_no}\n"
        f"Question type: {target}\n{hard_note}\n"
        f"Write one {target} question as JSON."
    )
    data = groq_chat_json(api_key, [{"role": "system", "content": sys}, {"role": "user", "content": user}])
    return normalize_answer(data, target, data.get("options"))

# ---- FastAPI app ----
app = FastAPI(title="StudyBattle")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@app.get("/api/status")
def api_status():
    src = get_key_source()
    return {"has_key": src != "none", "source": src, "model": MODEL_ID}

class KeyIn(BaseModel):
    key: str

    @field_validator("key")
    @classmethod
    def _nonempty(cls, v):
        if not v or not v.strip():
            raise ValueError("Key must be non-empty.")
        if len(v.strip()) > 500:
            raise ValueError("Key too long.")
        return v.strip()

@app.post("/api/key")
def api_set_key(body: KeyIn):
    global _MEMORY_KEY
    try:
        verify_key_live(body.key)
    except Exception as exc:
        code, msg = groq_error_to_status_and_msg(exc)
        return JSONResponse(status_code=code, content={"ok": False, "error": msg})
    _MEMORY_KEY = body.key
    return {"ok": True, "source": "memory", "model": MODEL_ID}

@app.delete("/api/key")
def api_clear_key():
    global _MEMORY_KEY
    _MEMORY_KEY = None
    return {"ok": True, "source": get_key_source()}

class StartIn(BaseModel):
    topic: str
    difficulty: str

    @field_validator("topic")
    @classmethod
    def _topic(cls, v):
        if not v or not v.strip():
            raise ValueError("Topic must be non-empty.")
        v = v.strip()
        if len(v) > 200:
            raise ValueError("Topic must be ≤ 200 characters.")
        return v

    @field_validator("difficulty")
    @classmethod
    def _diff(cls, v):
        if v not in ("Easy", "Medium", "Hard"):
            raise ValueError("Difficulty must be Easy, Medium, or Hard.")
        return v

@app.post("/api/start")
def api_start(body: StartIn):
    global GAME
    GAME = fresh_state(body.topic, body.difficulty)
    GAME.pop("idle", None)
    _SEEN_HASHES.clear()
    # Opening flavor: best-effort (fallback if no key/Groq down)
    flavor = f"Get ready to battle: {body.topic} ({body.difficulty})!"
    key = get_effective_key()
    if key:
        try:
            d = groq_chat_json(key, [
                {"role": "system", "content": "Output ONLY JSON: {\"flavor\": \"one hype sentence under 140 chars\"}."},
                {"role": "user", "content": f"Write hype opener for a quiz battle on topic: {body.topic} difficulty {body.difficulty}."},
            ], max_tokens=120)
            if isinstance(d, dict) and d.get("flavor"):
                flavor = str(d["flavor"])[:200]
        except Exception:
            pass
    GAME["flavor"] = flavor
    return {"ok": True, "round": 0, "topic": body.topic, "difficulty": body.difficulty,
            "flavor": flavor, "xp": 0, "level": 1, "score": 0}

@app.post("/api/question")
def api_question():
    global GAME
    if GAME.get("idle"):
        return JSONResponse(status_code=400, content={"ok": False, "error": "No game started. Call /api/start first."})
    if GAME.get("finished"):
        return JSONResponse(status_code=400, content={"ok": False, "error": "Game is finished. Start a new game or replay."})
    GAME["round"] += 1
    rnd = GAME["round"]
    is_boss = (rnd % 5 == 0)
    if is_boss:
        qtype = "boss"
    else:
        qtype = "true_false" if (rnd % 2 == 0) else "multiple_choice"
    limit = TIME_LIMITS.get(qtype, 15000)
    topic = GAME["topic"]
    diff = GAME["difficulty"]
    qdata: Optional[Dict[str, Any]] = None
    source = "fallback"
    err_note = None
    dropped = 0
    validated = False
    key = get_effective_key()
    if key:
        # Judge pass with retry budget: 1 initial attempt + up to 2 retries.
        # Invalid (bad shape / dup options / bad index) or repeated questions
        # are dropped and regenerated so per-round counts stay correct.
        for _attempt in range(1 + MAX_JUDGE_RETRIES):
            try:
                cand = generate_question_live(topic, diff, rnd, qtype if qtype != "boss" else "multiple_choice", is_boss, key)
            except ValueError as ve:
                dropped += 1
                err_note = f"Model output parse issue, retrying: {str(ve)[:150]}"
                continue
            except Exception as exc:
                code, msg = groq_error_to_status_and_msg(exc)
                # Groq hard-fail: stop retrying, fall through to fallback refill.
                err_note = msg
                qdata = None
                break
            ok, reason = validate_question(cand)
            if not ok:
                dropped += 1
                err_note = f"Question failed validation ({reason}), retrying."
                continue
            h = question_hash(cand["question"])
            if h in _SEEN_HASHES:
                dropped += 1
                err_note = "Duplicate question detected, regenerating."
                continue
            qdata = cand
            source = "groq"
            err_note = None
            break
        if qdata is None and dropped > MAX_JUDGE_RETRIES:
            # Retry budget exhausted: drop the bad item, refill below with a
            # known-good fallback so the round count stays correct.
            err_note = (err_note + " Retry budget exhausted; used fallback refill.") if err_note else "Retry budget exhausted; used fallback refill."
    if qdata is None:
        qdata = build_fallback_question(topic, diff, rnd, qtype, is_boss)
    # Final judge + dedupe guard on the served item (covers keyless/fallback
    # path too). Fallback text embeds the round number so it stays unique;
    # on the off chance it still collides, nudge it to keep counts correct.
    ok, _reason = validate_question(qdata)
    h = question_hash(qdata["question"])
    if h in _SEEN_HASHES and source == "fallback":
        qdata = dict(qdata)
        qdata["question"] = f"{qdata['question']} [Round {rnd}]"
        h = question_hash(qdata["question"])
    validated = validate_question(qdata)[0]
    _SEEN_HASHES.add(h)
    GAME.setdefault("seen_hashes", []).append(h)
    GAME["dropped_total"] = int(GAME.get("dropped_total", 0)) + dropped
    GAME["current"] = {**qdata, "round": rnd, "is_boss": is_boss,
                       "time_limit_ms": limit, "asked_at": time.time(), "source": source}
    out = {"ok": True, "round": rnd, "type": qdata["type"], "question": qdata["question"],
           "options": qdata["options"], "time_limit_ms": limit, "is_boss": is_boss,
           "difficulty": diff, "xp_multiplier": (3 if is_boss else 1), "source": source,
           "validated": validated, "dropped": dropped}
    if err_note:
        out["note"] = err_note
    return out


class BatchIn(BaseModel):
    topic: str
    difficulty: str = "Medium"
    count: int = 5

    @field_validator("topic")
    @classmethod
    def _topic(cls, v):
        if not v or not v.strip():
            raise ValueError("Topic must be non-empty.")
        v = v.strip()
        if len(v) > 200:
            raise ValueError("Topic must be ≤ 200 characters.")
        return v

    @field_validator("difficulty")
    @classmethod
    def _diff(cls, v):
        if v not in ("Easy", "Medium", "Hard"):
            raise ValueError("Difficulty must be Easy, Medium, or Hard.")
        return v

    @field_validator("count")
    @classmethod
    def _count(cls, v):
        try:
            iv = int(v)
        except Exception:
            raise ValueError("count must be an integer.")
        if not (1 <= iv <= 20):
            raise ValueError("count must be between 1 and 20.")
        return iv


@app.post("/api/questions/batch")
def api_questions_batch(body: BatchIn):
    """Generate a validated question SET (additive; gameplay flow untouched).

    Each item goes through the same judge pass as /api/question
    (exactly one correct answer, no duplicate options, correct index in
    range) plus session-level dedupe. Bad items are retried up to 2x, then
    dropped and refilled with a fallback so ``len(questions) == count``.
    Response always includes ``validated: true`` (all served items passed)
    and ``dropped: N`` (invalid/dupe items discarded along the way).
    """
    questions: List[Dict[str, Any]] = []
    dropped = 0
    key = get_effective_key()
    seen_local: set = set()
    for i in range(1, body.count + 1):
        item: Optional[Dict[str, Any]] = None
        is_boss = (i % 5 == 0)
        qtype = "boss" if is_boss else ("true_false" if (i % 2 == 0) else "multiple_choice")
        for _attempt in range(1 + MAX_JUDGE_RETRIES):
            if key:
                try:
                    cand = generate_question_live(
                        body.topic, body.difficulty, i,
                        qtype if qtype != "boss" else "multiple_choice", is_boss, key)
                except Exception:
                    dropped += 1
                    continue
            else:
                # Keyless path: pure-python fallback generation (existing pattern).
                cand = build_fallback_question(body.topic, body.difficulty, i, qtype, is_boss)
                # Fallback is deterministic; validate once, no Groq retries needed.
                ok, _r = validate_question(cand)
                h = question_hash(cand["question"])
                if not ok or h in _SEEN_HASHES or h in seen_local:
                    dropped += 1
                    cand = dict(cand)
                    cand["question"] = f"{cand['question']} [set {i}]"
                    h = question_hash(cand["question"])
                item = cand
                break
            ok, _r = validate_question(cand)
            h = question_hash(cand["question"])
            if not ok or h in _SEEN_HASHES or h in seen_local:
                dropped += 1
                continue
            item = cand
            break
        if item is None:
            # Retry budget exhausted: drop the bad item + refill with a
            # unique fallback so the requested count stays correct.
            dropped += 1
            item = build_fallback_question(body.topic, body.difficulty, 10_000 + i, qtype, is_boss)
            item = dict(item)
            item["question"] = f"{item['question']} [refill {i}]"
        h = question_hash(item["question"])
        seen_local.add(h)
        _SEEN_HASHES.add(h)
        GAME.setdefault("seen_hashes", []).append(h)
        questions.append({
            "type": item["type"],
            "question": item["question"],
            "options": item["options"],
            "answer": item["answer"],
            "explanation": item.get("explanation", ""),
            "subtopic": item.get("subtopic", "general"),
            "is_boss": is_boss,
        })
    all_ok = all(validate_question({**q})[0] for q in questions)
    GAME["dropped_total"] = int(GAME.get("dropped_total", 0)) + dropped
    return {"ok": True, "topic": body.topic, "difficulty": body.difficulty,
            "requested": body.count, "count": len(questions),
            "questions": questions, "validated": all_ok, "dropped": dropped}

class AnswerIn(BaseModel):
    choice: str
    time_ms: int = 0

    @field_validator("choice")
    @classmethod
    def _choice(cls, v):
        if v is None or (isinstance(v, str) and not v.strip()):
            raise ValueError("choice must be non-empty.")
        return str(v).strip() if isinstance(v, str) else str(v)

    @field_validator("time_ms")
    @classmethod
    def _t(cls, v):
        try:
            iv = int(v)
        except Exception:
            raise ValueError("time_ms must be an integer.")
        if iv < 0:
            raise ValueError("time_ms must be >= 0.")
        if iv > 300000:
            raise ValueError("time_ms unrealistically large.")
        return iv

@app.post("/api/answer")
def api_answer(body: AnswerIn):
    global GAME
    cur = GAME.get("current")
    if GAME.get("idle"):
        return JSONResponse(status_code=400, content={"ok": False, "error": "No game started."})
    if not cur:
        return JSONResponse(status_code=400, content={"ok": False, "error": "No active question. Call /api/question first."})
    answer = cur["answer"]
    # T/F case-insensitive, MCQ exact (case-insensitive fallback)
    ok = (body.choice.strip().lower() == str(answer).strip().lower())
    limit = cur["time_limit_ms"]
    qtype = cur["type"]
    is_boss = cur.get("is_boss", False)
    GAME["answered"] += 1
    xp_earned = 0
    hint = None
    if ok:
        GAME["correct"] += 1
        GAME["streak"] += 1
        GAME["wrong_streak"] = 0
        GAME["best_streak"] = max(GAME["best_streak"], GAME["streak"])
        xp_earned = calc_xp(qtype, GAME["difficulty"], GAME["streak"], body.time_ms, limit, is_boss)
        GAME["xp"] += xp_earned
        GAME["score"] += 30 if is_boss else 10
        if GAME["streak"] >= 3 and GAME["streak"] % 3 == 0:
            GAME["score"] += 5  # combo bonus points
    else:
        GAME["streak"] = 0
        GAME["wrong_streak"] += 1
        sub = cur.get("subtopic", "general")
        if sub not in GAME["weak_topics"]:
            GAME["weak_topics"].append(sub)
        if GAME["wrong_streak"] >= 2:
            hint = "Hint: review the explanation — the next round will ease off a bit."
    GAME["level"] = calc_level(GAME["xp"])
    new_diff, adaptation = adapt_difficulty(GAME["difficulty"], GAME["streak"], GAME["wrong_streak"])
    GAME["difficulty"] = new_diff
    GAME["history"].append({"round": cur["round"], "type": qtype, "correct": ok,
                           "choice": body.choice, "answer": answer, "subtopic": cur.get("subtopic", "general")})
    GAME["current"] = None
    combo = combo_multiplier(GAME["streak"])
    resp = {"ok": True, "correct": ok, "answer": answer, "explanation": cur.get("explanation", ""),
            "xp_earned": xp_earned, "xp_total": GAME["xp"], "score": GAME["score"],
            "streak": GAME["streak"], "combo": combo, "level": GAME["level"],
            "difficulty": GAME["difficulty"], "adaptation": adaptation, "is_boss": is_boss}
    if hint:
        resp["hint"] = hint
    return resp

@app.post("/api/finish")
def api_finish():
    global GAME
    if GAME.get("idle"):
        return JSONResponse(status_code=400, content={"ok": False, "error": "No game started."})
    GAME["finished"] = True
    total = GAME["answered"]
    correct = GAME["correct"]
    acc = round((correct / total * 100.0), 1) if total else 0.0
    return {"ok": True, "score": GAME["score"], "level": GAME["level"], "xp_total": GAME["xp"],
            "accuracy": acc, "correct": correct, "total": total,
            "best_streak": GAME["best_streak"], "weak_topics": GAME["weak_topics"],
            "rounds": GAME["round"], "topic": GAME["topic"]}

@app.get("/api/state")
def api_state():
    g = dict(GAME)
    cur = g.get("current")
    if cur:
        # hide answer from state peek? keep it hidden for fairness
        cur = {k: v for k, v in cur.items() if k != "answer"}
        g["current"] = cur
    return {"ok": True, "state": g}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)
