// ── State ──
let currentSetId = null;
let currentSetType = "mcq";    // 'mcq' or 'open'
let reviewQueue = [];           // filtered questions to review
let fullReviewQueue = [];       // complete unmastered queue (all categories)
let currentIdx = 0;             // index in reviewQueue
let answered = false;
let totalInSet = 0;
let masteredInSet = 0;
let dragSrcId = null;           // set id being dragged
let selectedCategory = null;    // null = all
let categories = [];            // list of category strings
let chatStreaming = false;      // is AI currently streaming?
let guidedStreaming = false;    // is guided dialogue streaming?
let isRecording = false;        // voice recording state
let mediaRecorder = null;
let audioChunks = [];

// ── Init ──
document.addEventListener("DOMContentLoaded", () => {
    loadSets();
    document.getElementById("upload-form").addEventListener("submit", handleUpload);

    // Chat: Enter to send, Shift+Enter for newline
    document.getElementById("chat-input").addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendChatMessage();
        }
    });

    // Guided input: Enter to send
    document.getElementById("guided-input").addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendGuidedMessage();
        }
    });

    // Configure marked.js
    if (typeof marked !== "undefined") {
        marked.setOptions({
            breaks: true,
            gfm: true,
        });
    }
});

// ── Markdown + LaTeX Rendering ──

function renderMarkdown(text) {
    if (!text) return "";

    // Pre-process: protect LaTeX delimiters from marked.js
    let processed = text;
    const displayMathBlocks = [];
    const inlineMathBlocks = [];

    // Display math: \[ ... \]
    processed = processed.replace(/\\\[([\s\S]*?)\\\]/g, (match, content) => {
        const idx = displayMathBlocks.length;
        displayMathBlocks.push(content);
        return `%%DISPLAY_MATH_${idx}%%`;
    });
    // Display math: $$ ... $$ (must come before single $)
    processed = processed.replace(/\$\$([\s\S]*?)\$\$/g, (match, content) => {
        const idx = displayMathBlocks.length;
        displayMathBlocks.push(content);
        return `%%DISPLAY_MATH_${idx}%%`;
    });

    // Inline math: \( ... \)
    processed = processed.replace(/\\\(([\s\S]*?)\\\)/g, (match, content) => {
        const idx = inlineMathBlocks.length;
        inlineMathBlocks.push(content);
        return `%%INLINE_MATH_${idx}%%`;
    });
    // Inline math: $ ... $ (not preceded/followed by $, not spanning newlines)
    processed = processed.replace(/(?<!\$)\$(?!\$)([^\n$]+?)\$(?!\$)/g, (match, content) => {
        const idx = inlineMathBlocks.length;
        inlineMathBlocks.push(content);
        return `%%INLINE_MATH_${idx}%%`;
    });

    // Run through marked
    let html;
    if (typeof marked !== "undefined") {
        html = marked.parse(processed);
    } else {
        html = esc(processed).replace(/\n/g, "<br>");
    }

    // Restore display math
    displayMathBlocks.forEach((content, idx) => {
        try {
            const rendered = katex.renderToString(content.trim(), {
                displayMode: true,
                throwOnError: false,
                trust: true,
            });
            html = html.replace(`%%DISPLAY_MATH_${idx}%%`, rendered);
        } catch (e) {
            html = html.replace(`%%DISPLAY_MATH_${idx}%%`, `<pre class="katex-error">${esc(content)}</pre>`);
        }
    });

    // Restore inline math
    inlineMathBlocks.forEach((content, idx) => {
        try {
            const rendered = katex.renderToString(content.trim(), {
                displayMode: false,
                throwOnError: false,
                trust: true,
            });
            html = html.replace(`%%INLINE_MATH_${idx}%%`, rendered);
        } catch (e) {
            html = html.replace(`%%INLINE_MATH_${idx}%%`, `<code class="katex-error">${esc(content)}</code>`);
        }
    });

    return html;
}

// ── Home / Sets ──

