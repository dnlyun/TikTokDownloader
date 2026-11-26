(function (root, factory) {
  "use strict";
  const exports = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = exports;
  root.TikTokSelectionModel = exports.TikTokSelectionModel;
  root.canonicalTikTokPostUrl = exports.canonicalTikTokPostUrl;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function canonicalTikTokPostUrl(value) {
    try {
      const url = new URL(value, "https://www.tiktok.com/");
      if (!["tiktok.com", "www.tiktok.com", "m.tiktok.com"].includes(url.hostname.toLowerCase())) {
        return null;
      }
      const match = url.pathname.match(/^\/(?:@[^/]+\/)?(video|photo)\/(\d+)\/?$/i);
      if (!match) return null;
      const creator = url.pathname.match(/^\/@([^/]+)\//)?.[1];
      const prefix = creator ? `/@${creator}` : "";
      return `https://www.tiktok.com${prefix}/${match[1].toLowerCase()}/${match[2]}`;
    } catch {
      return null;
    }
  }

  class TikTokSelectionModel {
    constructor() {
      this.items = new Map();
    }

    has(url) {
      const canonical = canonicalTikTokPostUrl(url);
      return canonical ? this.items.has(canonical) : false;
    }

    add(url) {
      const canonical = canonicalTikTokPostUrl(url);
      if (!canonical || this.items.has(canonical)) return false;
      this.items.set(canonical, true);
      return true;
    }

    remove(url) {
      const canonical = canonicalTikTokPostUrl(url);
      return canonical ? this.items.delete(canonical) : false;
    }

    toggle(url) {
      if (this.has(url)) {
        this.remove(url);
        return false;
      }
      this.add(url);
      return true;
    }

    clear() {
      this.items.clear();
    }

    indexOf(url) {
      const canonical = canonicalTikTokPostUrl(url);
      if (!canonical || !this.items.has(canonical)) return 0;
      return Array.from(this.items.keys()).indexOf(canonical) + 1;
    }

    urls() {
      return Array.from(this.items.keys());
    }

    get size() {
      return this.items.size;
    }
  }

  return { TikTokSelectionModel, canonicalTikTokPostUrl };
});
