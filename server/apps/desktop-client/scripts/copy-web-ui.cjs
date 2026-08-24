const fs = require('node:fs');
const path = require('node:path');

const source = path.resolve(__dirname, '..', '..', 'web-client', 'dist');
const destination = path.resolve(__dirname, '..', 'app', 'client');
fs.rmSync(destination, {recursive: true, force: true});
fs.cpSync(source, destination, {recursive: true});
console.log(`Desktop UI copied to ${destination}`);
