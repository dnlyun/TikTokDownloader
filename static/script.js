document.addEventListener("DOMContentLoaded", () => {
    const dropZone = document.getElementById("drop-zone");
    const urlInput = document.getElementById("url-input");
    const downloadBtn = document.getElementById("download-btn");
    const taskList = document.getElementById("task-list")

    function isValidUrl(text) {
        return /^https?:\/\/(www\.|vm\.)?tiktok\.com\/.+/.test(text.trim());
    }

    dropZone.addEventListener("dragover", (e) => {
        e.preventDefault();
        dropZone.classList.add("dragover");
    });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
    dropZone.addEventListener("drop", (e) => {
        e.preventDefault();
        dropZone.classList.remove("dragover");
        const text = (e.dataTransfer.getData("text/plain") || e.dataTransfer.getData("text/uri-list")).trim();
        if (text && isValidUrl(text)) {
            startDownload(text);
        }
    });

    downloadBtn.addEventListener("click", () => {
        const url = urlInput.value.trim();
        if (isValidUrl(url)) {
            startDownload(url);
            urlInput.value = "";
        }
    });

    urlInput.addEventListener("keydown", (e) => {
        if (e.key == "Enter") downloadBtn.click();
    });

    urlInput.addEventListener("paste", () => {
        setTimeout(() => {
            const url = urlInput.value.trim();
            if (isValidUrl(url)) {
                startDownload(url);
                urlInput.value = "";
            }
        }, 50);
    });

    async function startDownload(url) {
        const card = createCard(url, "...");

        try {
            const resp = await fetch("/api/download", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ url }),
            });
            const data = await resp.json()
            updateCardNumber(card, data.number);
            connectProgress(card, data.task_id);
        } catch (err) {
            setCardError(card, "Failed to start: " + err.message);
        }
    }

    function createCard(url, number) {
        const card = document.createElement("div");
        card.className = "task-card";
        card.innerHTML = `
            <div class="task-header">
                <span class="task-number">#${number}</span>
                <span class="task-status processing">Downloading</span>
            </div>
            <div class="task-url" title="${url}">${url}</div>
            <div class="progress-wrap"><div class="progress-bar"></div></div>
            <div class="task-message">Starting...</div>
            <div class="task-actions"></div>
        `;
        taskList.prepend(card);
        return card;
    }

    function updateCardNumber(card, number) {
        card.querySelector(".task-number").textContent = "#" + number;
    }

    function connectProgress(card, taskId) {
        const proto = location.protocol == "https:" ? "wss:" : "ws:";
        const ws = new WebSocket(`${proto}//${location.host}/ws/${taskId}`);

        ws.onmessage = (e) => {
            const d = JSON.parse(e.data);
            updateCard(card, d, taskId);
        };
        ws.onopen = () => ws.send("ping");

        const poll = setInterval(async () => {
            try {
                const resp = await fetch(`/api/status/${taskId}`);
                const d = await resp.json();
                updateCard(card, d, taskId);
                if (d.status === "completed" || d.status === "error") clearInterval(poll);
            } catch (_) {}
        }, 3000);

        ws.onclose = () => {}
        card._pollInterval = poll;
    }

    function updateCard(card, data, taskId) {
        const bar = card.querySelector(".progress-bar");
        const msg = card.querySelector(".task-message");
        const status = card.querySelector(".task-status");
        const actions = card.querySelector(".task-actions");

        bar.style.width = data.progress + "%";
        msg.textContent = data.message || "";

        if (data.status === "completed") {
            status.className = "task-status completed";
            status.textContent = "Done";
            bar.style.width = "100%";
            bar.style.background = "#4caf50";
            actions.innerHTML = `<a class="save-btn" href=/api/download/${taskId}" download>Save File</a>`;
        } else if (data.status === "error") {
            status.className = "task-status error";
            status.textContent = "Error";
            bar.style.background = "#f44336";
            if (card._pollInterval) clearInterval(card._pollInterval);
        }
    }

    function setCardError(card, message) {
        card.querySelector(".task-status").className = "task-status error";
        card.querySelector(".task-status").textContent = "Error";
        card.querySelector(".task-message").textContent = message;
        card.querySelector(".progress-bard").style.background = "#f44336"
    }
});