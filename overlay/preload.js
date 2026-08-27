const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('junaBridge', {
  onState: (cb) => ipcRenderer.on('juna-state', (_e, s) => cb(s)),
  onPet: (cb) => ipcRenderer.on('pet', (_e, meta, sheetSrc) => cb(meta, sheetSrc)),
  dragStart: () => ipcRenderer.send('drag-start'),
  dragEnd: () => ipcRenderer.send('drag-end'),
  contextMenu: () => ipcRenderer.send('context-menu'),
  hit: (b) => ipcRenderer.send('hit', b),
});