async function loadSets() {
    const res = await fetch("/api/sets");
    const sets = await res.json();
    const container = document.getElementById("sets-list");

    if (sets.length === 0) {
        container.innerHTML = '<div class="empty-state">No question sets yet. Upload a file to get started.</div>';
        return;
    }

    container.innerHTML = "";
    for (const s of sets) {
        const statsRes = await fetch(`/api/sets/${s.id}/stats`);
        const stats = await statsRes.json();

        const card = document.createElement("div");
        card.className = "set-card";
        card.dataset.setId = s.id;
        card.draggable = true;
        card.onclick = () => startQuiz(s.id);

        const typeLabel = (s.set_type === "open") ? "Open" : "MCQ";
        const typeBadge = `<span class="type-badge type-${s.set_type || 'mcq'}">${typeLabel}</span>`;

        card.innerHTML = `
            <span class="drag-handle" title="Drag to reorder">&#9776;</span>
            <div class="set-info">
                <h3>${esc(s.name)} ${typeBadge}</h3>
                <div class="meta">${esc(s.source_file || "")}</div>
            </div>
            <div class="set-stats">
                <div class="count">${stats.total} questions</div>
                <div class="mastered-info">${stats.mastered}/${stats.total} mastered</div>
            </div>
            <button class="delete-btn" title="Delete set">&times;</button>
        `;

        // Delete button handler
        card.querySelector(".delete-btn").addEventListener("click", (e) => {
            e.stopPropagation();
            showConfirmModal(s.id, s.name);
        });

        // Drag handle
        const handle = card.querySelector(".drag-handle");
        handle.addEventListener("mousedown", () => { card.draggable = true; });
        handle.addEventListener("click", (e) => { e.stopPropagation(); });

        // Drag events
        card.addEventListener("dragstart", (e) => {
            dragSrcId = s.id;
            card.classList.add("dragging");
            e.dataTransfer.effectAllowed = "move";
        });
        card.addEventListener("dragend", () => {
            card.classList.remove("dragging");
            clearDragStyles();
            dragSrcId = null;
        });
        card.addEventListener("dragover", (e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = "move";
            if (String(s.id) === String(dragSrcId)) return;
            clearDragStyles();
            const rect = card.getBoundingClientRect();
            const midY = rect.top + rect.height / 2;
            if (e.clientY < midY) {
                card.classList.add("drag-over-top");
            } else {
                card.classList.add("drag-over-bottom");
            }
        });
        card.addEventListener("dragleave", () => {
            card.classList.remove("drag-over-top", "drag-over-bottom");
        });
        card.addEventListener("drop", (e) => {
            e.preventDefault();
            if (String(s.id) === String(dragSrcId)) return;
            const rect = card.getBoundingClientRect();
            const midY = rect.top + rect.height / 2;
            const insertBefore = e.clientY < midY;
            reorderAfterDrop(dragSrcId, s.id, insertBefore);
        });

        container.appendChild(card);
    }
}

function showHome() {
    document.getElementById("home-view").classList.remove("hidden");
    document.getElementById("quiz-view").classList.add("hidden");
    document.getElementById("toggle-cat-btn").classList.add("hidden");
    document.getElementById("toggle-chat-btn").classList.add("hidden");
    document.body.classList.remove("quiz-active");
    currentSetId = null;
    currentSetType = "mcq";
    selectedCategory = null;
    categories = [];
    loadSets();
}

// ── Upload ──

async function handleUpload(e) {
    e.preventDefault();
    const btn = document.getElementById("upload-btn");
    const status = document.getElementById("upload-status");
    const fileInput = document.getElementById("pdf-file");

    if (!fileInput.files.length) return;

    const fileCount = fileInput.files.length;
    btn.disabled = true;
    status.className = "loading";
    status.classList.remove("hidden");
    const fileLabel = fileCount > 1 ? `${fileCount} files` : "file";
    status.innerHTML = `<span class="spinner"></span> Extracting questions from ${fileLabel} with AI... This may take a minute.`;

    const formData = new FormData();
    for (const file of fileInput.files) {
        formData.append("files", file);
    }
    formData.append("name", document.getElementById("set-name").value);
    formData.append("prompt", document.getElementById("prompt").value);
    formData.append("references", document.getElementById("references").value);
    formData.append("set_type", document.getElementById("set-type").value);

    try {
        const res = await fetch("/api/upload", { method: "POST", body: formData });
        const data = await res.json();

        if (!res.ok) {
            throw new Error(data.detail || "Upload failed");
        }

        status.className = "success";
        status.textContent = `Extracted ${data.question_count} questions into "${data.name}"`;

        document.getElementById("upload-form").reset();
        loadSets();
    } catch (err) {
        status.className = "error";
        status.textContent = `Error: ${err.message}`;
    } finally {
        btn.disabled = false;
    }
}

