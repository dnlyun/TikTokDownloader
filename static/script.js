document.addEventListener("DOMContentLoaded", () => {
    const $ = (id) => document.getElementById(id);
    const dropZone = $("drop-zone");
    const urlInput = $("url-input");
    const addBtn = $("add-btn");
    const queueSection = $("url-queue");
    const queueList = $("queue-list");
    const queueCount = $("queue-count");
    const downloadAllBtn = $("download-all-btn");
    const batchProgress = $("batch-progress");
    const batchBar = $("batch-bar");
    const batchStatusMsg = $("batch-status-msg");
    const batchCounts = $("batch-counts");
    const taskList = $("task-list");

    let urlQueue = [];
    let batchRunning = false;
    const activeCards = {};
    const oembedCache = new Map();

    function isValidUrl(text) {
        return /^https?:\/\/(www\.|vm\.)?tiktok\.com\/.+/.test(text.trim());
    }

    function extractUrls(text) {
        return text.trim().split(/\s+/).filter(isValidUrl);
    }

    async function fetchOembed(url) {
        if (oembedCache.has(url)) return oembedCache.get(url);
        try {
            const resp = await fetch(`https://www.tiktok.com/oembed?url=${encodeURIComponent(url)}`);
            if (!resp.ok) return null;
            const data = await resp.json();
            oembedCache.set(url, data);
            return data;
        } catch {
            return null;
        }
    }

    function isPhotoUrl(url) {
        return /\/photo\//.test(url);
    }

    async function loadThumbnail(url, container) {
        if (isPhotoUrl(url)) {
            const placeholder = document.createElement("div");
            placeholder.className = "queue-thumb placeholder-thumb";
            placeholder.textContent = "\ud83d\uddbc";
            container.prepend(placeholder);
            return;
        }

        const data = await fetchOembed(url);
        if (!data || !data.thumbnail_url) return;

        const img = document.createElement("img");
        img.src = data.thumbnail_url;
        img.className = "queue-thumb";
        img.alt = "";
        img.addEventListener("load", () => {
            const list = container.closest(".queue-list");
            if (list) list.scrollTop = list.scrollHeight;
        });
        container.prepend(img);
    }

    function addToQueue(url) {
        url = url.trim();
        if (!url || !isValidUrl(url) || urlQueue.includes(url)) return;
        urlQueue.push(url);
        renderQueue();
    }

    function renderQueue() {
        queueSection.style.display = urlQueue.length ? "block" : "none";
        downloadAllBtn.disabled = urlQueue.length === 0 || batchRunning;
        queueCount.textContent = `${urlQueue.length} link${urlQueue.length !== 1 ? "s" : ""}`;

        queueList.innerHTML = "";
        urlQueue.forEach((url, i) => {
            const item = document.createElement("div");
            item.className = "queue-item";

            const span = document.createElement("span");
            span.className = "queue-url";
            span.title = url;
            span.textContent = url;

            const btn = document.createElement("button");
            btn.className = "remove-btn";
            btn.textContent = "\u00d7";
            btn.addEventListener("click", () => {
                urlQueue.splice(i, 1);
                renderQueue();
            });

            item.append(span, btn);
            queueList.appendChild(item);

            loadThumbnail(url, item);
        });
        queueList.scrollTop = queueList.scrollHeight;
    }

    $("clear-all-btn").addEventListener("click", () => {
        urlQueue = [];
        renderQueue();
    });

    dropZone.addEventListener("dragover", (e) => {
        e.preventDefault();
        dropZone.classList.add("dragover");
    });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
    dropZone.addEventListener("drop", (e) => {
        e.preventDefault();
        dropZone.classList.remove("dragover");
        const text = e.dataTransfer.getData("text/plain") || e.dataTransfer.getData("text/uri-list");
        extractUrls(text).forEach(addToQueue);
    });

    addBtn.addEventListener("click", () => {
        addToQueue(urlInput.value);
        urlInput.value = "";
    });

    urlInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") addBtn.click();
    });

    urlInput.addEventListener("paste", () => {
        setTimeout(() => {
            extractUrls(urlInput.value).forEach(addToQueue);
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

            urls.forEach((url, i) => {
                activeCards[i] = createCard(url, data.numbers[i]);
            });

            connectBatchWs(data.batch_id);
        } catch (err) {
            batchStatusMsg.textContent = "Failed to start batch: " + err.message;
            resetBatchState();
        }
    }

    function resetBatchState() {
        batchRunning = false;
        downloadAllBtn.disabled = urlQueue.length === 0;
        downloadAllBtn.textContent = "Download All";
    }

    function connectBatchWs(id) {
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        const ws = new WebSocket(`${proto}//${location.host}/ws/${id}`);

        ws.onmessage = (e) => {
            const d = JSON.parse(e.data);
            if (d.type === "progress") handleProgress(d);
            else if (d.type === "batch_complete") handleBatchComplete(d);
            else if (d.type === "batch_error") {
                batchStatusMsg.textContent = "Error: " + d.message;
                resetBatchState();
            }
        };

        ws.onopen = () => ws.send("ping");
    }

    function handleProgress(d) {
        const card = activeCards[d.url_index];
        if (!card) return;

        const bar = card.querySelector(".progress-bar");
        const msg = card.querySelector(".task-message");
        const status = card.querySelector(".task-status");

        bar.style.width = d.progress + "%";
        msg.textContent = d.message || "";

        const states = {
            completed: { label: "Done", color: "#4caf50", cls: "completed" },
            error: { label: "Error", color: "#f44336", cls: "error" },
            rate_limited: { label: "Rate Limited", color: "#ff9800", cls: "rate-limited" },
        };

        const state = states[d.status];
        if (state) {
            status.className = "task-status " + state.cls;
            status.textContent = state.label;
            if (d.status === "completed") bar.style.width = "100%";
            bar.style.background = state.color;
        } else {
            status.className = "task-status processing";
            status.textContent = "Downloading";
        }

        const pct = Math.round(((d.completed_count || 0) / (d.total || 1)) * 100);
        batchBar.style.width = pct + "%";
        batchStatusMsg.textContent = `Processing ${d.completed_count || 0} of ${d.total}`;
        batchCounts.textContent = `${d.active_count || 0} active`;
    }

    function handleBatchComplete(d) {
        batchBar.style.width = "100%";
        batchBar.style.background = "#4caf50";
        batchStatusMsg.textContent = `Done! ${d.successful} downloaded, ${d.failed} failed`;
        batchCounts.textContent = "";
        resetBatchState();
    }

    function createCard(url, number) {
        const card = document.createElement("div");
        card.className = "task-card";

        const header = document.createElement("div");
        header.className = "task-header";
        header.innerHTML = `<span class="task-number">#${number}</span><span class="task-status processing">Queued</span>`;

        const urlEl = document.createElement("div");
        urlEl.className = "task-url";
        urlEl.title = url;
        urlEl.textContent = url;

        const progressWrap = document.createElement("div");
        progressWrap.className = "progress-wrap";
        progressWrap.innerHTML = '<div class="progress-bar"></div>';

        const msgEl = document.createElement("div");
        msgEl.className = "task-message";
        msgEl.textContent = "Waiting...";

        card.append(header, urlEl, progressWrap, msgEl);
        taskList.appendChild(card);

        loadThumbnail(url, card);

        return card;
    }
});
