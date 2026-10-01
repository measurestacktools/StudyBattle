// StudyBattle frontend — vanilla JS. NEVER stores API keys (no localStorage/sessionStorage for keys).
let difficulty = "Medium";
let timerId = null;
let qStart = 0;
let timeLimit = 15000;
let lastTimeout = false;

const $ = (id) => document.getElementById(id);

function show(id) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.remove("active"));
  $(id).classList.add("active");
}
function setHud(d) {
  if (d.score !== undefined) $("hudScore").textContent = d.score;
  if (d.xp_total !== undefined) $("hudXp").textContent = d.xp_total;
  if (d.xp !== undefined) $("hudXp").textContent = d.xp;
  if (d.level !== undefined) $("hudLevel").textContent = d.level;
  if (d.streak !== undefined) $("hudStreak").textContent = d.streak;
  const c = Number(d.combo);
  $("hudCombo").textContent = c && c > 1 ? `×${c}` : "";
}
async function api(path, method = "GET", body) {
  const r = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.detail || `Request failed (${r.status})`);
  return data;
}

// Setup
$("inpTopic").addEventListener("input", (e) => { $("topicCount").textContent = `${e.target.value.length}/200`; });
$("inpTopic").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("btnStart").click(); }
});
document.querySelectorAll("#diffRow .diff").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#diffRow .diff").forEach((x) => x.classList.remove("sel"));
    b.classList.add("sel");
    difficulty = b.dataset.d;
  });
});
$("btnStart").addEventListener("click", async () => {
  $("setupErr").textContent = "";
  const topic = $("inpTopic").value.trim();
  if (!topic) { $("setupErr").textContent = "Enter a subject/topic to battle!"; $("inpTopic").focus(); return; }
  if (topic.length > 200) { $("setupErr").textContent = "Topic must be ≤ 200 characters."; return; }
  const btn = $("btnStart");
  btn.disabled = true;
  btn.textContent = "WRITING ON BOARD…";
  try {
    const d = await api("/api/start", "POST", { topic, difficulty });
    $("setupFlavor").textContent = d.flavor || "";
    setHud({ score: 0, xp: 0, level: 1, streak: 0, combo: 1 });
    await loadQuestion();
  } catch (e) { $("setupErr").textContent = e.message; }
  btn.disabled = false;
  btn.textContent = "START QUIZ ➤";
});

function startTimer(ms) {
  clearInterval(timerId);
  timeLimit = ms; qStart = Date.now();
  lastTimeout = false;
  const bar = $("timerBar");
  bar.style.width = "100%";
  timerId = setInterval(() => {
    const el = Date.now() - qStart;
    const left = Math.max(0, 1 - el / ms);
    bar.style.width = `${left * 100}%`;
    if (el >= ms) { clearInterval(timerId); timeoutAnswer(); }
  }, 100);
}
async function loadQuestion() {
  show("screen-battle");
  $("battleErr").textContent = "";
  $("qText").textContent = "Writing question on the board…";
  $("qOpts").innerHTML = "";
  try {
    const q = await api("/api/question", "POST");
    $("roundLabel").textContent = `ROUND ${q.round}`;
    $("diffLabel").textContent = q.difficulty.toUpperCase();
    const boss = $("bossBadge");
    if (q.is_boss) boss.classList.remove("hidden"); else boss.classList.add("hidden");
    $("qText").textContent = q.question;
    const wrap = $("qOpts");
    q.options.forEach((opt) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "opt"; b.textContent = opt;
      b.addEventListener("click", () => answer(opt, b));
      wrap.appendChild(b);
    });
    startTimer(q.time_limit_ms);
  } catch (e) { $("battleErr").textContent = e.message; }
}
async function timeoutAnswer() {
  // auto-submit with empty-ish wrong choice on timeout
  lastTimeout = true;
  await answer("__TIMEOUT__", null, true);
}
function setOptsEnabled(on) {
  document.querySelectorAll("#qOpts .opt").forEach((b) => { b.disabled = !on; });
}
async function answer(choice, btn, isTimeout = false) {
  clearInterval(timerId);
  const elapsed = Date.now() - qStart;
  setOptsEnabled(false);
  try {
    const payloadChoice = isTimeout ? "__TIMEOUT__" : choice;
    const r = await api("/api/answer", "POST", { choice: payloadChoice, time_ms: elapsed });
    if (btn) btn.classList.add(r.correct ? "correct" : "wrong");
    // highlight right answer
    document.querySelectorAll("#qOpts .opt").forEach((b) => {
      if (b.textContent.trim().toLowerCase() === String(r.answer).trim().toLowerCase()) b.classList.add("correct");
    });
    setHud(r);
    setTimeout(() => showFeedback(r, isTimeout), 700);
  } catch (e) {
    $("battleErr").textContent = e.message;
    setOptsEnabled(true); // let the student retry instead of a dead board
  }
}
function showFeedback(r, wasTimeout) {
  show("screen-feedback");
  const timedOut = wasTimeout || lastTimeout;
  if (timedOut && !r.correct) {
    $("fbTitle").textContent = "TIME'S UP!";
  } else {
    $("fbTitle").textContent = r.correct ? (r.is_boss ? "BONUS NAILED!" : "CORRECT!") : "NOT QUITE!";
  }
  $("fbTitle").style.color = r.correct ? "#7fb069" : "#d1604f";
  $("fbXp").textContent = r.correct ? `+${r.xp_earned} XP` : "+0 XP";
  $("fbExpl").textContent = `Answer: ${r.answer} — ${r.explanation}`;
  let extra = `Streak ${r.streak} · Combo ×${r.combo} · Level ${r.level}`;
  if (r.adaptation === "difficulty_eased") extra += " · Next round eases off (2 misses)";
  if (r.adaptation === "difficulty_raised") extra += " · Stepping up (streak ≥ 5)!";
  if (r.hint) extra += ` · ${r.hint}`;
  $("fbHint").textContent = extra;
}
$("btnNext").addEventListener("click", loadQuestion);