// ── Quiz ──

async function startQuiz(setId) {
    currentSetId = setId;
    currentIdx = 0;
    answered = false;
    selectedCategory = null;

    // Load review queue + stats + categories + set type in parallel
    const [reviewRes, statsRes, catRes, typeRes] = await Promise.all([
        fetch(`/api/sets/${setId}/review`),
        fetch(`/api/sets/${setId}/stats`),
        fetch(`/api/sets/${setId}/categories`),
        fetch(`/api/sets/${setId}/type`),
    ]);

    fullReviewQueue = await reviewRes.json();
    reviewQueue = [...fullReviewQueue];

    const stats = await statsRes.json();
    totalInSet = stats.total;
    masteredInSet = stats.mastered;

    categories = await catRes.json();

    const typeData = await typeRes.json();
    currentSetType = typeData.set_type || "mcq";

    // Switch view
    document.getElementById("home-view").classList.add("hidden");
    document.getElementById("quiz-view").classList.remove("hidden");
    document.getElementById("toggle-cat-btn").classList.remove("hidden");
    document.getElementById("toggle-chat-btn").classList.remove("hidden");
    document.body.classList.add("quiz-active");

    renderCategoryList();
    loadChatHistory();

    if (reviewQueue.length === 0) {
        document.getElementById("quiz-card").classList.add("hidden");
        document.getElementById("done-card").classList.remove("hidden");
    } else {
        document.getElementById("quiz-card").classList.remove("hidden");
        document.getElementById("done-card").classList.add("hidden");
        renderQuestion();
    }
    updateProgress();
}

function renderQuestion() {
    const q = reviewQueue[currentIdx];
    answered = false;

    document.getElementById("question-category").textContent = q.category || "";
    document.getElementById("question-number").textContent = `Question ${currentIdx + 1} of ${reviewQueue.length}`;

    // Render question text with markdown+LaTeX
    const questionTextEl = document.getElementById("question-text");
    const isOpen = q.type === "open";
    if (isOpen) {
        // Wrap in styled card for better rendering of math content
        questionTextEl.className = "question-text markdown-content open-question-text-card";
    } else {
        questionTextEl.className = "question-text markdown-content";
    }
    questionTextEl.innerHTML = renderMarkdown(q.question_text);

    // Hide all result areas
    document.getElementById("result-area").classList.add("hidden");

    if (isOpen) {
        // Open question mode
        document.getElementById("options-list").innerHTML = "";
        document.getElementById("options-list").classList.add("hidden");
        document.getElementById("idk-area").classList.add("hidden");
        document.getElementById("open-question-area").classList.remove("hidden");
        document.getElementById("open-answer-area").classList.add("hidden");
        document.getElementById("reveal-btn").classList.remove("hidden");
        document.getElementById("open-master-btn").classList.remove("hidden");
        document.getElementById("open-next-btn").classList.remove("hidden");

        // Load per-question chat history
        loadGuidedChatHistory(q.id);
    } else {
        // MCQ mode
        document.getElementById("options-list").classList.remove("hidden");
        document.getElementById("open-question-area").classList.add("hidden");
        document.getElementById("idk-area").classList.remove("hidden");

        const optList = document.getElementById("options-list");
        optList.innerHTML = "";
        const labels = "abcdefghij";

        q.options.forEach((opt, i) => {
            const btn = document.createElement("button");
            btn.className = "option-btn";
            btn.innerHTML = `<span class="option-label">${labels[i].toUpperCase()}.</span> ${esc(opt)}`;
            btn.onclick = () => selectOption(i);
            optList.appendChild(btn);
        });
    }

    // Reload sidebar chat for this question
    loadChatHistory();
}

// ── MCQ Answer Flow ──

