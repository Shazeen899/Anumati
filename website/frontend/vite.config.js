import {defineConfig} from 'vite';
export default defineConfig({base:'/website/', server:{port:5174, proxy:{'/website/api':'http://127.0.0.1:8010', '/api':'http://127.0.0.1:8010'}}, build:{outDir:'dist'}});
