"use strict";

function normalizeTikTokUrl(value) {
    try {
        const url = new URL(value.trim());
        const hosts = ["tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"];
        if (!["http:", "https:"].includes(url.protocol) || !hosts.includes(url.hostname.toLowerCase()) || url.username || url.password || url.port) return null;
        const path = url.pathname.replace(/\/+$/, "");
        const post = path.match(/^\/@[^/]+\/(video|photo)\/\d+$/);
        const short = ["vm.tiktok.com", "vt.tiktok.com"].includes(url.hostname) && /^\/[A-Za-z0-9]+$/.test(path);
        const shared = /^\/t\/[A-Za-z0-9]+$/.test(path);
        if (!post && !short && !shared) return null;
        const host = post || shared ? "www.tiktok.com" : url.hostname;
        return `https://${host}${path}${short || shared ? "/" : ""}`;
    } catch {
        return null;
    }
}

function tikTokKey(url) {
    return url.match(/\/(?:video|photo)\/(\d+)/)?.[1] || url;
}

function extractTikTokUrls(text) {
    const values = text.match(/https?:\/\/[^\s<>"']+/gi) || [];
    return values.map(value => normalizeTikTokUrl(value.replace(/[),.;!?\]}]+$/, ""))).filter(Boolean);
}

function extractDroppedUrls(transfer, Parser = DOMParser) {
    // A selection can carry captions as plain text and post links only in HTML.
    // Read every representation synchronously while the drop data is accessible.
    const urls = [];
    for (const type of ["text/uri-list", "text/plain", "text/x-moz-url"]) {
        const text = transfer.getData(type) || "";
        urls.push(...extractTikTokUrls(text.split("\n").filter(line => !line.startsWith("#")).join("\n")));
    }
    const html = transfer.getData("text/html");
    if (html) {
        // The detached document is never inserted into the app.
        const doc = new Parser().parseFromString(html, "text/html");
        for (const link of doc.querySelectorAll("a[href]")) {
            const href = link.getAttribute("href");
            try {
                const url = normalizeTikTokUrl(new URL(href, "https://www.tiktok.com/").href);
                if (url) urls.push(url);
            } catch { /* Ignore malformed links. */ }
        }
    }
    const seen = new Set();
    return urls.filter(url => !seen.has(tikTokKey(url)) && seen.add(tikTokKey(url)));
}

// Keep URL handling testable without a browser or a live TikTok request.
if (typeof module !== "undefined" && module.exports) {
    module.exports = { normalizeTikTokUrl, tikTokKey, extractTikTokUrls, extractDroppedUrls };
}