async function selectOption(selectedIdx) {
    if (answered) return;
    answered = true;

    const q = reviewQueue[currentIdx];
    const optBtns = document.querySelectorAll("#options-list .option-btn");

    optBtns.forEach(b => b.disabled = true);
    document.getElementById("idk-area").classList.add("hidden");

    const res = await fetch("/api/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question_id: q.id, selected_index: selectedIdx }),
    });
    const result = await res.json();

    optBtns[result.correct_index].classList.add("correct");
    if (!result.correct) {
        optBtns[selectedIdx].classList.add("wrong");
    }

    const banner = document.getElementById("result-banner");
    if (result.correct) {
        banner.className = "correct";
        banner.textContent = "Correct!";
    } else {
        banner.className = "wrong";
        banner.textContent = "Incorrect";
    }

    renderExplanation(q, result);

    document.getElementById("result-area").classList.remove("hidden");

    // Show Mastered button only if correct
    const masterBtn = document.getElementById("master-btn");
    if (result.correct) {
        masterBtn.classList.remove("hidden");
        masterBtn.classList.remove("mastered");
        masterBtn.textContent = "Mastered";
    } else {
        masterBtn.classList.add("hidden");
    }
}

function revealMCQAnswer() {
    if (answered) return;
    answered = true;

    const q = reviewQueue[currentIdx];
    const optBtns = document.querySelectorAll("#options-list .option-btn");

    optBtns.forEach(b => b.disabled = true);
    document.getElementById("idk-area").classList.add("hidden");

    // Highlight correct answer
    optBtns[q.correct_index].classList.add("correct");

    const banner = document.getElementById("result-banner");
    banner.className = "wrong";
    banner.textContent = "Answer Revealed";

    // Record as seen but wrong
    fetch("/api/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question_id: q.id, selected_index: -1 }),
    });

    renderExplanation(q, { correct: false, correct_index: q.correct_index });

    document.getElementById("result-area").classList.remove("hidden");

    // Hide mastered button — user didn't answer
    document.getElementById("master-btn").classList.add("hidden");
}

function renderExplanation(q, result) {
    const area = document.getElementById("explanation-area");
    area.innerHTML = "";
    const labels = "abcdefghij";
    const explanation = q.explanation;

    if (explanation.correct) {
        const block = document.createElement("div");
        block.className = "explanation-block correct-explanation";
        block.innerHTML = `
            <h4>Correct Answer: ${labels[result.correct_index].toUpperCase()}</h4>
            <div class="en">${renderMarkdown(explanation.correct.en || "")}</div>
            <div class="zh">${renderMarkdown(explanation.correct.zh || "")}</div>
        `;
        area.appendChild(block);
    }

    if (explanation.options) {
        q.options.forEach((opt, i) => {
            const key = labels[i];
            const optExp = explanation.options[key];
            if (optExp) {
                const block = document.createElement("div");
                block.className = "explanation-block";
                block.innerHTML = `
                    <h4>${key.toUpperCase()}. ${esc(opt)}</h4>
                    <div class="en">${renderMarkdown(optExp.en || "")}</div>
                    <div class="zh">${renderMarkdown(optExp.zh || "")}</div>
                `;
                area.appendChild(block);
            }
        });
    }
}

// ── Open Question Flow ──

async function revealOpenAnswer() {
    const q = reviewQueue[currentIdx];
    answered = true;

    const res = await fetch(`/api/questions/${q.id}/answer`);
    const data = await res.json();

    const answerContent = document.getElementById("open-answer-content");
    answerContent.innerHTML = renderMarkdown(data.answer);

    document.getElementById("open-answer-area").classList.remove("hidden");
    document.getElementById("reveal-btn").classList.add("hidden");
    document.getElementById("open-master-btn").classList.add("hidden");
}

async function loadGuidedChatHistory(questionId) {
    const res = await fetch(`/api/questions/${questionId}/chat/history`);
    const messages = await res.json();
    const container = document.getElementById("guided-messages");
    container.innerHTML = "";

    for (const msg of messages) {
        appendGuidedBubble(msg.role, msg.content);
    }

    scrollGuidedToBottom();
}

function appendGuidedBubble(role, content) {
    const container = document.getElementById("guided-messages");
    const div = document.createElement("div");
    div.className = `chat-msg ${role}`;

    if (role === "assistant") {
        // Remove [UNDERSTOOD] tag from display
        const cleanContent = content.replace(/\[UNDERSTOOD\]/g, "").trim();
        div.innerHTML = renderMarkdown(cleanContent);
    } else {
        div.textContent = content;
    }

    container.appendChild(div);
    return div;
}

function scrollGuidedToBottom() {
    const container = document.getElementById("guided-messages");
    container.scrollTop = container.scrollHeight;
}

