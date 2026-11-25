const assert = require('node:assert/strict');
const {extractDroppedUrls} = require('../static/script.js');
const first = 'https://www.tiktok.com/@one/video/123';
const second = 'https://www.tiktok.com/@two/photo/456';
// Stub only the browser HTML parser; exercise all transfer/normalization/dedupe logic.
class Parser {
    parseFromString(html) {
        assert.equal(html, '<selection>');
        return {querySelectorAll: () => [first+'?tracking=1','/@two/photo/456','https://evil.test/video/789','javascript:alert(1)'].map(href => ({getAttribute: () => href}))};
    }
}
const transfer = data => ({getData: type => data[type] || ''});
assert.deepEqual(extractDroppedUrls(transfer({'text/plain': 'Caption only', 'text/html':'<selection>', 'text/uri-list':first}),Parser), [first,second]);
assert.deepEqual(extractDroppedUrls(transfer({'text/plain':'Caption only'}),Parser), []);
assert.deepEqual(extractDroppedUrls(transfer({'text/uri-list':'# comment\n'+first+'\n'+second}),Parser),[first,second]);
assert.deepEqual(extractDroppedUrls(transfer({'text/x-moz-url':first+'\nCaption'}),Parser),[first]);
console.log('PASS: mixed HTML/text/URI drops, relative links, dedupe, invalid links, and text-only selections');