// Finish flow via the FINISH button on the battle HUD.
$("btnEndBattle").addEventListener("click", finishGame);
async function finishGame() {
  try {
    const f = await api("/api/finish", "POST");
    $("fScore").textContent = f.score;
    $("fXp").textContent = f.xp_total;
    $("fLevel").textContent = f.level;
    $("fAcc").textContent = `${f.accuracy}% (${f.correct}/${f.total})`;
    $("fStreak").textContent = f.best_streak;
    const weak = $("fWeak");
    weak.innerHTML = "";
    if (f.weak_topics.length) {
      f.weak_topics.forEach((w) => {
        const s = document.createElement("span");
        s.textContent = w; // textContent: never interpret model text as HTML
        weak.appendChild(s);
      });
    } else {
      const s = document.createElement("span");
      s.textContent = "None — flawless!";
      weak.appendChild(s);
    }
    show("screen-final");
  } catch (e) { $("battleErr").textContent = e.message; }
}
$("btnFinish").addEventListener("click", () => show("screen-setup"));
$("btnReplay").addEventListener("click", async () => {
  const topic = $("inpTopic").value.trim() || "General Knowledge";
  try {
    await api("/api/start", "POST", { topic, difficulty });
    setHud({ score: 0, xp: 0, level: 1, streak: 0, combo: 1 });
    await loadQuestion();
  } catch (e) { show("screen-setup"); $("setupErr").textContent = e.message; }
});
$("btnCopy").addEventListener("click", async () => {
  const t = `STUDY BATTLE RESULTS — Score ${$("fScore").textContent}, XP ${$("fXp").textContent}, Level ${$("fLevel").textContent}, Accuracy ${$("fAcc").textContent}, Best streak ${$("fStreak").textContent}, Weak: ${$("fWeak").textContent}`;
  try { await navigator.clipboard.writeText(t); $("copyMsg").textContent = "Copied!"; }
  catch { $("copyMsg").textContent = t; }
});

// Key modal (no browser storage of keys — key lives only in POST body, then discarded)
$("btnKey").addEventListener("click", async () => {
  $("keyModal").classList.remove("hidden");
  $("inpKey").value = "";
  try { const s = await api("/api/status"); $("keyStatus").textContent = `status: ${s.has_key ? "set (" + s.source + ")" : "not set"} · ${s.model}`; }
  catch { $("keyStatus").textContent = "status: ?"; }
});
$("btnCloseKey").addEventListener("click", () => $("keyModal").classList.add("hidden"));
$("keyModal").addEventListener("click", (e) => { if (e.target === $("keyModal")) $("keyModal").classList.add("hidden"); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("keyModal").classList.contains("hidden")) $("keyModal").classList.add("hidden");
});
$("btnSaveKey").addEventListener("click", async () => {
  $("keyMsg").textContent = "Verifying...";
  const k = $("inpKey").value;
  if (!k.trim()) { $("keyMsg").textContent = "Paste a key first."; return; }
  $("btnSaveKey").disabled = true;
  try {
    await api("/api/key", "POST", { key: k });
    $("keyMsg").textContent = "Key saved in server memory ✓";
    $("inpKey").value = "";
  } catch (e) { $("keyMsg").textContent = e.message; }
  $("btnSaveKey").disabled = false;
});
$("btnClearKey").addEventListener("click", async () => {
  try { await api("/api/key", "DELETE"); $("keyMsg").textContent = "Key cleared."; }
  catch (e) { $("keyMsg").textContent = e.message; }
});