async function sendGuidedMessage() {
    if (guidedStreaming || answered) return;

    const input = document.getElementById("guided-input");
    const message = input.value.trim();
    if (!message) return;

    input.value = "";

    const q = reviewQueue[currentIdx];

    appendGuidedBubble("user", message);
    scrollGuidedToBottom();

    // Typing indicator
    const container = document.getElementById("guided-messages");
    const typingDiv = document.createElement("div");
    typingDiv.className = "typing-indicator";
    typingDiv.innerHTML = "<span></span><span></span><span></span>";
    container.appendChild(typingDiv);
    scrollGuidedToBottom();

    guidedStreaming = true;
    document.getElementById("guided-send-btn").disabled = true;

    try {
        const res = await fetch(`/api/questions/${q.id}/chat/stream`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message }),
        });

        typingDiv.remove();
        const assistantDiv = appendGuidedBubble("assistant", "");

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let fullText = "";

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            const chunk = decoder.decode(value, { stream: true });
            const lines = chunk.split("\n");

            for (const line of lines) {
                if (!line.startsWith("data: ")) continue;
                const payload = line.slice(6).trim();
                if (payload === "[DONE]") continue;

                try {
                    const data = JSON.parse(payload);
                    if (data.delta) {
                        fullText += data.delta;
                        const cleanText = fullText.replace(/\[UNDERSTOOD\]/g, "").trim();
                        assistantDiv.innerHTML = renderMarkdown(cleanText);
                        scrollGuidedToBottom();
                    }
                    if (data.understood) {
                        // AI determined user understands — auto trigger mastered
                        showUnderstoodNotification();
                    }
                    if (data.error) {
                        assistantDiv.textContent = `Error: ${data.error}`;
                    }
                } catch {
                    // ignore parse errors
                }
            }
        }
    } catch (err) {
        typingDiv.remove();
        appendGuidedBubble("assistant", `Error: ${err.message}`);
    } finally {
        guidedStreaming = false;
        document.getElementById("guided-send-btn").disabled = false;
        scrollGuidedToBottom();
    }
}

function showUnderstoodNotification() {
    // Flash the mastered button to indicate AI thinks user understands
    const masterBtn = document.getElementById("open-master-btn");
    masterBtn.classList.add("ai-suggested");
    masterBtn.textContent = "AI: You got it! Mark Mastered?";

    setTimeout(() => {
        if (!masterBtn.classList.contains("mastered")) {
            masterBtn.classList.remove("ai-suggested");
            masterBtn.textContent = "Mastered";
        }
    }, 5000);
}

// ── Voice Recording ──

async function toggleVoiceRecording() {
    if (isRecording) {
        stopRecording();
    } else {
        startRecording();
    }
}

async function startRecording() {
    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        mediaRecorder = new MediaRecorder(stream);
        audioChunks = [];

        mediaRecorder.ondataavailable = (e) => {
            if (e.data.size > 0) audioChunks.push(e.data);
        };

        mediaRecorder.onstop = async () => {
            stream.getTracks().forEach(t => t.stop());
            const audioBlob = new Blob(audioChunks, { type: "audio/webm" });
            await sendAudioForTranscription(audioBlob);
        };

        mediaRecorder.start();
        isRecording = true;
        document.getElementById("mic-icon").classList.add("hidden");
        document.getElementById("mic-recording-icon").classList.remove("hidden");
        document.getElementById("voice-btn").classList.add("recording");
    } catch (err) {
        console.error("Microphone access denied:", err);
    }
}

function stopRecording() {
    if (mediaRecorder && mediaRecorder.state !== "inactive") {
        mediaRecorder.stop();
    }
    isRecording = false;
    document.getElementById("mic-icon").classList.remove("hidden");
    document.getElementById("mic-recording-icon").classList.add("hidden");
    document.getElementById("voice-btn").classList.remove("recording");
}

async function sendAudioForTranscription(audioBlob) {
    const voiceBtn = document.getElementById("voice-btn");
    voiceBtn.disabled = true;

    try {
        const formData = new FormData();
        formData.append("file", audioBlob, "recording.webm");

        const res = await fetch("/api/transcribe", { method: "POST", body: formData });
        const data = await res.json();

        if (data.text) {
            const input = document.getElementById("guided-input");
            // Append to existing text
            if (input.value.trim()) {
                input.value += " " + data.text;
            } else {
                input.value = data.text;
            }
            input.focus();
        }
    } catch (err) {
        console.error("Transcription failed:", err);
    } finally {
        voiceBtn.disabled = false;
    }
}

