document.addEventListener("DOMContentLoaded", () => {
    const dropZone = document.getElementById("drop-zone");
    const urlInput = document.getElementById("url-input");
    const addBtn = document.getElementById("add-btn");
    const queueSection = document.getElementById("url-queue");
    const queueList = document.getElementById("queue-list");
    const queueCount = document.getElementById("queue-count");
    const clearAllBtn = document.getElementById("clear-all-btn");
    const downloadAllBtn = document.getElementById("download-all-btn");
    const batchProgress = document.getElementById("batch-progress");
    const batchBar = document.getElementById("batch-bar");
    const batchStatusMsg = document.getElementById("batch-status-msg");
    const batchCounts = document.getElementById("batch-counts");
    const taskList = document.getElementById("task-list");

    let urlQueue = [];
    let batchId = null;
    let batchRunning = false;
    const activeCards = [];

    function isValidUrl(text) {
        return /^https?:\/\/(www\.|vm\.)?tiktok\.com\/.+/.test(text.trim());
    }

    function addToQueue(url) {
        url = url.trim();
        if (!url || !isValidUrl(url)) return;
        if (urlQueue.includes(url)) return;
        urlQueue.push(url);
        renderQueue();
    }

    function removeFromQueue(index) {
        urlQueue.splice(index, 1);
        renderQueue();
    }

    function renderQueue() {
        queueSection.style.display = urlQueue.length ? "block" : "none";
        downloadAllBtn.disabled = urlQueue.length === 0 || batchRunning;
        queueCount.textContent = urlQueue.length + " link" + (urlQueue.length !== 1 ? "s": "");

        queueList.innerHTML = "";
        urlQueue.forEach((url, i) => {
            const item = document.createElement("div");
            item.className = "queue-item";
            item.innerHTML == `
                <span class="queue-url" title="${url}">${url}</span>
                <button class="remove-btn" data-index="${i}">&times;</button>
            `;
            queueList.appendChild(item);
        });

        queueList.querySelector(".remove-btn").forEach((btn) => {
            btn.addEventListener("click", () => {
                removeFromQueue(parseInt(btn.dataset.index));
            });
        });
    }

    clearAllBtn.addEventListener("click", () => {
        urlQueue = [];
        renderQueue();
    })

    dropZone.addEventListener("dragover", (e) => {
        e.preventDefault();
        dropZone.classList.add("dragover");
    });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
    dropZone.addEventListener("drop", (e) => {
        e.preventDefault();
        dropZone.classList.remove("dragover");
        const text = (e.dataTransfer.getData("text/plain") || e.dataTransfer.getData("text/uri-list")).trim();
        if (text) {
            text.split(/\s+/).forEach((line) => {
                if (isValidUrl(line)) addToQueue(line);
            });
        }
    });

    addBtn.addEventListener("click", () => {
        const url = urlInput.value.trim();
        if (isValidUrl(url)) {
            addToQueue(url);
            urlInput.value = "";
        }
    });

    urlInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") addBtn.click();
    });

    urlInput.addEventListener("paste", () => {
        setTimeout(() => {
            const text = urlInput.value.trim();
            text.split(/\s+/).forEach((line) => {
                if (isValidUrl(line)) addToQueue(line);
            });
            urlInput.value = "";
        }, 50);
    });

    downloadAllBtn.addEventListener("click", startBatch);

    async function startBatch() {
        if (batchRunning || urlQueue.length === 0) return;
        batchRunning = true;
        downloadAllBtn.disabled = true;
        downloadAllBtn.textContent = "Downloading...";
        batchProgress.style.display = "block";
        batchStatusMsg.textContent = "Starting batch...";
        batchCounts.textContent = "";
        batchBar.style.width = "0%";
        taskList.innerHTML = "";

        const urls = [...urlQueue];
        urlQueue = [];
        renderQueue();

        try {
            const resp = await fetch("/api/batch", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ urls }),
            });
            const data = await resp.json();
            batchId = data.batch_id;
            const numbers = data.numbers;

            urls.forEach((url, i) => {
                const card = createCard(url, numbers[i]);
                activeCards[i] = card;
            });

            connectBatchWs(batchId);
        } catch (err) {
            batchStatusMsg.textContent = "Failed to start batch: " + err.message;
            batchRunning = false;
            downloadAllBtn.disabled = false;
            downloadAllBtn.textContent = "Download All";
        }
    }

    function connectBatchWs(id) {
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        const ws = new WebSocket(`${proto}//${location.host}/ws/${id}`);

        ws.onmessage = (e) => {
            const d = JSON.parse(e.data);
            if (d.type === "progress") {
                handleProgress(d);
            } else if (d.type === "batch_complete") {
                handleBatchComplete(d);
            } else if (d.type === "batch_error") {
                batchStatusMsg.textContent = "Error: " + d.message;
                batchRunning = false;
                downloadAllBtn.disabled = false;
                downloadAllBtn.textContent = "Download All";
            }
        };
        ws.onopen = () => ws.send("ping");
        ws.onclose = () => {};
    }

    function handleProgress(d) {
        const card = activeCards[d.url_index];
        if (!card) return;

        const bar = card.querySelector(".progress-bar");
        const msg = card.querySelector(".task-message");
        const status = card.querySelector(".task-status");

        bar.style.width = d.progress + "%";
        msg.textContent = d.message || "";

        if (d.status === "completed") {
            status.className = "task-status completed";
            status.textContent = "Done";
            bar.style.width = "100%";
            bar.style.background = "#4caf50";
        } else if (d.status === "error") {
            status.className = "task-status error";
            status.textContent = "Error";
            bar.style.background = "#f44336";
        } else if (d.status === "rate_limited") {
            status.className = "task-status rate-limited";
            status.textContent = "Rate Limited";
            bar.style.background = "#ff9800";
        } else {
            status.className = "task-status processing";
            status.textContent = "Downloading";
        }

        const completed = d.completed_count || 0;
        const total = d.total || 1;
        const pct = Math.round((completed / total) * 100);
        batchBar.style.width = pct + "%";
        batchStatusMsg.textContent = `Processing ${completed} of ${total}`;
        batchCounts.textContent = `${d.active_count || 0} active`;
    }

    function handleBatchComplete(d) {
        batchBar.style.width = "100%";
        batchBar.style.background = "#4caf50";
        batchStatusMsg.textContent = `Done! ${d.successful} downloaded, ${d.failed} failed`;
        batchCounts.textContent = "";
        batchRunning = false;
        downloadAllBtn.disabled = urlQueue.length === 0;
        downloadAllBtn.textContent = "Download All";
    }

    function createCard(url, number) {
        const card = document.createElement("div");
        card.className = "task-card";
        card.innerHTML = `
            <div class="task-header">
                <span class="task-number">#${number}</span>
                <span class="task-status processing">Queued</span>
            </div>
            <div class="task-url" title="${url}">${url}</div>
            <div class="progress-wrap"><div class="progress-bar"></div></div>
            <div class="task-message">Starting...</div>
        `;
        taskList.prepend(card);
        return card;
    }
});