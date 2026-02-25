// ── State ──
let currentSetId = null;
let reviewQueue = [];        // filtered questions to review
let fullReviewQueue = [];    // complete unmastered queue (all categories)
let currentIdx = 0;          // index in reviewQueue
let answered = false;
let totalInSet = 0;
let masteredInSet = 0;
let dragSrcId = null;        // set id being dragged
let selectedCategory = null; // null = all
let categories = [];         // list of category strings
let chatStreaming = false;   // is AI currently streaming?

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
});

// ── Home / Sets ──

async function loadSets() {
    const res = await fetch("/api/sets");
    const sets = await res.json();
    const container = document.getElementById("sets-list");

    if (sets.length === 0) {
        container.innerHTML = '<div class="empty-state">No question sets yet. Upload a PDF to get started.</div>';
        return;
    }

    container.innerHTML = "";
    for (const s of sets) {
        // fetch stats
        const statsRes = await fetch(`/api/sets/${s.id}/stats`);
        const stats = await statsRes.json();

        const card = document.createElement("div");
        card.className = "set-card";
        card.dataset.setId = s.id;
        card.draggable = true;
        card.onclick = () => startQuiz(s.id);
        card.innerHTML = `
            <span class="drag-handle" title="Drag to reorder">&#9776;</span>
            <div class="set-info">
                <h3>${esc(s.name)}</h3>
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

        // Drag handle: only start drag from handle
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

    if (!fileInput.files[0]) return;

    btn.disabled = true;
    status.className = "loading";
    status.classList.remove("hidden");
    status.innerHTML = '<span class="spinner"></span> Extracting questions with AI... This may take a minute.';

    const formData = new FormData();
    formData.append("file", fileInput.files[0]);
    formData.append("name", document.getElementById("set-name").value);
    formData.append("prompt", document.getElementById("prompt").value);
    formData.append("references", document.getElementById("references").value);

    try {
        const res = await fetch("/api/upload", { method: "POST", body: formData });
        const data = await res.json();

        if (!res.ok) {
            throw new Error(data.detail || "Upload failed");
        }

        status.className = "success";
        status.textContent = `Extracted ${data.question_count} questions into "${data.name}"`;

        // Reset form
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

    // Load review queue + stats + categories in parallel
    const [reviewRes, statsRes, catRes] = await Promise.all([
        fetch(`/api/sets/${setId}/review`),
        fetch(`/api/sets/${setId}/stats`),
        fetch(`/api/sets/${setId}/categories`),
    ]);

    fullReviewQueue = await reviewRes.json();
    reviewQueue = [...fullReviewQueue];

    const stats = await statsRes.json();
    totalInSet = stats.total;
    masteredInSet = stats.mastered;

    categories = await catRes.json();

    // Switch view
    document.getElementById("home-view").classList.add("hidden");
    document.getElementById("quiz-view").classList.remove("hidden");
    document.getElementById("toggle-cat-btn").classList.remove("hidden");
    document.getElementById("toggle-chat-btn").classList.remove("hidden");
    document.body.classList.add("quiz-active");

    // Render category sidebar
    renderCategoryList();

    // Load chat history
    loadChatHistory(setId);

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
    document.getElementById("question-text").textContent = q.question_text;
    document.getElementById("result-area").classList.add("hidden");

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

async function selectOption(selectedIdx) {
    if (answered) return;
    answered = true;

    const q = reviewQueue[currentIdx];
    const optBtns = document.querySelectorAll("#options-list .option-btn");

    // Disable all buttons
    optBtns.forEach(b => b.disabled = true);

    // Submit answer
    const res = await fetch("/api/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question_id: q.id, selected_index: selectedIdx }),
    });
    const result = await res.json();

    // Highlight correct / wrong
    optBtns[result.correct_index].classList.add("correct");
    if (!result.correct) {
        optBtns[selectedIdx].classList.add("wrong");
    }

    // Show result banner
    const banner = document.getElementById("result-banner");
    if (result.correct) {
        banner.className = "correct";
        banner.textContent = "Correct!";
    } else {
        banner.className = "wrong";
        banner.textContent = "Incorrect";
    }

    // Show explanation
    renderExplanation(q, result);

    // Show result area
    document.getElementById("result-area").classList.remove("hidden");

    // Update master button state
    const masterBtn = document.getElementById("master-btn");
    masterBtn.classList.remove("mastered");
    masterBtn.textContent = "I know this";
}

function renderExplanation(q, result) {
    const area = document.getElementById("explanation-area");
    area.innerHTML = "";
    const labels = "abcdefghij";
    const explanation = q.explanation;

    // Correct answer explanation
    if (explanation.correct) {
        const block = document.createElement("div");
        block.className = "explanation-block correct-explanation";
        block.innerHTML = `
            <h4>Correct Answer: ${labels[result.correct_index].toUpperCase()}</h4>
            <div class="en">${esc(explanation.correct.en || "")}</div>
            <div class="zh">${esc(explanation.correct.zh || "")}</div>
        `;
        area.appendChild(block);
    }

    // Each option explanation
    if (explanation.options) {
        q.options.forEach((opt, i) => {
            const key = labels[i];
            const optExp = explanation.options[key];
            if (optExp) {
                const block = document.createElement("div");
                block.className = "explanation-block";
                block.innerHTML = `
                    <h4>${key.toUpperCase()}. ${esc(opt)}</h4>
                    <div class="en">${esc(optExp.en || "")}</div>
                    <div class="zh">${esc(optExp.zh || "")}</div>
                `;
                area.appendChild(block);
            }
        });
    }
}

async function markMastered() {
    const q = reviewQueue[currentIdx];
    const masterBtn = document.getElementById("master-btn");
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
        // Remove from both queues
        reviewQueue.splice(currentIdx, 1);
        const fullIdx = fullReviewQueue.findIndex(fq => fq.id === q.id);
        if (fullIdx !== -1) fullReviewQueue.splice(fullIdx, 1);
        // Adjust index
        if (currentIdx >= reviewQueue.length) currentIdx = 0;
        // Update category counts
        renderCategoryList();
    } else {
        masterBtn.classList.remove("mastered");
        masterBtn.textContent = "I know this";
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

    // Move to next (wrap around)
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

// ── Category Sidebar ──

function renderCategoryList() {
    const ul = document.getElementById("category-list");
    ul.innerHTML = "";

    // "All" option
    const allLi = document.createElement("li");
    allLi.className = selectedCategory === null ? "active" : "";
    const allCount = fullReviewQueue.length;
    allLi.innerHTML = `<span>All</span><span class="cat-count">${allCount}</span>`;
    allLi.onclick = () => selectCategory(null);
    ul.appendChild(allLi);

    // Each category
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
    document.getElementById("quiz-view").classList.toggle("right-collapsed");
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
    status.innerHTML = '<span class="spinner"></span> Generating questions with AI...';

    const formData = new FormData();
    formData.append("prompt", document.getElementById("append-prompt").value);
    const fileInput = document.getElementById("append-file");
    if (fileInput.files[0]) {
        formData.append("file", fileInput.files[0]);
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

        // Reload everything
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

    // Re-apply category filter
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

// ── Chat ──

async function loadChatHistory(setId) {
    const res = await fetch(`/api/sets/${setId}/chat/history`);
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
        div.innerHTML = renderSimpleMarkdown(content);
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

async function sendChatMessage() {
    if (chatStreaming) return;

    const input = document.getElementById("chat-input");
    const message = input.value.trim();
    if (!message) return;

    input.value = "";

    // Get current question ID if available
    let questionId = null;
    if (reviewQueue.length > 0 && currentIdx < reviewQueue.length) {
        questionId = reviewQueue[currentIdx].id;
    }

    // Show user message
    appendChatBubble("user", message);
    scrollChatToBottom();

    // Show typing indicator
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

        // Remove typing indicator, create assistant bubble
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
                        assistantDiv.innerHTML = renderSimpleMarkdown(fullText);
                        scrollChatToBottom();
                    }
                    if (data.error) {
                        assistantDiv.textContent = `Error: ${data.error}`;
                    }
                } catch {
                    // ignore parse errors for incomplete chunks
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

// Simple markdown: **bold**, `code`, ```code blocks```
function renderSimpleMarkdown(text) {
    // Escape HTML first
    let html = esc(text);

    // Code blocks: ```...```
    html = html.replace(/```([\s\S]*?)```/g, (_, code) => {
        return `<pre><code>${code.trim()}</code></pre>`;
    });

    // Inline code: `...`
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

    // Bold: **...**
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

    // Line breaks
    html = html.replace(/\n/g, '<br>');

    return html;
}

// ── Delete ──

function showConfirmModal(setId, setName) {
    document.getElementById("confirm-message").textContent =
        `Are you sure you want to delete "${setName}"? All questions and progress will be lost.`;
    const modal = document.getElementById("confirm-modal");
    modal.classList.remove("hidden");
    const okBtn = document.getElementById("confirm-ok-btn");
    // Replace button to remove old listeners
    const newBtn = okBtn.cloneNode(true);
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

    // Remove source from array
    const srcIdx = ids.indexOf(Number(srcId));
    if (srcIdx === -1) return;
    ids.splice(srcIdx, 1);

    // Find target position and insert
    let targetIdx = ids.indexOf(Number(targetId));
    if (!insertBefore) targetIdx++;
    ids.splice(targetIdx, 0, Number(srcId));

    // Save to server
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