// ── Mastered + Next ──

async function markMastered() {
    const q = reviewQueue[currentIdx];

    // Determine which button to update based on question type
    const isOpen = q.type === "open";
    const masterBtn = isOpen
        ? document.getElementById("open-master-btn")
        : document.getElementById("master-btn");

    const isMastered = !masterBtn.classList.contains("mastered");

    await fetch("/api/master", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question_id: q.id, mastered: isMastered }),
    });

    if (isMastered) {
        masterBtn.classList.add("mastered");
        masterBtn.textContent = "Mastered!";
        masteredInSet++;
        reviewQueue.splice(currentIdx, 1);
        const fullIdx = fullReviewQueue.findIndex(fq => fq.id === q.id);
        if (fullIdx !== -1) fullReviewQueue.splice(fullIdx, 1);
        if (currentIdx >= reviewQueue.length) currentIdx = 0;
        renderCategoryList();
    } else {
        masterBtn.classList.remove("mastered");
        masterBtn.textContent = "Mastered";
        masteredInSet--;
    }

    updateProgress();
}

function nextQuestion() {
    if (reviewQueue.length === 0) {
        document.getElementById("quiz-card").classList.add("hidden");
        document.getElementById("done-card").classList.remove("hidden");
        return;
    }

    if (currentIdx >= reviewQueue.length) currentIdx = 0;
    if (!answered) {
        currentIdx = (currentIdx + 1) % reviewQueue.length;
    } else {
        const q = reviewQueue[currentIdx];
        if (q) {
            currentIdx = (currentIdx + 1) % reviewQueue.length;
        }
    }

    renderQuestion();
    updateProgress();
}

function updateProgress() {
    const pct = totalInSet > 0 ? (masteredInSet / totalInSet) * 100 : 0;
    document.getElementById("progress-fill").style.width = `${pct}%`;
    document.getElementById("progress-text").textContent = `${masteredInSet}/${totalInSet}`;
}

// ── Reset & Restart ──

async function resetAndRestart() {
    if (!currentSetId) return;
    await fetch(`/api/sets/${currentSetId}/reset`, { method: "POST" });
    await startQuiz(currentSetId);
}

// ── Category Sidebar ──

function renderCategoryList() {
    const ul = document.getElementById("category-list");
    ul.innerHTML = "";

    const allLi = document.createElement("li");
    allLi.className = selectedCategory === null ? "active" : "";
    const allCount = fullReviewQueue.length;
    allLi.innerHTML = `<span>All</span><span class="cat-count">${allCount}</span>`;
    allLi.onclick = () => selectCategory(null);
    ul.appendChild(allLi);

    for (const cat of categories) {
        const count = fullReviewQueue.filter(q => q.category === cat).length;
        const li = document.createElement("li");
        li.className = selectedCategory === cat ? "active" : "";
        li.innerHTML = `<span>${esc(cat)}</span><span class="cat-count">${count}</span>`;
        li.onclick = () => selectCategory(cat);
        ul.appendChild(li);
    }
}

function selectCategory(cat) {
    selectedCategory = cat;
    currentIdx = 0;
    answered = false;

    if (cat === null) {
        reviewQueue = [...fullReviewQueue];
    } else {
        reviewQueue = fullReviewQueue.filter(q => q.category === cat);
    }

    renderCategoryList();

    if (reviewQueue.length === 0) {
        document.getElementById("quiz-card").classList.add("hidden");
        document.getElementById("done-card").classList.remove("hidden");
    } else {
        document.getElementById("quiz-card").classList.remove("hidden");
        document.getElementById("done-card").classList.add("hidden");
        renderQuestion();
    }
}

// ── Sidebar Toggles ──

function toggleCategorySidebar() {
    document.getElementById("quiz-view").classList.toggle("left-collapsed");
}

function toggleChatSidebar() {
    const quizView = document.getElementById("quiz-view");
    quizView.classList.toggle("right-collapsed");
    const toggleBtn = document.getElementById("toggle-chat-btn");
    if (quizView.classList.contains("right-collapsed")) {
        toggleBtn.style.right = "8px";
    } else {
        toggleBtn.style.right = `calc(var(--right-w) + 8px)`;
    }
}

