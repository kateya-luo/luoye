const {spawn} = require('node:child_process');
const path = require('node:path');
const readline = require('node:readline');

class BleAgent {
  constructor({app, onEvent}) {
    this.app = app;
    this.onEvent = onEvent;
    this.process = null;
  }

  start() {
    if (this.process) return;
    const packagedExe = path.join(process.resourcesPath, 'ble-agent', 'ClearMeeting.BleAgent.exe');
    const developmentDll = path.join(
      __dirname,
      'native',
      'ClearMeeting.BleAgent',
      'bin',
      'Release',
      'net8.0-windows10.0.19041.0',
      'ClearMeeting.BleAgent.dll',
    );
    const executable = this.app.isPackaged ? packagedExe : 'dotnet';
    const args = this.app.isPackaged ? [] : [developmentDll];
    this.process = spawn(executable, args, {windowsHide: true, stdio: ['pipe', 'pipe', 'pipe']});

    readline.createInterface({input: this.process.stdout}).on('line', (line) => {
      try { this.onEvent(JSON.parse(line)); }
      catch { this.onEvent({type: 'error', data: {operation: 'agent_output', message: line}}); }
    });
    readline.createInterface({input: this.process.stderr}).on('line', (line) => {
      this.onEvent({type: 'agent_log', data: {message: line}});
    });
    this.process.on('error', (error) => {
      this.onEvent({type: 'error', data: {operation: 'agent_start', message: error.message}});
      this.process = null;
    });
    this.process.on('exit', (code, signal) => {
      this.onEvent({type: 'agent_exit', data: {code, signal}});
      this.process = null;
    });
  }

  send(command) {
    if (!this.process) this.start();
    if (!this.process?.stdin?.writable) throw new Error('BLE Agent 未启动');
    this.process.stdin.write(`${JSON.stringify(command)}\n`);
  }

  stop() {
    if (!this.process) return;
    try { this.send({command: 'quit'}); }
    catch {}
    const processToStop = this.process;
    setTimeout(() => { if (!processToStop.killed) processToStop.kill(); }, 1500).unref();
  }
}

module.exports = {BleAgent};
