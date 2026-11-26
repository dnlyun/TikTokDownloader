const assert = require("assert");
const {
  TikTokSelectionModel,
  canonicalTikTokPostUrl,
} = require("../chrome-extension/selection-model.js");

const a = "https://www.tiktok.com/@one/video/111?tracking=1";
const b = "https://www.tiktok.com/@two/photo/222/";
const c = "https://m.tiktok.com/@three/video/333";
const liveLikedPhoto = "https://www.tiktok.com/@mikaalafuente_/photo/7314799302549703942";
const liveLikedVideo = "https://www.tiktok.com/@mikkimouse404/video/7039638211248868610";

assert.equal(canonicalTikTokPostUrl(a), "https://www.tiktok.com/@one/video/111");
assert.equal(canonicalTikTokPostUrl(b), "https://www.tiktok.com/@two/photo/222");
assert.equal(canonicalTikTokPostUrl("https://example.com/video/111"), null);

const selected = new TikTokSelectionModel();
assert.equal(selected.add(a), true);
assert.equal(selected.add(b), true);
assert.equal(selected.add(a), false);
assert.deepEqual(selected.urls(), [
  "https://www.tiktok.com/@one/video/111",
  "https://www.tiktok.com/@two/photo/222",
]);
assert.equal(selected.indexOf(b), 2);

selected.remove(a);
selected.add(c);
selected.add(a);
assert.deepEqual(selected.urls(), [
  "https://www.tiktok.com/@two/photo/222",
  "https://www.tiktok.com/@three/video/333",
  "https://www.tiktok.com/@one/video/111",
]);
assert.equal(selected.indexOf(a), 3);

selected.clear();
assert.equal(selected.size, 0);

selected.add(liveLikedVideo);
selected.add(liveLikedPhoto);
assert.deepEqual(selected.urls(), [liveLikedVideo, liveLikedPhoto]);
console.log("PASS: extension canonicalization, dedupe, selection order, reselection, and clear");
