const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('panelBridge', {
  onSessions: (cb) => ipcRenderer.on('sessions', (_e, list) => cb(list)),
  raise: (pid) => ipcRenderer.send('panel-raise', pid),
  over: (v) => ipcRenderer.send('panel-over', v),
  sized: (h) => ipcRenderer.send('panel-size', h),
});