if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", () => {
    const $ = id => document.getElementById(id);
    const storageKey = "tiktok-downloader.v2";
    const dropZone = $("drop-zone");
    const urlInput = $("url-input");
    const downloadButton = $("download-all-btn");
    const retryButton = $("retry-failed-btn");
    const feedback = $("feedback");
    const queueRows = new Map();
    const cards = new Map();
    const thumbnailJobs = [];
    let thumbnailActive = 0;
    let urlQueue = [];
    let batchId = null;
    let pendingCreate = null;
    let snapshot = null;
    let submitting = false;
    let restoring = true;
    let socket = null;
    let reconnectTimer = null;
    let reconnectAttempt = 0;
    let lastSnapshotAt = 0;
    let lastServerUpdate = 0;
    let polling = false;
    let discovering = false;
    let needsDiscovery = true;
    let storageWarning = false;

    function message(text, isError = false) {
        feedback.textContent = text;
        feedback.classList.toggle("error", isError);
    }

    function saveLocal() {
        try {
            localStorage.setItem(storageKey, JSON.stringify({ queue: urlQueue, batchId, pendingCreate }));
        } catch {
            if (!storageWarning) {
                storageWarning = true;
                message("Browser storage is unavailable. Keep this tab open until your queued links are submitted.", true);
            }
        }
    }

    try {
        const saved = JSON.parse(localStorage.getItem(storageKey) || "{}");
        const seen = new Set();
        urlQueue = (Array.isArray(saved.queue) ? saved.queue : []).filter(value => typeof value === "string")
            .map(normalizeTikTokUrl).filter(url => url && !seen.has(tikTokKey(url)) && seen.add(tikTokKey(url)));
        batchId = typeof saved.batchId === "string" ? saved.batchId : null;
        if (typeof saved.pendingCreate?.request_id === "string" && Array.isArray(saved.pendingCreate.urls)) {
            const urls = saved.pendingCreate.urls.filter(value => typeof value === "string").map(normalizeTikTokUrl).filter(Boolean);
            if (urls.length) {
                pendingCreate = { request_id: saved.pendingCreate.request_id, urls };
                for (const url of urls) if (!seen.has(tikTokKey(url))) {
                    seen.add(tikTokKey(url));
                    urlQueue.push(url);
                }
            }
        }
    } catch {
        message("The saved browser queue could not be read. Server downloads are still available.", true);
    }

    const thumbnailObserver = typeof IntersectionObserver !== "undefined" ? new IntersectionObserver(entries => {
        for (const entry of entries) if (entry.isIntersecting) {
            thumbnailObserver.unobserve(entry.target);
            thumbnailJobs.push(entry.target);
        }
        pumpThumbnails();
    }, { rootMargin: "80px" }) : null;

    function pumpThumbnails() {
        while (thumbnailActive < 3 && thumbnailJobs.length) {
            const target = thumbnailJobs.shift();
            if (!target.isConnected) continue;
            thumbnailActive++;
            loadThumbnail(target.dataset.thumbnailUrl, target).finally(() => {
                thumbnailActive--;
                pumpThumbnails();
            });
        }
    }

    async function loadThumbnail(url, target) {
        const placeholder = target.querySelector(".thumbnail-placeholder");
        if (!placeholder) return;
        placeholder.disabled = true;
        placeholder.title = "Loading preview…";
        const image = document.createElement("img");
        image.className = "queue-thumb";
        image.alt = "TikTok preview";
        const success = await new Promise(resolve => {
            const timer = setTimeout(() => resolve(false), 25000);
            image.onload = () => { clearTimeout(timer); resolve(true); };
            image.onerror = () => { clearTimeout(timer); resolve(false); };
            image.src = `/api/thumbnail?url=${encodeURIComponent(url)}`;
        });
        if (!target.isConnected) return;
        if (success) placeholder.replaceWith(image);
        else {
            image.src = "";
            placeholder.disabled = false;
            placeholder.title = "Preview unavailable — click to retry";
            placeholder.setAttribute("aria-label", placeholder.title);
        }
    }

    function addThumbnail(url, target) {
        const placeholder = document.createElement("button");
        placeholder.type = "button";
        placeholder.className = "queue-thumb placeholder-thumb thumbnail-placeholder";
        placeholder.textContent = /\/photo\//.test(url) ? "🖼" : "▶";
        placeholder.title = "Load preview";
        placeholder.setAttribute("aria-label", "Load TikTok preview");
        placeholder.addEventListener("click", () => {
            if (placeholder.disabled) return;
            placeholder.disabled = true;
            thumbnailJobs.push(target);
            pumpThumbnails();
        });
        target.dataset.thumbnailUrl = url;
        target.prepend(placeholder);
        if (thumbnailObserver) thumbnailObserver.observe(target);
        else setTimeout(() => { thumbnailJobs.push(target); pumpThumbnails(); }, 0);
    }

    function isRunning() {
        return snapshot && !["completed", "error", "cancelled"].includes(snapshot.status);
    }

    function updateButtons() {
        downloadButton.disabled = submitting || restoring || (!urlQueue.length && !pendingCreate);
        downloadButton.textContent = submitting ? "Sending…" : pendingCreate ? "Retry sending batch" : isRunning() ? "Add to current batch" : "Download All";
        retryButton.hidden = !(snapshot?.failed > 0);
        retryButton.disabled = submitting || restoring;
        $("clear-all-btn").disabled = submitting || Boolean(pendingCreate);
    }

    function renderQueue() {
        const visible = new Set(urlQueue.map(tikTokKey));
        for (const [key, row] of queueRows) if (!visible.has(key)) {
            thumbnailObserver?.unobserve(row);
            row.remove();
            queueRows.delete(key);
        }
        const fragment = document.createDocumentFragment();
        const locked = new Set((pendingCreate?.urls || []).map(tikTokKey));
        for (const url of urlQueue) {
            const key = tikTokKey(url);
            if (!queueRows.has(key)) {
                const item = document.createElement("div");
                item.className = "queue-item";
                const label = document.createElement("span");
                label.className = "queue-url";
                label.title = url;
                label.textContent = url;
                const remove = document.createElement("button");
                remove.className = "remove-btn";
                remove.textContent = "×";
                remove.setAttribute("aria-label", `Remove ${url}`);
                remove.addEventListener("click", () => {
                    urlQueue = urlQueue.filter(value => tikTokKey(value) !== key);
                    saveLocal();
                    renderQueue();
                });
                item.append(label, remove);
                queueRows.set(key, item);
                fragment.appendChild(item);
                addThumbnail(url, item);
            }
            queueRows.get(key).querySelector(".remove-btn").disabled = submitting || locked.has(key);
        }
        $("queue-list").appendChild(fragment);
        $("url-queue").style.display = urlQueue.length ? "block" : "none";
        $("queue-count").textContent = `${urlQueue.length} link${urlQueue.length === 1 ? "" : "s"}`;
        updateButtons();
    }

    function enqueueText(text) {
        const urls = extractTikTokUrls(text);
        if (!urls.length) {
            message("No supported TikTok links found. Paste video, photo, or TikTok share links.", true);
            return false;
        }
        const seen = new Set(urlQueue.map(tikTokKey));
        let added = 0;
        for (const url of urls) if (!seen.has(tikTokKey(url))) {
            seen.add(tikTokKey(url));
            urlQueue.push(url);
            added++;
        }
        saveLocal();
        renderQueue();
        $("queue-list").scrollTop = $("queue-list").scrollHeight;
        message(added ? `Added ${added} link${added === 1 ? "" : "s"}.${urls.length > added ? " Duplicate links skipped." : ""}` : "Those links are already queued.");
        return true;
    }

    async function requestJson(path, body) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 15000);
        try {
            const response = await fetch(path, {
                signal: controller.signal,
                cache: "no-store",
                ...(body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
            });
            let data;
            try { data = await response.json(); } catch { data = {}; }
            if (!response.ok) {
                const detail = data.detail || data.error;
                const description = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map(item => item.msg).join("; ") : `Request failed (${response.status})`;
                const error = new Error(description);
                error.status = response.status;
                throw error;
            }
            if (!data || typeof data !== "object") throw new Error("The server returned an unexpected response");
            return data;
        } catch (error) {
            if (error.name === "AbortError") throw new Error("The server took too long to respond");
            throw error;
        } finally {
            clearTimeout(timer);
        }
    }

    function finishSubmission(urls) {
        const submitted = new Set(urls.map(tikTokKey));
        urlQueue = urlQueue.filter(url => !submitted.has(tikTokKey(url)));
        pendingCreate = null;
        saveLocal();
        renderQueue();
    }

    async function submitQueue() {
        if (submitting || restoring || (!urlQueue.length && !pendingCreate)) return;
        submitting = true;
        const appending = !pendingCreate && Boolean(isRunning());
        if (!appending && !pendingCreate) {
            pendingCreate = { request_id: crypto.randomUUID(), urls: [...urlQueue] };
            saveLocal();
        }
        const urls = appending ? [...urlQueue] : [...pendingCreate.urls];
        const body = appending ? { batch_id: batchId, urls } : pendingCreate;
        renderQueue();
        message(appending ? "Adding links to your batch…" : "Sending your batch…");
        try {
            const data = await requestJson(appending ? "/api/batch/add" : "/api/batch", body);
            if (!data.batch_id || !data.snapshot) throw new Error("The server did not confirm the batch. Your links have been kept");
            selectBatch(data.batch_id);
            applySnapshot(data.snapshot);
            finishSubmission(urls);
            message(data.duplicates ? `${data.duplicates} duplicate link${data.duplicates === 1 ? "" : "s"} skipped.` : "Batch saved. You can keep adding links while it downloads.");
        } catch (error) {
            if (error.status >= 400 && error.status < 500) pendingCreate = null;
            saveLocal();
            message(`${error.message}. Your links are still queued.${pendingCreate ? " Retry sending to confirm the same batch." : ""}`, true);
        } finally {
            submitting = false;
            renderQueue();
        }
    }

    function createCard(result) {
        const card = document.createElement("div");
        card.className = "task-card";
        const header = document.createElement("div");
        header.className = "task-header";
        const number = document.createElement("span");
        number.className = "task-number";
        number.textContent = `#${result.number}`;
        const status = document.createElement("span");
        status.className = "task-status queued";
        header.append(number, status);
        const url = document.createElement("div");
        url.className = "task-url";
        url.textContent = result.url;
        url.title = result.url;
        const progress = document.createElement("div");
        progress.className = "progress-wrap";
        const bar = document.createElement("div");
        bar.className = "progress-bar";
        progress.appendChild(bar);
        const detail = document.createElement("div");
        detail.className = "task-message";
        card.append(header, url, progress, detail);
        addThumbnail(result.url, card);
        return { element: card, number, status, bar, detail };
    }

    function applySnapshot(data) {
        if (!data || data.batch_id !== batchId || !Array.isArray(data.results)) return;
        const updated = Number(data.updated_at) || Date.parse(data.updated_at) || 0;
        if (updated && updated < lastServerUpdate) return;
        lastServerUpdate = updated;
        lastSnapshotAt = Date.now();
        snapshot = data;
        const fragment = document.createDocumentFragment();
        const seen = new Set();
        for (const result of data.results) {
            const key = result.url_index;
            seen.add(key);
            if (!cards.has(key)) {
                const card = createCard(result);
                cards.set(key, card);
                fragment.appendChild(card.element);
            }
            const card = cards.get(key);
            card.number.textContent = `#${result.number}`;
            const state = {
                queued: ["Queued", "queued", "#777"],
                pending: ["Queued", "queued", "#777"],
                starting: ["Starting", "processing", "#fe2c55"],
                processing: ["Downloading", "processing", "#fe2c55"],
                retrying: ["Retrying", "retrying", "#ff9800"],
                rate_limited: ["Cooling down", "retrying", "#ff9800"],
                completed: ["Done", "completed", "#4caf50"],
                error: ["Failed", "error", "#f44336"],
            }[result.status] || [result.status || "Queued", "queued", "#777"];
            card.status.textContent = state[0];
            card.status.className = `task-status ${state[1]}`;
            card.bar.style.width = `${result.status === "completed" ? 100 : Math.max(0, Math.min(100, result.progress || 0))}%`;
            card.bar.style.background = state[2];
            card.detail.textContent = result.message || result.error || (result.status === "queued" ? "Waiting…" : "");
        }
        for (const [key, card] of cards) if (!seen.has(key)) {
            thumbnailObserver?.unobserve(card.element);
            card.element.remove();
            cards.delete(key);
        }
        $("task-list").appendChild(fragment);
        $("batch-progress").style.display = "block";
        const complete = !isRunning();
        const completed = data.completed_count ?? ((data.successful || 0) + (data.failed || 0));
        $("batch-bar").style.width = `${complete ? 100 : Math.round(completed / Math.max(1, data.total) * 100)}%`;
        $("batch-bar").style.background = complete ? (data.failed ? "#ff9800" : "#4caf50") : "#fe2c55";
        $("batch-status-msg").textContent = complete ? `${data.successful || 0} downloaded, ${data.failed || 0} failed` : `${completed} of ${data.total} finished`;
        $("batch-counts").textContent = complete ? "" : `${data.active_count || 0} active`;
        retryButton.textContent = `Retry ${data.failed || 0} failed`;
        updateButtons();
    }

    function selectBatch(id) {
        if (batchId !== id) {
            socket?.close();
            socket = null;
            clearTimeout(reconnectTimer);
            for (const card of cards.values()) thumbnailObserver?.unobserve(card.element);
            cards.clear();
            $("task-list").replaceChildren();
            snapshot = null;
            lastServerUpdate = 0;
            batchId = id;
        }
        saveLocal();
        connectSocket();
    }

    function connectSocket() {
        if (!batchId || (socket && socket.readyState < WebSocket.CLOSING)) return;
        clearTimeout(reconnectTimer);
        const id = batchId;
        const protocol = location.protocol === "https:" ? "wss:" : "ws:";
        const current = new WebSocket(`${protocol}//${location.host}/ws/${encodeURIComponent(id)}`);
        socket = current;
        $("connection-status").textContent = "Connecting…";
        current.onopen = () => {
            if (socket !== current) return;
            reconnectAttempt = 0;
            $("connection-status").textContent = "Live updates";
            current.send("ping");
        };
        current.onmessage = event => {
            if (socket !== current || batchId !== id) return;
            try {
                const data = JSON.parse(event.data);
                if (data.type === "snapshot") applySnapshot(data);
                else if (["batch_complete", "batch_error"].includes(data.type)) refreshSnapshot();
            } catch {
                refreshSnapshot();
            }
        };
        current.onerror = () => current.close();
        current.onclose = () => {
            if (socket !== current || batchId !== id) return;
            socket = null;
            $("connection-status").textContent = "Reconnecting · checking saved progress";
            refreshSnapshot();
            const delay = Math.min(30000, 1000 * 2 ** Math.min(reconnectAttempt++, 5));
            reconnectTimer = setTimeout(connectSocket, delay);
        };
    }

    async function refreshSnapshot() {
        if (!batchId || polling) return;
        const id = batchId;
        polling = true;
        try {
            const data = await requestJson(`/api/batch/${encodeURIComponent(id)}`);
            if (batchId !== id) return;
            applySnapshot(data);
            if (!socket || socket.readyState !== WebSocket.OPEN) $("connection-status").textContent = "Progress saved · reconnecting live updates";
        } catch (error) {
            if (batchId !== id) return;
            $("connection-status").textContent = error.status === 404 ? "This batch is no longer available" : "Server unavailable · retrying automatically";
        } finally {
            polling = false;
        }
    }

    async function restoreBatch() {
        if (discovering) return;
        discovering = true;
        try {
            const data = await requestJson("/api/batches");
            needsDiscovery = false;
            const batches = Array.isArray(data.batches) ? data.batches : [];
            const submitted = pendingCreate && batches.find(item => item.request_id === pendingCreate.request_id);
            const saved = submitted || batches.find(item => item.batch_id === batchId) || batches.find(item => ["running", "queued"].includes(item.status)) || batches[0];
            if (saved) {
                selectBatch(saved.batch_id);
                applySnapshot(saved);
                if (submitted) finishSubmission(pendingCreate.urls);
            } else {
                batchId = null;
                saveLocal();
            }
        } catch {
            needsDiscovery = true;
            if (batchId) {
                selectBatch(batchId);
                refreshSnapshot();
            }
            message("Could not load saved downloads. Your queued links are kept; reconnecting automatically.", true);
        } finally {
            discovering = false;
            restoring = false;
            renderQueue();
        }
    }

    $("add-btn").addEventListener("click", () => {
        if (enqueueText(urlInput.value)) urlInput.value = "";
    });
    urlInput.addEventListener("keydown", event => {
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            $("add-btn").click();
        }
    });
    urlInput.addEventListener("paste", event => {
        const text = event.clipboardData?.getData("text/plain");
        if (text && extractTikTokUrls(text).length) {
            event.preventDefault();
            enqueueText(text);
            urlInput.value = "";
        }
    });
    $("clear-all-btn").addEventListener("click", () => {
        if (pendingCreate || submitting) return;
        urlQueue = [];
        saveLocal();
        renderQueue();
        message("");
    });
    dropZone.addEventListener("dragover", event => {
        event.preventDefault();
        dropZone.classList.add("dragover");
    });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
    dropZone.addEventListener("drop", event => {
        event.preventDefault();
        dropZone.classList.remove("dragover");
        const urls = extractDroppedUrls(event.dataTransfer);
        if (urls.length) enqueueText(urls.join("\n"));
        else message("This selection contains text but no TikTok post links. Drag the video thumbnails/links, or copy the post URLs.", true);
    });
    downloadButton.addEventListener("click", submitQueue);
    retryButton.addEventListener("click", async () => {
        if (!batchId || submitting) return;
        submitting = true;
        renderQueue();
        try {
            const data = await requestJson(`/api/batch/${encodeURIComponent(batchId)}/retry`, {});
            applySnapshot(data.snapshot || data);
            message("Failed downloads queued for another attempt.");
        } catch (error) {
            message(`Could not retry downloads: ${error.message}`, true);
        } finally {
            submitting = false;
            renderQueue();
        }
    });
    setInterval(() => {
        if (batchId && (!socket || socket.readyState !== WebSocket.OPEN || Date.now() - lastSnapshotAt > 15000)) refreshSnapshot();
        else if (!batchId && needsDiscovery && !submitting) restoreBatch();
        if (socket?.readyState === WebSocket.OPEN) socket.send("ping");
    }, 5000);
    window.addEventListener("online", () => {
        if (batchId) { connectSocket(); refreshSnapshot(); }
        else restoreBatch();
    });
    window.addEventListener("focus", () => refreshSnapshot());
    renderQueue();
    restoreBatch();
});
