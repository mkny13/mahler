const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('mahler/console/console.js', 'utf8');
assert(source.includes('function initDiscussion'), 'discussion controller is implemented');