// ── Append Questions ──

function openAppendModal() {
    document.getElementById("append-modal").classList.remove("hidden");
    document.getElementById("append-prompt").focus();
}

function closeAppendModal() {
    document.getElementById("append-modal").classList.add("hidden");
    document.getElementById("append-form").reset();
    const status = document.getElementById("append-status");
    status.classList.add("hidden");
    status.className = "hidden";
}

async function handleAppend(e) {
    e.preventDefault();
    const btn = document.getElementById("append-submit-btn");
    const status = document.getElementById("append-status");

    btn.disabled = true;
    btn.textContent = "Generating...";
    status.className = "loading";
    status.classList.remove("hidden");
    const appendFileCount = document.getElementById("append-file").files.length;
    const appendLabel = appendFileCount > 1 ? ` from ${appendFileCount} files` : "";
    status.innerHTML = `<span class="spinner"></span> Generating questions${appendLabel} with AI...`;

    const formData = new FormData();
    formData.append("prompt", document.getElementById("append-prompt").value);
    const fileInput = document.getElementById("append-file");
    for (const file of fileInput.files) {
        formData.append("files", file);
    }
    formData.append("references", document.getElementById("append-references").value);

    try {
        const res = await fetch(`/api/sets/${currentSetId}/append`, {
            method: "POST",
            body: formData,
        });
        const data = await res.json();

        if (!res.ok) {
            throw new Error(data.detail || "Failed to add questions");
        }

        status.className = "success";
        status.textContent = `Added ${data.added_count} questions!`;

        await reloadQuizData();
        setTimeout(() => closeAppendModal(), 1200);
    } catch (err) {
        status.className = "error";
        status.textContent = `Error: ${err.message}`;
    } finally {
        btn.disabled = false;
        btn.textContent = "Add";
    }
}

async function reloadQuizData() {
    if (!currentSetId) return;

    const [reviewRes, statsRes, catRes] = await Promise.all([
        fetch(`/api/sets/${currentSetId}/review`),
        fetch(`/api/sets/${currentSetId}/stats`),
        fetch(`/api/sets/${currentSetId}/categories`),
    ]);

    fullReviewQueue = await reviewRes.json();
    const stats = await statsRes.json();
    totalInSet = stats.total;
    masteredInSet = stats.mastered;
    categories = await catRes.json();

    if (selectedCategory) {
        reviewQueue = fullReviewQueue.filter(q => q.category === selectedCategory);
    } else {
        reviewQueue = [...fullReviewQueue];
    }

    currentIdx = 0;
    renderCategoryList();
    updateProgress();

    if (reviewQueue.length > 0) {
        document.getElementById("quiz-card").classList.remove("hidden");
        document.getElementById("done-card").classList.add("hidden");
        renderQuestion();
    }
}

// ── Chat (Set-level, right sidebar) ──

async function loadChatHistory() {
    if (!currentSetId) return;
    let questionId = null;
    if (reviewQueue.length > 0 && currentIdx < reviewQueue.length) {
        questionId = reviewQueue[currentIdx].id;
    }

    let url = `/api/sets/${currentSetId}/chat/history`;
    if (questionId) url += `?question_id=${questionId}`;

    const res = await fetch(url);
    const messages = await res.json();
    const container = document.getElementById("chat-messages");
    container.innerHTML = "";

    for (const msg of messages) {
        appendChatBubble(msg.role, msg.content);
    }

    scrollChatToBottom();
}

function appendChatBubble(role, content) {
    const container = document.getElementById("chat-messages");
    const div = document.createElement("div");
    div.className = `chat-msg ${role}`;

    if (role === "assistant") {
        div.innerHTML = renderMarkdown(content);
    } else {
        div.textContent = content;
    }

    container.appendChild(div);
    return div;
}

function scrollChatToBottom() {
    const container = document.getElementById("chat-messages");
    container.scrollTop = container.scrollHeight;
}

async function clearSidebarChat() {
    if (!currentSetId) return;
    let questionId = null;
    if (reviewQueue.length > 0 && currentIdx < reviewQueue.length) {
        questionId = reviewQueue[currentIdx].id;
    }
    let url = `/api/sets/${currentSetId}/chat`;
    if (questionId) url += `?question_id=${questionId}`;
    await fetch(url, { method: "DELETE" });
    document.getElementById("chat-messages").innerHTML = "";
}

