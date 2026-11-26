(function () {
  "use strict";

  const existing = globalThis.__tiktokOrderedSelector;
  if (existing) {
    existing.show();
    existing.scan();
    return;
  }

  const model = new TikTokSelectionModel();
  const overlays = new Map();
  let scanTimer = null;

  const toolbar = document.createElement("section");
  toolbar.id = "tt-ordered-selector-toolbar";
  toolbar.setAttribute("aria-label", "TikTok link selector");
  toolbar.innerHTML = `
    <div class="ttos-title">TikTok selector</div>
    <div class="ttos-count" aria-live="polite">0 selected</div>
    <div class="ttos-actions">
      <button type="button" data-action="select-loaded">Select all loaded</button>
      <button type="button" data-action="clear">Clear</button>
      <button type="button" class="ttos-primary" data-action="copy" disabled>Copy selected links</button>
      <button type="button" data-action="close" aria-label="Close selector">Close</button>
    </div>
    <div class="ttos-message" aria-live="polite"></div>
  `;
  document.documentElement.appendChild(toolbar);

  const count = toolbar.querySelector(".ttos-count");
  const message = toolbar.querySelector(".ttos-message");
  const copyButton = toolbar.querySelector('[data-action="copy"]');

  function postLinks() {
    const found = new Map();
    for (const anchor of document.querySelectorAll('a[href*="/video/"], a[href*="/photo/"]')) {
      const url = canonicalTikTokPostUrl(anchor.href);
      if (!url || found.has(url) || toolbar.contains(anchor)) continue;
      found.set(url, anchor);
    }
    return found;
  }

  function tileFor(anchor) {
    return anchor.closest(
      '[data-e2e*="user-post-item"], [data-e2e*="user-liked-item"], [data-e2e*="post-item"], li'
    ) || anchor.parentElement || anchor;
  }

  function createOverlay(url, anchor) {
    const tile = tileFor(anchor);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ttos-select-button";
    button.dataset.url = url;
    button.setAttribute("aria-label", "Select this TikTok post");
    button.innerHTML = '<span class="ttos-check" aria-hidden="true">✓</span><span class="ttos-order"></span>';
    button.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      model.toggle(url);
      refresh();
    });
    if (getComputedStyle(tile).position === "static") {
      tile.classList.add("ttos-positioned-tile");
    }
    tile.appendChild(button);
    overlays.set(url, { button, tile });
  }

  function scan() {
    const links = postLinks();
    for (const [url, anchor] of links) {
      const current = overlays.get(url);
      if (!current || !current.button.isConnected) createOverlay(url, anchor);
    }
    for (const [url, overlay] of overlays) {
      if (!overlay.button.isConnected) overlays.delete(url);
    }
    refresh();
  }

  function scheduleScan() {
    clearTimeout(scanTimer);
    scanTimer = setTimeout(scan, 120);
  }

  function refresh() {
    for (const [url, overlay] of overlays) {
      const order = model.indexOf(url);
      overlay.button.classList.toggle("ttos-selected", order > 0);
      overlay.button.setAttribute("aria-pressed", order > 0 ? "true" : "false");
      overlay.button.setAttribute(
        "aria-label",
        order > 0 ? `Selected as number ${order}` : "Select this TikTok post"
      );
      overlay.button.querySelector(".ttos-order").textContent = order ? String(order) : "";
    }
    count.textContent = `${model.size} selected`;
    copyButton.disabled = model.size === 0;
  }

  async function copySelected() {
    const text = model.urls().join("\n");
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const field = document.createElement("textarea");
      field.value = text;
      field.className = "ttos-copy-fallback";
      document.documentElement.appendChild(field);
      field.select();
      document.execCommand("copy");
      field.remove();
    }
    message.textContent = `Copied ${model.size} link${model.size === 1 ? "" : "s"} in selection order.`;
  }

  function close() {
    observer.disconnect();
    clearTimeout(scanTimer);
    for (const overlay of overlays.values()) {
      overlay.button.remove();
      overlay.tile.classList.remove("ttos-positioned-tile");
    }
    overlays.clear();
    toolbar.remove();
    delete globalThis.__tiktokOrderedSelector;
  }

  toolbar.addEventListener("click", event => {
    const action = event.target.closest("button")?.dataset.action;
    if (action === "select-loaded") {
      for (const url of postLinks().keys()) model.add(url);
      message.textContent = "Selected every currently loaded post.";
      refresh();
    } else if (action === "clear") {
      model.clear();
      message.textContent = "Selection cleared.";
      refresh();
    } else if (action === "copy") {
      copySelected();
    } else if (action === "close") {
      close();
    }
  });

  const observer = new MutationObserver(mutations => {
    const pageChanged = mutations.some(mutation => {
      const target = mutation.target;
      return !toolbar.contains(target) && !target.closest?.(".ttos-select-button");
    });
    if (pageChanged) scheduleScan();
  });
  observer.observe(document.body, { childList: true, subtree: true });

  globalThis.__tiktokOrderedSelector = {
    scan,
    show() {
      toolbar.hidden = false;
      toolbar.scrollIntoView({ block: "nearest" });
    },
  };
  scan();
})();