async function clearGuidedChat() {
    if (reviewQueue.length === 0 || currentIdx >= reviewQueue.length) return;
    const q = reviewQueue[currentIdx];
    await fetch(`/api/questions/${q.id}/chat`, { method: "DELETE" });
    document.getElementById("guided-messages").innerHTML = "";
}

async function sendChatMessage() {
    if (chatStreaming) return;

    const input = document.getElementById("chat-input");
    const message = input.value.trim();
    if (!message) return;

    input.value = "";

    let questionId = null;
    if (reviewQueue.length > 0 && currentIdx < reviewQueue.length) {
        questionId = reviewQueue[currentIdx].id;
    }

    appendChatBubble("user", message);
    scrollChatToBottom();

    const container = document.getElementById("chat-messages");
    const typingDiv = document.createElement("div");
    typingDiv.className = "typing-indicator";
    typingDiv.innerHTML = "<span></span><span></span><span></span>";
    container.appendChild(typingDiv);
    scrollChatToBottom();

    chatStreaming = true;
    document.getElementById("chat-send-btn").disabled = true;

    try {
        const res = await fetch(`/api/sets/${currentSetId}/chat/stream`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message, question_id: questionId }),
        });

        typingDiv.remove();
        const assistantDiv = appendChatBubble("assistant", "");

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let fullText = "";

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            const chunk = decoder.decode(value, { stream: true });
            const lines = chunk.split("\n");

            for (const line of lines) {
                if (!line.startsWith("data: ")) continue;
                const payload = line.slice(6).trim();
                if (payload === "[DONE]") continue;

                try {
                    const data = JSON.parse(payload);
                    if (data.delta) {
                        fullText += data.delta;
                        assistantDiv.innerHTML = renderMarkdown(fullText);
                        scrollChatToBottom();
                    }
                    if (data.error) {
                        assistantDiv.textContent = `Error: ${data.error}`;
                    }
                } catch {
                    // ignore parse errors
                }
            }
        }
    } catch (err) {
        typingDiv.remove();
        appendChatBubble("assistant", `Error: ${err.message}`);
    } finally {
        chatStreaming = false;
        document.getElementById("chat-send-btn").disabled = false;
        scrollChatToBottom();
    }
}

// ── Delete ──

function showConfirmModal(setId, setName) {
    document.getElementById("confirm-message").textContent =
        `Are you sure you want to delete "${setName}"? All questions and progress will be lost.`;
    const modal = document.getElementById("confirm-modal");
    modal.classList.remove("hidden");
    const okBtn = document.getElementById("confirm-ok-btn");
    const newBtn = okBtn.cloneNode(true);
    newBtn.disabled = false;
    newBtn.textContent = "Delete";
    okBtn.parentNode.replaceChild(newBtn, okBtn);
    newBtn.addEventListener("click", async () => {
        newBtn.disabled = true;
        newBtn.textContent = "Deleting...";
        await fetch(`/api/sets/${setId}`, { method: "DELETE" });
        closeConfirmModal();
        loadSets();
    });
}

function closeConfirmModal() {
    document.getElementById("confirm-modal").classList.add("hidden");
}

// ── Drag & Drop ──

function clearDragStyles() {
    document.querySelectorAll(".set-card").forEach(c => {
        c.classList.remove("drag-over-top", "drag-over-bottom");
    });
}

function reorderAfterDrop(srcId, targetId, insertBefore) {
    const container = document.getElementById("sets-list");
    const cards = [...container.querySelectorAll(".set-card")];
    const ids = cards.map(c => Number(c.dataset.setId));

    const srcIdx = ids.indexOf(Number(srcId));
    if (srcIdx === -1) return;
    ids.splice(srcIdx, 1);

    let targetIdx = ids.indexOf(Number(targetId));
    if (!insertBefore) targetIdx++;
    ids.splice(targetIdx, 0, Number(srcId));

    fetch("/api/sets/reorder", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ordered_ids: ids }),
    }).then(() => loadSets());
}

// ── Util ──
function esc(str) {
    const d = document.createElement("div");
    d.textContent = str;
    return d.innerHTML;
}
